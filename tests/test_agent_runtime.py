"""Agent SERVER-SIDE (`agent_runtime`) — les invariants qui tiennent la surface
publique : allowlist fail-closed, bornes (tours d'outils, taille de sortie),
composition du prompt système, et forme du fil rendu au client.

Logique PURE + stubs (convention du repo) : ni DB, ni LLM réel — `agent_llm.complete`
est remplacé par un scénario scripté.
"""
import pytest

from oto_mcp import agent_llm, agent_runtime
from oto_mcp.capabilities import agent as agent_cap


# ── Allowlist : le preset du projet est un PLAFOND, jamais un plancher ───────
def test_allowlist_defaults_to_project_preset():
    row = {"mcp_tools": ["fr_search", "serper_web_search"]}
    assert agent_cap._allowlist(row, None) == frozenset({"fr_search", "serper_web_search"})


def test_allowlist_can_only_narrow():
    row = {"mcp_tools": ["fr_search", "serper_web_search"]}
    # Un outil hors preset demandé par l'appelant est ÉCARTÉ (intersection).
    assert agent_cap._allowlist(row, ["fr_search", "gmail_search"]) == frozenset({"fr_search"})
    assert agent_cap._allowlist(row, ["gmail_search"]) == frozenset()


def test_allowlist_empty_preset_stays_empty():
    assert agent_cap._allowlist({"mcp_tools": []}, ["fr_search"]) == frozenset()


# ── Exécution d'outil : fail-closed AVANT toute résolution ───────────────────
@pytest.mark.asyncio
async def test_execute_tool_refuses_outside_allowlist():
    spec = agent_runtime.AgentSpec(system="s", tools=frozenset({"fr_search"}))
    text, is_error = await agent_runtime.execute_tool(
        spec, agent_llm.ToolCall(id="t1", name="gmail_search", arguments={}))
    assert is_error is True
    assert "gmail_search" in text
    # L'erreur NOMME ce qui est permis : le modèle doit pouvoir se corriger seul.
    assert "fr_search" in text


@pytest.mark.asyncio
async def test_execute_tool_unknown_tool_is_a_result_not_a_raise():
    """Un outil de l'allowlist absent du serveur (registre non lié en test) revient
    en résultat d'erreur — jamais une exception qui casserait le tour."""
    spec = agent_runtime.AgentSpec(system="s", tools=frozenset({"nope_tool"}))
    text, is_error = await agent_runtime.execute_tool(
        spec, agent_llm.ToolCall(id="t1", name="nope_tool", arguments={}))
    assert is_error is True
    assert "nope_tool" in text


# ── Bornes de sortie ─────────────────────────────────────────────────────────
def test_tool_output_is_truncated_with_an_explicit_mark():
    out = agent_runtime._cap("x" * (agent_runtime.MAX_TOOL_OUTPUT_CHARS + 5000))
    assert len(out) < agent_runtime.MAX_TOOL_OUTPUT_CHARS + 5000
    # Le modèle doit SAVOIR qu'il ne voit qu'un extrait.
    assert "tronquée" in out


def test_small_tool_output_is_untouched():
    assert agent_runtime._cap("court") == "court"
    assert agent_runtime._serialize({"a": 1}) == '{"a": 1}'


class _Block:
    def __init__(self, text):
        self.text = text


class _Result:
    """`ToolResult` minimal : sortie en TEXTE LIBRE, sans structuré."""
    structured_content = None

    def __init__(self, text):
        self.content = [_Block(text)]


def test_free_text_tool_output_is_not_lost_as_null():
    # extract_payload rend None sur du texte libre : s'y fier seul renverrait « null ».
    assert agent_runtime._result_text(_Result("42 résultats")) == "42 résultats"


def test_history_is_trimmed_from_the_start():
    msgs = [{"role": "user", "content": str(i)} for i in range(40)]
    trimmed = agent_runtime._trim(msgs)
    assert len(trimmed) == agent_runtime.MAX_HISTORY_MESSAGES
    # On garde la FIN (le contexte proche), pas le début.
    assert trimmed[-1]["content"] == "39"


# ── Prompt système : cadre + contenu de l'AUTEUR (jamais du visiteur) ────────
def test_project_system_prompt_composes_frame_brief_and_author_notes():
    prompt = agent_runtime.project_system_prompt({
        "name": "Veille CAC40", "brief_md": "Suivre les dépôts INPI.",
        "agent_prompt_md": "Toujours citer le SIREN."})
    assert "Veille CAC40" in prompt
    assert "Suivre les dépôts INPI." in prompt
    assert "Toujours citer le SIREN." in prompt
    # Le garde-fou anti-injection fait partie du cadre, pas d'une option.
    assert "consigne" in prompt.lower()


def test_project_spec_clamps_max_steps():
    # Plafond dur : une valeur folle en base ne fait pas boucler un endpoint public.
    assert agent_runtime.project_spec({"id": 1, "agent_max_steps": 99}, []).max_steps == 12
    # Absent / 0 = non réglé → défaut (jamais « zéro tour d'outil », qui rendrait
    # l'agent muet sans que personne ne comprenne pourquoi).
    for unset in (None, 0):
        assert agent_runtime.project_spec({"id": 1, "agent_max_steps": unset}, []).max_steps \
            == agent_runtime.DEFAULT_MAX_STEPS


# ── La boucle ────────────────────────────────────────────────────────────────
def _turn(text="", calls=(), stop="end_turn"):
    return agent_llm.Turn(
        text=text, tool_calls=tuple(calls), stop_reason=stop,
        raw_content=[{"type": "text", "text": text}] if text else [],
        usage={"input_tokens": 10, "output_tokens": 5})


@pytest.mark.asyncio
async def test_run_executes_a_tool_then_answers(monkeypatch):
    scripted = [
        _turn(calls=[agent_llm.ToolCall(id="t1", name="fr_search", arguments={"q": "x"})]),
        _turn(text="Voici la réponse."),
    ]
    monkeypatch.setattr(agent_llm, "complete", lambda **kw: scripted.pop(0))
    monkeypatch.setattr(agent_runtime, "tool_schemas",
                        lambda names: _async([{"name": "fr_search", "description": "",
                                               "input_schema": {"type": "object"}}]))

    async def fake_exec(spec, call):
        return ("3 résultats", False)
    monkeypatch.setattr(agent_runtime, "execute_tool", fake_exec)

    spec = agent_runtime.AgentSpec(system="s", tools=frozenset({"fr_search"}))
    res = await agent_runtime.run(spec, "cherche x")

    assert res.reply == "Voici la réponse."
    assert res.stopped == "end_turn"
    assert [s.tool for s in res.steps] == ["fr_search"]
    assert res.steps[0].ok is True
    # Usage cumulé sur les deux tours.
    assert res.usage["output_tokens"] == 10
    # Fil rendu au client : user → assistant(tool_use) → user(tool_result) → assistant.
    assert res.messages[0]["role"] == "user"
    assert res.messages[2]["content"][0]["type"] == "tool_result"
    assert res.messages[2]["content"][0]["tool_use_id"] == "t1"


@pytest.mark.asyncio
async def test_run_stops_at_max_steps(monkeypatch):
    call = agent_llm.ToolCall(id="t", name="fr_search", arguments={})
    monkeypatch.setattr(agent_llm, "complete", lambda **kw: _turn(calls=[call]))
    monkeypatch.setattr(agent_runtime, "tool_schemas", lambda names: _async([]))

    async def fake_exec(spec, c):
        return ("ok", False)
    monkeypatch.setattr(agent_runtime, "execute_tool", fake_exec)

    spec = agent_runtime.AgentSpec(system="s", tools=frozenset({"fr_search"}), max_steps=2)
    res = await agent_runtime.run(spec, "boucle")
    assert res.stopped == "max_steps"
    # Le plafond est un plafond : jamais plus de tours d'outils que demandé (+1 tour
    # de conclusion tenté), sinon un endpoint public boucle à nos frais.
    assert len(res.steps) <= 3


@pytest.mark.asyncio
async def test_run_surfaces_refusal_without_reading_content(monkeypatch):
    monkeypatch.setattr(agent_llm, "complete", lambda **kw: _turn(stop="refusal"))
    monkeypatch.setattr(agent_runtime, "tool_schemas", lambda names: _async([]))
    spec = agent_runtime.AgentSpec(system="s", tools=frozenset())
    res = await agent_runtime.run(spec, "…")
    assert res.stopped == "refusal"
    assert res.reply == ""


def _async(value):
    async def _coro():
        return value
    return _coro()
