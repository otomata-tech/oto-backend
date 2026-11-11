"""QUI A TROUVÉ chaque valeur : `outil` et `fournisseur` sur le journal des révisions,
`data_write(via=…)`, et l'agrégat par colonne servi à l'en-tête
(`GET …/column-sources`).

Tout se juge sur ce que porte la BASE, après être passé par le vrai chemin :

1. **l'outil de l'appel** — un appel MCP estampille son outil (`oto_call` déplié) ;
2. **le fournisseur** — `geste.par_fournisseur` le pose sur la seule écriture qu'il
   entoure ;
3. **`via`** — accepté seulement si l'outil a RÉUSSI dans le run de l'appel ; refusé
   sans run, hors run, ou sur un outil de la plateforme ;
4. **l'agrégat** — la valeur ACTUELLE de chaque cellule tenue, comptée par ce qui l'a
   écrite ; une ligne supprimée et une valeur effacée ne comptent pas ;
5. **la face REST** — la forme servie, et le 404 d'un tableau hors périmètre.
"""
from __future__ import annotations

import uuid

import pytest

SUB = "sub-sources-colonnes"
AUTRE = "sub-sources-colonnes-autre"


def _sql(requete: str, *params):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        cur = conn.execute(requete, params or None)
        return cur.fetchall() if cur.description else None


@pytest.fixture(scope="module")
def compte(live):
    from oto_mcp import db
    for s in (SUB, AUTRE):
        db.upsert_user(s, email=f"{s}@sources.invalid", name=s)
    return SUB


def _table(proprietaire: str = SUB) -> tuple[str, int]:
    from oto_mcp import db
    ns = f"sources-{uuid.uuid4().hex[:6]}"
    ns_id = db.create_datastore("user", proprietaire, ns)
    db.set_datastore_schema(ns_id, {"fields": [
        {"key": "nom", "type": "text"}, {"key": "email", "type": "text"}]})
    return ns, ns_id


def _revisions(ns_id: int) -> list[dict]:
    return _sql("SELECT row_id, source, outil, fournisseur, diff "
                "FROM datastore_row_revisions WHERE ns_id = %s ORDER BY id", ns_id)


def _maj(ns_id: int, row_id: str, patch: dict) -> None:
    from oto_mcp import db
    db.datastore_merge_row_locked(ns_id, row_id, lambda d: {**d, **patch},
                                  "2026-11-11T10:00:00")


def _run() -> str:
    from oto_mcp import db
    run_id = uuid.uuid4().hex
    db.insert_run(run_id, sub=SUB, org_id=None, label="t-sources")
    return run_id


def _appel_journalise(run_id: str, tool: str, *, ok: bool = True, args=None) -> None:
    import json
    _sql("INSERT INTO tool_calls (kind, sub, tool, args, ok, run_id) "
         "VALUES ('mcp', %s, %s, %s::jsonb, %s, %s)",
         SUB, tool, json.dumps(args or {}), ok, run_id)


# ── 1. l'outil de l'appel ──────────────────────────────────────────────────────

def test_oto_call_est_deplie_vers_sa_cible():
    from oto_mcp.calllog import _outil_ecrivant
    assert _outil_ecrivant("jev_rows", {}) == "jev_rows"
    assert _outil_ecrivant("oto_call", {"name": "waterfall_email_act",
                                        "arguments": {}}) == "waterfall_email_act"
    assert _outil_ecrivant("oto_call", {"name": "  "}) == "oto_call"
    assert _outil_ecrivant("oto_call", None) == "oto_call"


@pytest.fixture
def mcp(compte, monkeypatch):
    """`data_write` derrière la chaîne servie : contexte d'appel, puis journal."""
    from fastmcp import FastMCP

    from oto_mcp import call_axes
    from oto_mcp.calllog import ToolCallLogger
    from oto_mcp.datastore.core import make_store
    from oto_mcp.middleware.call_context import CallContextMiddleware
    from oto_mcp.tools import datastore as T
    from oto_mcp.tools import register_all

    monkeypatch.setattr(T, "_acting_store", lambda: make_store(SUB))
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    monkeypatch.setattr(T, "_project_hint", lambda ns: None)
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: SUB)

    async def sink(row):
        return None

    async def identite():
        return {"sub": SUB}

    m = FastMCP("t-sources")
    register_all(m)
    m.add_middleware(CallContextMiddleware(frozenset()))
    m.add_middleware(ToolCallLogger(sink, server="t", identity=identite))
    return m


async def _ecrire(m, arguments: dict, *, refus: bool = False):
    import asyncio

    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from oto_mcp import calllog
    async with Client(m) as c:
        if refus:
            with pytest.raises(ToolError) as e:
                await c.call_tool("data_write", arguments)
            return str(e.value)
        await c.call_tool("data_write", arguments)
    while calllog._PENDING:
        await asyncio.gather(*list(calllog._PENDING), return_exceptions=True)
    return None


@pytest.mark.asyncio
async def test_un_appel_mcp_estampille_son_outil(mcp):
    ns, ns_id = _table()
    await _ecrire(mcp, {"datastore": ns, "row": {"nom": "Ada"}})
    (r,) = _revisions(ns_id)
    assert (r["source"], r["outil"], r["fournisseur"]) == ("agent", "data_write", None)


# ── 2. le fournisseur ──────────────────────────────────────────────────────────

def test_le_fournisseur_ne_vaut_que_pour_l_ecriture_qu_il_entoure(compte):
    from oto_mcp import db, geste
    _, ns_id = _table()
    with geste.portee(geste.AGENT, SUB, outil="waterfall_email_act"):
        with geste.par_fournisseur("Hunter"):
            db.datastore_insert_row(ns_id, "r1", {"email": "a@x.fr"})
        db.datastore_insert_row(ns_id, "r2", {"email": "b@x.fr"})
    a, b = _revisions(ns_id)
    assert (a["outil"], a["fournisseur"]) == ("waterfall_email_act", "hunter")
    assert (b["outil"], b["fournisseur"]) == ("waterfall_email_act", None)


def test_sans_geste_ni_l_outil_ni_le_fournisseur_ne_s_inventent(compte):
    from oto_mcp import db
    _, ns_id = _table()
    db.datastore_insert_row(ns_id, "r1", {"nom": "x"})
    (r,) = _revisions(ns_id)
    assert (r["source"], r["outil"], r["fournisseur"]) == ("system", None, None)


# ── 3. `via` ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_via_prouve_dans_le_run_nomme_l_outil(mcp):
    ns, ns_id = _table()
    run = _run()
    _appel_journalise(run, "apollo_match_person")
    _appel_journalise(run, "oto_call", args={"name": "hunter_find_email"})
    await _ecrire(mcp, {"datastore": ns, "row": {"email": "a@x.fr"},
                        "via": "apollo_match_person", "_run_id": run})
    await _ecrire(mcp, {"datastore": ns, "row": {"email": "b@x.fr"},
                        "via": "hunter_find_email", "_run_id": run})
    assert [(r["source"], r["outil"]) for r in _revisions(ns_id)] == [
        ("agent", "apollo_match_person"), ("agent", "hunter_find_email")]


@pytest.mark.asyncio
async def test_via_non_prouve_ecrit_quand_meme_sans_attribution(mcp):
    """Jamais bloquant : la donnée ne paie pas pour son étiquette."""
    import json

    from fastmcp import Client
    ns, ns_id = _table()
    run = _run()
    _appel_journalise(run, "apollo_match_person", ok=False)
    cas = [({"via": "apollo_match_person", "_run_id": run}, "no successful"),
           ({"via": "apollo_match_person"}, "needs a run"),
           ({"via": "data_rows", "_run_id": run}, "platform tool")]
    async with Client(mcp) as c:
        for i, (extra, attendu) in enumerate(cas):
            r = await c.call_tool("data_write", {"datastore": ns,
                                                 "row": {"email": f"{i}@x.fr"}, **extra})
            corps = r.structured_content or json.loads(r.content[0].text)
            assert attendu in corps["via_dropped"], corps
    revs = _revisions(ns_id)
    assert len(revs) == 3, "les trois écritures ont eu lieu"
    assert {r["outil"] for r in revs} == {"data_write"}, "aucune n'est attribuée"
    assert _cases(ns_id) == []


def test_via_est_decrit_dans_la_description_servie():
    from oto_mcp.datastore import via
    from oto_mcp.tools import datastore as T
    assert "<<via>>" not in (via.sous_via(lambda **k: None).__doc__ or "")
    import inspect
    src = inspect.getsource(T)
    assert "@ds_via.sous_via" in src and "via: <<via>>" in src


# ── 4. la table des cases attribuées, et l'agrégat ────────────────────────────

def _cases(ns_id: int) -> list[tuple]:
    return [(r["row_id"], r["champ"], r["outil"], r["fournisseur"]) for r in _sql(
        "SELECT row_id, champ, outil, fournisseur FROM datastore_cell_sources "
        "WHERE ns_id = %s ORDER BY row_id, champ", ns_id)]


def _waterfall(ns_id, rid, fournisseur, patch):
    from oto_mcp import geste
    with geste.portee(geste.AGENT, SUB, outil="waterfall_email_act"):
        with geste.par_fournisseur(fournisseur):
            _maj(ns_id, rid, patch)


def test_une_case_trouvee_est_attribuee_et_la_suite_la_deplace_ou_la_retire(compte):
    from oto_mcp import db, geste
    _, ns_id = _table()
    with geste.portee(geste.CONSOLE, SUB):
        for rid in ("r1", "r2", "r3", "r4"):
            db.datastore_insert_row(ns_id, rid, {"nom": rid})
    assert _cases(ns_id) == [], "écrit à la main : rien d'attribué"
    for rid, f in (("r1", "hunter_email_finder"), ("r2", "hunter_email_finder"),
                   ("r3", "fullenrich_enrich_linkedin"), ("r4", "hunter_email_finder")):
        _waterfall(ns_id, rid, f, {"email": f"{rid}@x.fr"})
    # Un COMMENTAIRE seul ne change pas la valeur : l'attribution reste.
    with geste.portee(geste.CONSOLE, SUB):
        _maj(ns_id, "r1", {"email": {"valeur": "r1@x.fr", "comment": "vérifié"}})
    # La valeur réécrite à la main : ce n'est plus Hunter qui la tient.
    with geste.portee(geste.CONSOLE, SUB):
        _maj(ns_id, "r2", {"email": "fixe@x.fr"})
    # Un agent qui écrit sans nommer l'outil : retirée aussi.
    with geste.portee(geste.AGENT, SUB, outil="data_write"):
        _maj(ns_id, "r3", {"email": "autre@x.fr"})
    # Ligne supprimée : ses cases partent avec elle.
    db.datastore_delete_row(ns_id, "r4")
    assert _cases(ns_id) == [("r1", "email", "waterfall_email_act", "hunter_email_finder")]


def test_effacer_la_valeur_retire_l_attribution(compte):
    _, ns_id = _table()
    from oto_mcp import db
    db.datastore_insert_row(ns_id, "r1", {"nom": "x"})
    _waterfall(ns_id, "r1", "hunter_email_finder", {"email": "a@x.fr"})
    _maj(ns_id, "r1", {"email": None})
    assert _cases(ns_id) == []


def test_l_attribution_survit_a_la_purge_du_journal(compte):
    """Le défaut que la table corrige : la purge garde l'import, pas l'enrichissement."""
    from oto_mcp import db, geste
    from oto_mcp.db import historique
    _, ns_id = _table()
    with geste.portee(geste.IMPORT, SUB):
        db.datastore_insert_row(ns_id, "r1", {"nom": "x", "email": "vieux@x.fr"})
    _waterfall(ns_id, "r1", "hunter_email_finder", {"email": "neuf@x.fr"})
    _sql("DELETE FROM datastore_row_revisions WHERE ns_id = %s AND source <> 'import'",
         ns_id)
    out = historique.sources_des_colonnes(ns_id)
    assert out["colonnes"]["email"] == [{"source": "agent", "outil": "waterfall_email_act",
                                         "fournisseur": "hunter_email_finder",
                                         "cellules": 1}]


def test_l_agregat_rend_les_parts_et_les_cases_tenues(compte):
    from oto_mcp import db
    from oto_mcp.db import historique
    _, ns_id = _table()
    for i in range(4):
        db.datastore_insert_row(ns_id, f"r{i}", {"nom": str(i)})
    _waterfall(ns_id, "r0", "hunter_email_finder", {"email": "a@x.fr"})
    _waterfall(ns_id, "r1", "hunter_email_finder", {"email": "b@x.fr"})
    _waterfall(ns_id, "r2", "fullenrich_enrich_linkedin", {"email": "c@x.fr"})
    _maj(ns_id, "r3", {"email": "d@x.fr"})
    out = historique.sources_des_colonnes(ns_id)
    assert [(g["fournisseur"], g["cellules"]) for g in out["colonnes"]["email"]] == [
        ("hunter_email_finder", 2), ("fullenrich_enrich_linkedin", 1)]
    assert "nom" not in out["colonnes"]
    assert out["tenues"] == {"email": 4}


def test_un_tableau_sans_attribution_ne_lit_pas_ses_lignes(compte, monkeypatch):
    from oto_mcp import db
    from oto_mcp.db import historique
    _, ns_id = _table()
    db.datastore_insert_row(ns_id, "r1", {"nom": "x"})
    assert historique.sources_des_colonnes(ns_id) == {"colonnes": {}, "tenues": None}


# ── 5. la face REST ────────────────────────────────────────────────────────────

class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@sources.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


@pytest.fixture(scope="module")
def rest(compte):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oto_mcp.api import routes as api_routes
    app = Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None))
    return TestClient(api_routes.RestCallLogger(app))


def test_face_rest_forme_servie_et_hors_perimetre(rest):
    from oto_mcp import db, geste
    ns, ns_id = _table()
    with geste.portee(geste.AGENT, SUB, outil="jev_rows"):
        db.datastore_insert_row(ns_id, "r1", {"nom": "x"})
    r = rest.get(f"/api/datastores/{ns_id}/column-sources",
                 headers={"Authorization": f"Bearer {SUB}"})
    assert r.status_code == 200, r.text
    corps = r.json()
    assert corps["columns"] == {"nom": [
        {"source": "agent", "tool": "jev_rows", "provider": None, "cells": 1}]}
    assert corps["held"] == {"nom": 1} and corps["since"]
    _, autre = _table(AUTRE)
    r = rest.get(f"/api/datastores/{autre}/column-sources",
                 headers={"Authorization": f"Bearer {SUB}"})
    assert r.status_code == 404, r.text


# ── 6. Jev, l'interrupteur du journal, la migration ───────────────────────────

def test_jev_attribue_ses_verdicts_par_le_vrai_chemin_d_ecriture(compte):
    """Le chemin de `jev.qualifier` : `pr.ecrire_ligne` sur le fil appelant, sous le geste
    que pose la tâche de fond (`portee(…, outil="jev_rows")`) ou l'appel MCP."""
    from oto_mcp import db, geste
    from oto_mcp.datastore import par_reference as pr
    from oto_mcp.datastore.core import make_store
    ns, ns_id = _table()
    db.set_datastore_schema(ns_id, {"fields": [{"key": "nom", "type": "text"},
                                                {"key": "email", "type": "text"},
                                                {"key": "fit", "type": "text"}]})
    db.datastore_insert_row(ns_id, "r1", {"nom": "Acme"})
    store = make_store(SUB)
    with geste.portee(geste.AGENT, SUB, outil="jev_rows"):
        code = pr.ecrire_ligne(store, ns, "r1", {"fit": "yes"})
    assert code is None
    assert _cases(ns_id) == [("r1", "fit", "jev_rows", None)]


def test_la_tache_de_fond_jev_ecrit_sous_jev_rows():
    import inspect

    from oto_mcp import jev_jobs_worker
    assert 'outil="jev_rows"' in inspect.getsource(jev_jobs_worker._tranche)


def test_journal_coupe_une_reecriture_retire_quand_meme_l_attribution(compte):
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    _, ns_id = _table()
    db.datastore_insert_row(ns_id, "r1", {"nom": "x"})
    _waterfall(ns_id, "r1", "hunter_email_finder", {"email": "a@x.fr"})
    with _connect() as conn:
        with conn.transaction():
            conn.execute("SET LOCAL oto.journal_revisions = 'off'")
            conn.execute("UPDATE datastore_rows SET data = data || '{\"email\": \"b@x.fr\"}' "
                         "WHERE ns_id = %s AND row_id = 'r1'", (ns_id,))
    assert _cases(ns_id) == []


def test_la_migration_pose_ses_declencheurs_sous_garde_sans_drop(compte):
    from oto_mcp.db import attribution_cellules as ac
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        for nom, quand, condition in ac._DECLENCHEURS:
            ddl = ac.ddl_declencheur_si_absent(nom, quand, condition)
            assert "DROP" not in ddl
            conn.execute(ddl)          # déjà posé : rien
        n = conn.execute("SELECT count(*) AS n FROM pg_trigger WHERE tgrelid = "
                         "'datastore_rows'::regclass AND tgname LIKE "
                         "'datastore_rows_40_attribution_%%'").fetchone()["n"]
    assert n == 3
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / \
        "oto_mcp/db/migrations/versions/20261111_1100_cell_sources.py"
    assert "DROP TRIGGER" not in src.read_text().split("def downgrade")[0]
