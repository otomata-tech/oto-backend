"""Instructions injectées au `initialize` — artefact composé A/B/C (#50).

Bloc A (secret sauce, DB→seed), bloc B (onboarding + catalogue, gaté `onboarded`),
bloc C (contexte résolu + doctrine d'org avec variables). Style `asyncio.run` +
monkeypatch des seams, comme les autres tests du repo.
"""
import asyncio
import types

import oto_mcp.access as access
import oto_mcp.db as db
import oto_mcp.group_store as group_store
import oto_mcp.org_store as org_store
import oto_mcp.providers as providers
import oto_mcp.roles as roles
from oto_mcp import instructions as instr
from oto_mcp import middleware as mw


# ── blocs plateforme A/B (DB override → seed) ────────────────────────────────
def test_block_a_db_override(monkeypatch):
    monkeypatch.setattr(db, "get_platform_instruction",
                        lambda key: {"body_md": "POSTURE ÉDITÉE"})
    assert instr._block_a() == "POSTURE ÉDITÉE"


def test_block_a_seed_fallback(monkeypatch):
    monkeypatch.setattr(db, "get_platform_instruction", lambda key: None)
    out = instr._block_a()
    assert "TA boîte à outils" in out          # le seed constant
    assert "Encadre et remonte" in out


def test_block_a_fail_open_to_seed(monkeypatch):
    def boom(key):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "get_platform_instruction", boom)
    assert "TA boîte à outils" in instr._block_a()


def test_block_b_includes_catalog(monkeypatch):
    monkeypatch.setattr(db, "get_platform_instruction",
                        lambda key: {"body_md": "ONBOARDING PROSE"})
    monkeypatch.setattr(providers, "render_namespace_catalog", lambda: "• fr_* — entreprises")
    out = instr._block_b()
    assert "ONBOARDING PROSE" in out and "• fr_* — entreprises" in out


# ── substitution de variables (bloc C) ───────────────────────────────────────
def test_apply_vars():
    ctx = {"org_name": "Acme", "user_name": "Jean", "group_name": "Sales",
           "connectors": ["folk", "serpapi"]}
    body = "Org {{org}}, user {{user}}, équipe {{équipe}}, outils {{connecteurs_actifs}}. {{inconnu}}"
    out = instr._apply_vars(body, ctx)
    assert "Org Acme" in out and "user Jean" in out and "équipe Sales" in out
    assert "outils folk, serpapi" in out
    assert "{{inconnu}}" in out                # token inconnu laissé tel quel


def test_apply_vars_empty_dashes():
    ctx = {"org_name": "Acme", "user_name": "", "group_name": "", "connectors": []}
    out = instr._apply_vars("{{user}}/{{équipe}}/{{connecteurs_actifs}}", ctx)
    assert out == "—/—/—"


def test_format_context_optional_lines():
    ctx = {"org_name": "Acme", "role": "org_admin", "group_name": "",
           "connectors": [], "projects": ["Ferme solaire"],
           "runs": [{"label": "prospection", "doctrine": "scout", "outcome": "done"}]}
    out = instr._format_context(ctx)
    assert "## Ton contexte oto" in out
    assert "Organisation : Acme (ton rôle : org_admin)" in out
    assert "Équipe active" not in out          # vide → ligne omise
    assert "Connecteurs actifs" not in out
    assert "Projets récents : Ferme solaire" in out
    assert "Derniers déroulés : prospection [scout] → done" in out


# ── composition de session ───────────────────────────────────────────────────
def _wire_context(monkeypatch, *, doctrine_body="Doctrine de {{org}}."):
    monkeypatch.setattr(db, "get_platform_instruction", lambda key: None)  # seeds
    monkeypatch.setattr(providers, "render_namespace_catalog", lambda: "CATALOGUE")
    monkeypatch.setattr(org_store, "get_org", lambda oid: {"name": "Acme"})
    monkeypatch.setattr(db, "get_user", lambda sub: {"name": "Jean"})
    monkeypatch.setattr(roles, "effective_org_role", lambda sub, oid: "member")
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(group_store, "get_group", lambda gid: None)
    monkeypatch.setattr(access, "status_for",
                        lambda sub: {"providers": {"folk": {"mode": "user"},
                                                   "x": {"mode": "forbidden"}}})
    monkeypatch.setattr(db, "list_projects_for_owners", lambda owners: [{"id": 1, "name": "P1"}])
    monkeypatch.setattr(db, "recent_runs", lambda sub, oid, limit=5: [])
    monkeypatch.setattr(org_store, "get_instruction",
                        lambda oid, slug: {"body_md": doctrine_body})


def test_compose_session_full(monkeypatch):
    _wire_context(monkeypatch)
    out = instr.compose_session("u1", 7, onboarded=False)
    assert "TA boîte à outils" in out                       # bloc A
    assert "CATALOGUE" in out                               # bloc B (pas onboarded)
    assert "## Ton contexte oto" in out                     # bloc C contexte
    assert "Connecteurs actifs : folk" in out               # forbidden exclu
    assert "## Doctrine de ton organisation (Acme)" in out  # bloc C doctrine
    assert "Doctrine de Acme." in out                       # variable {{org}} substituée


def test_compose_session_onboarded_skips_b(monkeypatch):
    _wire_context(monkeypatch)
    out = instr.compose_session("u1", 7, onboarded=True)
    assert "CATALOGUE" not in out                           # bloc B omis
    assert "TA boîte à outils" in out                       # bloc A reste
    assert "## Ton contexte oto" in out                     # bloc C reste


def test_compose_session_no_org(monkeypatch):
    monkeypatch.setattr(db, "get_platform_instruction", lambda key: None)
    monkeypatch.setattr(providers, "render_namespace_catalog", lambda: "CATALOGUE")
    out = instr.compose_session("u1", None, onboarded=True)
    assert "TA boîte à outils" in out
    assert "## Ton contexte oto" not in out                 # pas d'org → pas de bloc C


def test_compose_session_doctrine_fail_open(monkeypatch):
    # Résolution du contexte qui casse → fallback doctrine seule, jamais d'exception.
    _wire_context(monkeypatch)
    def boom(sub):
        raise RuntimeError("status down")
    monkeypatch.setattr(access, "status_for", boom)
    out = instr.compose_session("u1", 7, onboarded=True)
    assert "## Doctrine de ton organisation (Acme)" in out  # doctrine encore servie


# ── middleware on_initialize ─────────────────────────────────────────────────
def _run(result, sub, monkeypatch):
    monkeypatch.setattr(mw, "current_user_sub_from_token", lambda: sub)

    async def call_next(ctx):
        return result
    return asyncio.run(mw.DynamicInstructionsMiddleware().on_initialize(object(), call_next))


def test_middleware_composes_session(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    monkeypatch.setattr(db, "get_account_profile", lambda sub: {"onboarded": False})
    monkeypatch.setattr(instr, "compose_session",
                        lambda sub, org, *, onboarded: f"[A/B/C org={org} onb={onboarded}]")
    res = types.SimpleNamespace(instructions="BASE")
    out = _run(res, "u1", monkeypatch)
    assert out.instructions == "[A/B/C org=7 onb=False]"


def test_middleware_noop_without_sub(monkeypatch):
    res = types.SimpleNamespace(instructions="BASE")
    out = _run(res, None, monkeypatch)
    assert out.instructions == "BASE"


def test_middleware_noop_when_no_instructions(monkeypatch):
    res = types.SimpleNamespace(instructions="")
    out = _run(res, "u1", monkeypatch)
    assert out.instructions == ""


def test_middleware_fail_open(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    def boom(sub):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "get_account_profile", boom)
    res = types.SimpleNamespace(instructions="BASE")
    out = _run(res, "u1", monkeypatch)
    assert out.instructions == "BASE"            # composition échoue → statique gardé


# ── index des skills (description dynamique de oto_get_doctrine) ──────────────
def test_skills_index_md(monkeypatch):
    monkeypatch.setattr(org_store, "list_instructions", lambda oid: [
        {"slug": "a", "title": "Skill A", "description": "fait A"},
        {"slug": "b", "title": "Skill B", "description": ""},
    ])
    out = instr.skills_index_md(7)
    assert out.startswith("Doctrines nommées")
    assert "- a — Skill A : fait A" in out
    assert "- b — Skill B" in out and "Skill B :" not in out


def test_skills_index_md_empty(monkeypatch):
    monkeypatch.setattr(org_store, "list_instructions", lambda oid: [])
    assert instr.skills_index_md(7) == ""
    assert instr.skills_index_md(None) == ""


class _FakeTool:
    def __init__(self, name, description=""):
        self.name, self.description = name, description

    def model_copy(self, update):
        return _FakeTool(self.name, update.get("description", self.description))


def _run_list(tools, sub, monkeypatch):
    monkeypatch.setattr(mw, "current_user_sub_from_token", lambda: sub)

    async def call_next(ctx):
        return tools
    return asyncio.run(mw.DynamicInstructionsMiddleware().on_list_tools(object(), call_next))


def test_on_list_tools_enriches_get_doctrine(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    monkeypatch.setattr(instr, "skills_index_md", lambda org: "INDEX-BLOCK")
    tools = [_FakeTool("fr_get", "search"), _FakeTool("oto_get_doctrine", "load doctrine")]
    out = {t.name: t for t in _run_list(tools, "u1", monkeypatch)}
    assert out["fr_get"].description == "search"
    assert "load doctrine" in out["oto_get_doctrine"].description
    assert "INDEX-BLOCK" in out["oto_get_doctrine"].description


def test_on_list_tools_noop_without_index(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    monkeypatch.setattr(instr, "skills_index_md", lambda org: "")
    tools = [_FakeTool("oto_get_doctrine", "load")]
    assert _run_list(tools, "u1", monkeypatch) is tools


def test_on_list_tools_noop_without_sub(monkeypatch):
    tools = [_FakeTool("oto_get_doctrine", "load")]
    assert _run_list(tools, None, monkeypatch) is tools
