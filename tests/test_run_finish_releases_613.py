"""#613 — fermer le run (`run_finish`) libère ce que le run tenait, et la description
de `data_claim_next` ne prescrit plus un outil que l'appelant n'a peut-être pas.

Mesuré le 29/08/2026 sur une campagne où `data_release` était filtré à l'inclusion :
la description disait « then RELEASE the row … Release with data_release », relue à
chaque appel par des agents qui ne pouvaient pas l'exécuter. Ils ont écrit leur
intention DANS la fiche — des colonnes fabriquées (`_liberation: "run_finish"`,
`_action: "release"`) dans des données clientes, exportées.

Avant d'écrire « finishing your run releases it » dans une description servie, on le
PROUVE sur le chemin réel : la réservation posée par `data_claim_next` tel qu'il est
monté (middleware + outil enregistré, `_run_id=` lu des arguments bruts), puis
`run_finish` tel que `register_all` le monte — contre un vrai PostgreSQL. Le niveau
base seul (`datastore_release_by_run`, `test_row_lock_native.py`) était couvert ; ce
qui manquait est le lien entre le VERBE SERVI et la libération.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

SUB = "sub-flotte-613"


@pytest.fixture
def surface(live, monkeypatch):
    """Les outils `data_*` tels que le serveur les monte, l'acteur tenu."""
    from oto_mcp.datastore.core import make_store
    from oto_mcp.tools import datastore as T
    monkeypatch.setattr(T, "_acting_store", lambda: make_store(SUB))
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    monkeypatch.setattr(T, "_project_hint", lambda ns: None)


_OUTILS: dict = {}


def _outil(nom: str):
    """Ce que charge le BOOT (`register_all`), pas un module seul."""
    if not _OUTILS:
        from fastmcp import FastMCP

        from oto_mcp.tools import register_all
        m = FastMCP("t-613")
        register_all(m)
        for n in ("data_claim_next", "run_finish"):
            _OUTILS[n] = asyncio.run(m.get_tool(n))
    return _OUTILS[nom]


def _run() -> str:
    """Un jeton PROPRE au test : les baux d'un run se lisent sur toute la base."""
    return uuid.uuid4().hex


def _table(n: int):
    from oto_mcp import db
    ns_id = db.create_datastore("user", SUB, "file-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)

    for i in range(n):
        db.datastore_insert_row(ns_id, f"r{i}", {"siren": f"5511100{i}", "statut": "a_enrichir"})
    return ns, ns_id


def _claim(ns: str, run: str) -> dict:
    """L'appel comme il arrive en production : `_run_id=` lu des arguments BRUTS par
    le middleware, posé, retiré, puis l'outil dispatché."""
    from oto_mcp.middleware.call_context import CallContextMiddleware

    outil = _outil("data_claim_next")

    class _Msg:
        pass

    class _Ctx:                     # comme le vrai MiddlewareContext : PAS de get_state
        pass

    msg = _Msg()
    msg.name = "data_claim_next"
    msg.arguments = {"datastore": ns, "worker": run, "filter": {"statut": "a_enrichir"},
                     "lease_s": 600, "_run_id": run}
    ctx = _Ctx()
    ctx.message = msg

    async def _next(c):
        return await outil.run(c.message.arguments)

    async def _go():
        return await CallContextMiddleware(frozenset()).on_call_tool(ctx, _next)

    return asyncio.run(_go()).structured_content


class _SessionCtx:
    """La pile de runs de la session, telle que `run_finish` la lit (`pop_run`)."""

    def __init__(self):
        self._state: dict = {}

    async def get_state(self, key):
        return self._state.get(key)

    async def set_state(self, key, value):
        self._state[key] = value


def _finish(run: str, outcome: str) -> dict:
    fn = _outil("run_finish").fn
    # `partial` exige sa note (ce qui est fait, ce qui reste) — oto#91.
    note = "une ligne faite, le reste à reprendre" if outcome == "partial" else None
    return asyncio.run(fn(_SessionCtx(), run_id=run, outcome=outcome, note=note))


def _bail(ns_id, row_id) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return dict(conn.execute(
            "SELECT claimed_by, claimed_until, claimed_run FROM datastore_rows "
            "WHERE ns_id = %s AND row_id = %s", (ns_id, row_id)).fetchone() or {})


# ── ① la preuve : fermer le run rend la ligne, quelle que soit l'issue ──────────

@pytest.mark.parametrize("outcome", ["done", "failed", "blocked", "partial"])
def test_fermer_le_run_libere_la_ligne_qu_il_tenait(surface, outcome):
    """Claim sous `_run_id=R` par le chemin réel, puis `run_finish(R)` : la ligne
    revient libre, la réponse le dit, et un AUTRE run peut la reprendre."""
    ns, ns_id = _table(1)
    run_r = _run()

    ligne = _claim(ns, run_r)["row"]
    assert ligne, "une ligne était libre : le claim la rend"
    assert _bail(ns_id, ligne["_id"])["claimed_run"] == run_r

    out = _finish(run_r, outcome)
    assert out["ok"] and out["outcome"] == outcome
    assert out.get("rows_released") == 1, (
        "la libération doit être DITE à l'agent, pas seulement faite : "
        f"{out!r}")
    bail = _bail(ns_id, ligne["_id"])
    assert bail["claimed_by"] is None and bail["claimed_run"] is None, bail

    reprise = _claim(ns, _run())["row"]
    assert reprise and reprise["_id"] == ligne["_id"], "la ligne est de nouveau dans la file"


def test_fermer_un_autre_run_ne_libere_rien(surface):
    """La clause `claimed_run = run` : un run qui se ferme ne rend que ce qu'IL tenait."""
    ns, ns_id = _table(1)
    run_r, run_q = _run(), _run()

    ligne = _claim(ns, run_r)["row"]
    assert ligne

    out = _finish(run_q, "done")
    # #633 : le zéro est ÉCRIT — un poste de flotte distingue « zéro ligne rendue »
    # de « champ absent ».
    assert out["ok"] and "rows_released" in out and out["rows_released"] == 0, out
    assert _bail(ns_id, ligne["_id"])["claimed_run"] == run_r, "le bail de R est intact"


# ── ② la description dit quoi faire SANS `data_release`, et où ne PAS écrire ─────

def test_la_description_tient_compte_de_l_outil_absent():
    """Une prescription inconditionnelle d'un outil filtré à l'inclusion a produit des
    colonnes `_action`/`_liberation` dans des fiches clientes. La phrase servie donne
    l'alternative qui existe (`run_finish`) et nomme le geste interdit."""
    # Servie avec ses retours à la ligne : on compare la prose, pas sa mise en page.
    d = " ".join(_outil("data_claim_next").description.split())
    assert "Release with data_release" not in d, "la prescription inconditionnelle"
    assert "`data_release` if you have it" in d, d
    assert "run_finish" in d and "releases it" in d, d
    assert "Never write your intent into the row" in d, d
    assert "_action" in d and "_liberation" in d, (
        "les colonnes fabriquées sont nommées : c'est le geste vu, pas un geste supposé")


# ── la libération appartient au PROPRIÉTAIRE du run (comme la clôture, #108) ──

def _indexer(run: str, sub: str) -> None:
    from oto_mcp import db
    db.insert_run(run, sub=sub, org_id=None, label="t-613-proprietaire", guide=None)


@pytest.fixture
def appelant(monkeypatch):
    from oto_mcp.auth import hooks
    monkeypatch.setattr(hooks, "current_user_sub_from_token", lambda: SUB)


def test_fermer_le_run_d_un_autre_compte_ne_libere_pas_ses_lignes(surface, appelant):
    """Un run_id est une poignée, pas un droit : `run_finish` sur le run d'un AUTRE
    compte ne rend pas ses baux à la file — la ligne reste à son titulaire."""
    ns, ns_id = _table(1)
    run_autrui = _run()
    _indexer(run_autrui, "sub-autre-compte-613")
    ligne = _claim(ns, run_autrui)["row"]
    assert ligne

    out = _finish(run_autrui, "done")
    assert out["ok"] and out["rows_released"] == 0, out
    assert _bail(ns_id, ligne["_id"])["claimed_run"] == run_autrui


def test_fermer_son_propre_run_indexe_libere_toujours(surface, appelant):
    ns, ns_id = _table(1)
    run_r = _run()
    _indexer(run_r, SUB)
    ligne = _claim(ns, run_r)["row"]
    out = _finish(run_r, "done")
    assert out["rows_released"] == 1, out
    assert _bail(ns_id, ligne["_id"])["claimed_run"] is None
