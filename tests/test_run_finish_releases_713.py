"""#713 — reproduit le scénario EXACT du signal 414 (org 226, 13/08/2026) : `run_start`
pousse un run dans l'état de session, trois `data_claim_next` SANS `_run_id=` explicite —
le geste réel de l'agent, confirmé par le calllog du run `a186986859bf4b9c82e69e4e29ceee7e`
(`oto_admin_monitoring(op="run", ...)`) : les trois appels ne portent que `datastore`,
`worker`, `filter` — puis `run_finish`. Le signal : les 3 lignes restaient réservées
après la fermeture, mitigé par l'expiration du bail (900s).

Chronologie établie en investiguant #713 : cet incident (13/08 13:59-14:02) précède de
~2h17 le commit `a943fdf6` (« Le verrou reconnaît enfin son titulaire… », 13/08 16:19,
Refs #317) qui corrige exactement cette racine — `_current_run()` (seam SYNC) ne lisait
QUE l'axe explicite `_run_id=`, jamais la pile de session posée par `run_start`, alors que
le sink calllog (async) lisait déjà les deux sources. Le calllog du run montre aussi le
symptôme ② de ce même commit : les `data_write` en mode ligne unique y échouent avec
« Erreur interne du serveur » (RowLocked non traduite), sur les 3 lignes claimées sous ce
run — texto le défaut ② décrit dans `test_row_lock_prod_defects.py`.

Ce test comble un angle que ni `test_row_lock_prod_defects.py` (pose le run à la main via
`session_org.set_call_run` OU exerce le middleware seul, jamais le VERBE `run_finish`
bout en bout) ni `test_run_finish_releases_613.py` (`_run_id=` toujours PASSÉ explicitement)
ne couvrent : la combinaison réelle de l'incident — session STABLE (le calllog ne porte
qu'un seul `session_id` de bout en bout), `_run_id` JAMAIS passé, trois réservations, une
fermeture, sur les outils tels que `register_all` les monte.

⚠️ Le middleware reçoit un `MiddlewareContext`, qui n'a PAS `get_state` : l'état de
session est sur son `fastmcp_context`. Ce banc passait l'état de session comme contexte
du middleware lui-même — il est resté vert pendant que le repli était mort en
production (28/08 → 27/10/2026, #830). Le contexte a désormais la forme réelle ; le banc
sur un vrai serveur FastMCP est `test_run_de_session_830.py`.

Résultat sur le code actuel : PASSE. La garantie tient — à la condition que l'état de
session survive entre les appels, ce que #317 a rendu vrai pour ce seam. #547 (29/08)
rappelle que cette condition n'est PAS garantie en production sur claude.ai (renouvellement
de session par appel) : c'est pour ça que `_run_id=` reste OBLIGATOIRE à chaque appel dans
la description servie, même si ce test prouve que la pile de session est un filet qui
fonctionne quand la session, elle, tient.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

SUB = "sub-signal-414"


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
        m = FastMCP("t-713")
        register_all(m)
        for n in ("run_start", "data_claim_next", "run_finish"):
            _OUTILS[n] = asyncio.run(m.get_tool(n))
    return _OUTILS[nom]


def _table(n: int):
    from oto_mcp import db
    ns_id = db.create_datastore("user", SUB, "file-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)

    for i in range(n):
        db.datastore_insert_row(ns_id, f"r{i}", {"siren": f"85045{i:04d}", "statut": "a_faire"})
    return ns, ns_id


class _SessionStable:
    """L'état de session FastMCP TEL QU'IL TIENT dans l'incident : un seul `session_id`
    du premier `run_start` au dernier `run_finish` (confirmé par le calllog). C'est
    l'hypothèse la plus favorable à l'ancien code — et elle échouait quand même avant
    #317, parce que le seam SYNC ne consultait pas cet état."""

    def __init__(self):
        self._state: dict = {}

    async def get_state(self, key):
        return self._state.get(key)

    async def set_state(self, key, value):
        self._state[key] = value


def _claim_sans_run_id(ctx, ns: str, worker: str) -> dict:
    """`data_claim_next(worker=...)` — AUCUN `_run_id=`, exactement l'appel du calllog
    (`{"ns_id":204,"filter":..., "worker":"test-campagne-3lignes","datastore":"edition-vivier"}`).
    Passe par le VRAI middleware pour que le filet #317 (pile de session) ait sa chance."""
    from oto_mcp.middleware.call_context import CallContextMiddleware

    outil = _outil("data_claim_next")

    class _Msg:
        pass

    class _MiddlewareCtx:       # comme le vrai : PAS de get_state, la session est
        pass                    # sur `fastmcp_context` (#830)

    msg = _Msg()
    msg.name = "data_claim_next"
    msg.arguments = {"datastore": ns, "worker": worker}
    mctx = _MiddlewareCtx()
    mctx.message = msg
    mctx.fastmcp_context = ctx

    async def _next(c):
        return await outil.run(c.message.arguments)

    async def _go():
        return await CallContextMiddleware().on_call_tool(mctx, _next)

    return asyncio.run(_go()).structured_content


def _bail(ns_id, row_id) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return dict(conn.execute(
            "SELECT claimed_by, claimed_until, claimed_run FROM datastore_rows "
            "WHERE ns_id = %s AND row_id = %s", (ns_id, row_id)).fetchone() or {})


def test_le_scenario_du_signal_414_libere_les_3_lignes(surface):
    """`run_start` (pousse dans l'état de session) → 3× `data_claim_next` SANS `_run_id=`
    → `run_finish`. Sur le code actuel : les 3 lignes reviennent libres et `run_finish`
    le dit (`rows_released` == 3) — la panne du 13/08 (0 ligne libérée) ne reproduit plus."""
    ns, ns_id = _table(3)
    ctx = _SessionStable()

    run_start = _outil("run_start").fn
    out_start = asyncio.run(run_start(
        ctx, label="Test contrôle — enrichissement 3 fiches édition-vivier (campagne cliente)",
        guide="enrichissement-editeur-campagne"))
    run_id = out_start["run_id"]

    pris = [_claim_sans_run_id(ctx, ns, "test-campagne-3lignes")["row"] for _ in range(3)]
    assert all(pris), "les 3 lignes étaient libres : chaque claim en rend une"
    ids = {p["_id"] for p in pris}
    assert len(ids) == 3, "trois lignes DISTINCTES, comme dans l'incident"
    for row_id in ids:
        assert _bail(ns_id, row_id)["claimed_run"] == run_id, (
            "la pile de session doit suffire à rattacher la réservation au run — "
            "c'est le filet posé par #317")

    run_finish = _outil("run_finish").fn
    out_finish = asyncio.run(run_finish(ctx, run_id=run_id, outcome="done"))

    assert out_finish["ok"] and out_finish["was_open"] is True
    assert out_finish.get("rows_released") == 3, (
        "la panne exacte du signal 414 : `run_finish` répondait `ok` sans rien "
        f"libérer. Réponse actuelle : {out_finish!r}")
    for row_id in ids:
        bail = _bail(ns_id, row_id)
        assert bail["claimed_by"] is None and bail["claimed_run"] is None, (
            f"ligne {row_id} encore réservée après run_finish : {bail!r}")
