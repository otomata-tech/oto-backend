"""The loop's answers: a card's answer must be for THE call that waits, of the kind that
waits; "don't ask again" only covers plain writes; a destructive call asks even in Act
mode; reads run; an interrupted turn is settled by appending, never by editing."""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp.capabilities.copilot import boucle, outils
from oto_mcp.capabilities.copilot.outils import Boite, Issue


def _fil(**k):
    base = {"id": "thr_t", "sub": "u-a", "org_id": 1, "project_id": None, "run_id": "run-1",
            "messages": [], "queue": [], "results": [], "pending": None, "remembered": [],
            "client_tools": [], "mode": "ask", "model_calls": 0}
    base.update(k)
    return base


def _boite(*noms):
    noms = set(noms) | {"oto_call"}
    return Boite(frozenset(noms), {n: object() for n in noms},
                 [{"name": n} for n in sorted(noms)])


def _attente(bloc, sorte="write"):
    return _fil(queue=[bloc], pending={"kind": "approval", "block": bloc, "sorte": sorte})


BLOC = {"id": "tu_1", "name": "data_write", "input": {"datastore": 3, "row": {"a": "x"}}}


def test_reponse_a_une_autre_carte_refusee():
    fil = _attente(dict(BLOC))
    assert boucle.repondre_carte(fil, "tu_autre", True, None, False)
    assert fil["queue"] and fil["pending"]


def test_reponse_de_page_a_une_carte_refusee():
    fil = _attente(dict(BLOC))
    assert boucle.repondre_page(fil, "tu_1", True, {}, None, None)
    assert fil["pending"]["kind"] == "approval"


def test_refus_repond_au_modele_et_vide_la_file():
    fil = _attente(dict(BLOC))
    assert boucle.repondre_carte(fil, "tu_1", False, None, False) is None
    assert fil["queue"] == [] and fil["results"][0]["is_error"]
    assert fil["results"][0]["tool_use_id"] == "tu_1"


def test_retenir_seulement_une_ecriture_simple():
    fil = _attente(dict(BLOC), sorte="write")
    boucle.repondre_carte(fil, "tu_1", True, None, True)
    assert fil["remembered"] == ["data_write:"]
    assert fil["queue"][0]["approved"]
    fil = _attente({"id": "tu_2", "name": "data_delete_row", "input": {"datastore": 3}},
                   sorte="destructive")
    boucle.repondre_carte(fil, "tu_2", True, None, True)
    assert fil["remembered"] == []


def test_modifications_appliquees_et_dites():
    bloc = {"id": "tu_3", "name": "oto_trigger",
            "input": {"op": "update", "trigger_id": 4, "cron": "0 9 * * *"}}
    fil = _attente(bloc, sorte="destructive")
    boucle.repondre_carte(fil, "tu_3", True, {"cron": "0 7 * * *", "trigger_id": "1"}, False)
    q = fil["queue"][0]
    assert q["input"]["cron"] == "0 7 * * *" and q["input"]["trigger_id"] == 4
    assert q["_edits"] == ["cron"]


def test_resultat_de_page_rafraichit_les_actions():
    bloc = {"id": "tu_4", "name": "navigate", "input": {"to": "x"}}
    fil = _fil(queue=[bloc], pending={"kind": "client", "block": bloc},
               client_tools=[{"name": "navigate"}])
    neuves = [{"name": "navigate"}, {"name": "filter_table"}]
    assert boucle.repondre_page(fil, "tu_4", True, {"ok": 1}, None, neuves) is None
    assert fil["client_tools"] == neuves
    assert "filter_table" in fil["results"][0]["content"]


def test_interrompu_ajoute_sans_modifier():
    assistant = {"role": "assistant", "content": [
        {"type": "text", "text": "x"},
        {"type": "tool_use", "id": "a", "name": "data_rows", "input": {}},
        {"type": "tool_use", "id": "b", "name": "data_rows", "input": {}}]}
    fil = _fil(messages=[{"role": "user", "content": "q"}, assistant],
               results=[{"type": "tool_result", "tool_use_id": "a", "content": "ok"}],
               queue=[{"id": "b"}], pending={"kind": "approval"})
    boucle.regler_interrompus(fil, "stop")
    assert fil["messages"][1] is assistant
    dernier = fil["messages"][-1]
    assert [r["tool_use_id"] for r in dernier["content"]] == ["a", "b"]
    assert fil["queue"] == [] and fil["pending"] is None


def _drainer(fil, b, monkeypatch, appels):
    async def faux_executer(_b, sub, name, args, **k):
        appels.append((name, args, k))
        return Issue(True, '{"rows": []}', {"rows": []})

    async def faux_sauver(_fil):
        return None
    monkeypatch.setattr(boucle, "executer", faux_executer)
    monkeypatch.setattr(boucle, "sauver", faux_sauver)

    async def go():
        etat: dict = {}
        evs = [e async for e in boucle.drainer(fil, b, etat)]
        return evs, etat
    return asyncio.run(go())


def test_lecture_passe_ecriture_demande(monkeypatch):
    appels: list = []
    fil = _fil(queue=[
        {"id": "r", "name": "data_rows", "input": {"datastore": 1}},
        {"id": "w", "name": "data_write", "input": {"datastore": 1, "row": {"a": "b"}}}])
    evs, etat = _drainer(fil, _boite("data_rows", "data_write"), monkeypatch, appels)
    assert [e["type"] for e in evs] == ["tool_call", "tool_result", "approval_required"]
    assert etat["pause"] and appels[0][0] == "data_rows"
    carte_ = evs[-1]
    assert carte_["rememberable"] is True and carte_["id"] == "w"


def test_la_carte_dit_quand_rien_ne_se_defait(monkeypatch):
    from oto_mcp.capabilities.copilot import annulation
    fil = _fil(queue=[{"id": "w", "name": "data_write",
                       "input": {"datastore": 1, "row": {"a": "b"}}}])
    evs, _ = _drainer(fil, _boite("data_write"), monkeypatch, [])
    assert annulation.SANS_ANNULATION not in evs[-1]["title"]
    fil = _fil(queue=[{"id": "d", "name": "oto_doc",
                       "input": {"op": "create", "title": "Note"}}])
    evs, _ = _drainer(fil, _boite("oto_doc"), monkeypatch, [])
    assert evs[-1]["type"] == "approval_required"
    assert evs[-1]["title"].endswith(annulation.SANS_ANNULATION)


def test_annulation_promise_mais_non_preparee_le_dit(monkeypatch):
    from oto_mcp.capabilities.copilot import annulation

    def casse(*a, **k):
        raise RuntimeError("journal")
    monkeypatch.setattr(annulation, "preparer", casse)
    bloc = {"id": "w", "name": "data_write", "input": {"datastore": 1, "row": {"a": "b"}},
            "approved": True}
    evs, _ = _drainer(_fil(queue=[bloc]), _boite("data_write"), monkeypatch, [])
    res = evs[-1]
    assert res["type"] == "tool_result" and res["ok"] and "revert_id" not in res
    assert res["summary"].endswith("no undo for this change")


def test_ecriture_retenue_passe_destructif_jamais(monkeypatch):
    appels: list = []
    fil = _fil(mode="act", remembered=["data_delete_row:"], queue=[
        {"id": "w", "name": "data_write", "input": {"datastore": 1, "row": {"a": "b"}}},
        {"id": "d", "name": "data_delete_row", "input": {"datastore": 1, "id": "r"}}])
    evs, etat = _drainer(fil, _boite("data_write", "data_delete_row"), monkeypatch, appels)
    assert [a[0] for a in appels] == ["data_write"]
    assert evs[-1]["type"] == "approval_required" and evs[-1]["rememberable"] is False


def test_oto_call_imbrique_et_hors_boite_refuses(monkeypatch):
    appels: list = []
    fil = _fil(queue=[
        {"id": "n", "name": "oto_call", "input": {"name": "oto_call", "arguments": {}}},
        {"id": "h", "name": "oto_call", "input": {"name": "hidden_tool", "arguments": {}}},
        {"id": "a", "name": "oto_call", "input": {"name": "oto_admin_org",
                                                  "arguments": {"op": "list"}}},
        {"id": "x", "name": "data_rows", "input": {}}])
    evs, etat = _drainer(fil, _boite(), monkeypatch, appels)
    assert appels == [] and not etat["pause"]
    assert [r["is_error"] for r in fil["results"]] == [True, True, True, True]


def test_action_de_page_met_en_pause(monkeypatch):
    fil = _fil(client_tools=[{"name": "navigate"}],
               queue=[{"id": "c", "name": "navigate", "input": {"to": "/x"}}])
    evs, etat = _drainer(fil, _boite(), monkeypatch, [])
    assert evs == [{"type": "client_tool_call", "id": "c", "name": "navigate",
                    "args": {"to": "/x"}}]
    assert fil["pending"]["kind"] == "client"


def test_schema_offert_sans_axes():
    s = outils._schema_sans_axes({"type": "object", "properties": {
        "q": {"type": "string"}, "_org": {}, "_run_id": {}, "_project": {}, "_group": {},
        "_account": {}}, "required": ["q", "_org"]})
    assert set(s["properties"]) == {"q", "_account"} and s["required"] == ["q"]


@pytest.fixture(autouse=True)
def _pas_de_base(monkeypatch):
    """These tests never reach the base: preparing an undo is stubbed."""
    from oto_mcp.capabilities.copilot import annulation
    monkeypatch.setattr(annulation, "preparer", lambda *a, **k: None)


# ── Une sortie coupée à max_tokens ──────────────────────────────────────────

def _conduire(monkeypatch, messages):
    """Runs one leg against scripted model turns; returns (events, conversation)."""
    from oto_mcp.capabilities.copilot import cle as cles

    tours = list(messages)

    async def faux_appel(fil, b, cle, sortie):
        sortie["message"] = tours.pop(0)
        return
        yield  # an async generator

    monkeypatch.setattr(boucle, "_un_appel", faux_appel)
    monkeypatch.setattr(boucle.store, "prolonger", lambda *a, **k: None)
    monkeypatch.setattr(boucle.store, "enregistrer", lambda *a, **k: None)
    fil = _fil(_verrou="v")

    async def go():
        return [e async for e in boucle.conduire(fil, _boite("oto_apps"), cles.Cle("sk", None, None))]

    return asyncio.run(go()), fil


COUPE_APPEL = {"stop_reason": "max_tokens", "content": [
    {"type": "tool_use", "id": "tu_c", "name": "oto_apps", "input": {}}]}


def test_appel_coupe_dit_au_modele_et_reessaie_une_fois(monkeypatch):
    fin = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Done."}]}
    events, fil = _conduire(monkeypatch, [COUPE_APPEL, fin])
    assert [e["type"] for e in events] == ["done"]
    resultat = fil["messages"][1]["content"][0]
    assert resultat["tool_use_id"] == "tu_c" and resultat["is_error"]
    assert "op=patch" in resultat["content"]


def test_coupe_deux_fois_le_dit_a_la_personne(monkeypatch):
    events, fil = _conduire(monkeypatch, [COUPE_APPEL, dict(COUPE_APPEL, content=[
        {"type": "tool_use", "id": "tu_d", "name": "oto_apps", "input": {}}])])
    assert [e["type"] for e in events] == ["error", "done"]
    assert events[0]["message"] == boucle.COUPE_ECHEC
    # The history stays sendable: the second cut call is answered too.
    assert fil["messages"][-1]["content"][0]["tool_use_id"] == "tu_d"


def test_reponse_coupee_le_dit_sans_reessayer(monkeypatch):
    texte = {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "Here is a lo"}]}
    events, _ = _conduire(monkeypatch, [texte])
    assert [e["type"] for e in events] == ["error", "done"]
