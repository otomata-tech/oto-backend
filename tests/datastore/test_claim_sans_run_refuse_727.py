"""#727 — `data_claim_next` sans run actif REFUSE, et ne pose RIEN.

Le défaut (2 occurrences sur 2, deux sous-agents de mission) : sans run, le claim
rendait une vraie ligne, complète — et posait un bail. L'agent enquêtait dessus, puis
son `data_write` était refusé (un bail se tient par son RUN, `_lease_guard`) ; après
`run_start`, le claim suivant rendait une AUTRE ligne. Pire des deux mondes : la
ligne était retirée à tout le monde pendant le bail ET refusée à qui l'avait demandée.

Ce que ce banc verrouille, contre un vrai PostgreSQL et l'outil tel que le boot le
monte, derrière le vrai middleware :

1. sans run, le claim est REFUSÉ et le refus NOMME `run_start` et `_run_id` ;
2. il ne pose AUCUN bail : la ligne reste servie à l'agent suivant ;
3. avec un run, le claim réserve comme avant, sous ce run.

⚠️ Pas de run implicite (l'issue l'écarte) : ouvrir un déroulé à la place de
l'appelant attribuerait un fait à quelqu'un qui ne l'a pas posé.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from oto_mcp.mcp_errors import McpError

SUB = "sub-claim-sans-run-727"
ORG = 727


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
    if nom not in _OUTILS:
        from fastmcp import FastMCP

        from oto_mcp.tools import register_all
        m = FastMCP("t-727")
        register_all(m)
        _OUTILS[nom] = asyncio.run(m.get_tool(nom))
    return _OUTILS[nom]


def _table():
    from oto_mcp import db
    ns = "file-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", SUB, ns)
    ns = str(ns_id)
    db.datastore_insert_row(ns_id, "r0", {"siren": "417891959", "statut": "a_enrichir"})
    return ns, ns_id


def _claim(ns: str, run: str | None) -> dict:
    """La réservation comme elle arrive en production : `_run_id=` lu des arguments
    BRUTS par le middleware, posé, retiré, puis l'outil dispatché."""
    from oto_mcp.middleware.call_context import CallContextMiddleware

    outil = _outil("data_claim_next")

    class _Msg:
        pass

    class _Ctx:                     # comme le vrai MiddlewareContext : PAS de get_state
        pass

    msg = _Msg()
    msg.name = "data_claim_next"
    msg.arguments = {"datastore": ns, "worker": "sous-agent-1"}
    if run is not None:
        msg.arguments["_run_id"] = run
    ctx = _Ctx()
    ctx.message = msg

    async def _next(c):
        return await outil.run(c.message.arguments)

    async def _go():
        return await CallContextMiddleware(frozenset()).on_call_tool(ctx, _next)

    return asyncio.run(_go()).structured_content


def _bail(ns_id: int, row_id: str) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return dict(conn.execute(
            "SELECT claimed_by, claimed_until, claimed_run FROM datastore_rows "
            "WHERE ns_id = %s AND row_id = %s", (ns_id, row_id)).fetchone() or {})


def test_sans_run_le_claim_refuse_en_nommant_run_start(surface):
    ns, _ = _table()
    with pytest.raises(McpError) as e:
        _claim(ns, None)
    message = str(e.value)
    assert "run_start" in message and "_run_id" in message, message


def test_sans_run_le_claim_ne_pose_aucun_bail(surface):
    """La moitié du dégât qu'un refus seul laisserait : la ligne retirée à tout le
    monde pour la durée du bail. Rien ne doit être posé."""
    ns, ns_id = _table()
    with pytest.raises(McpError):
        _claim(ns, None)
    bail = _bail(ns_id, "r0")
    assert bail["claimed_by"] is None and bail["claimed_until"] is None, (
        f"un claim refusé a quand même posé un bail : {bail!r}")


def test_avec_run_le_claim_reserve_sous_ce_run(surface):
    from oto_mcp import db
    ns, ns_id = _table()
    run_id = uuid.uuid4().hex
    db.insert_run(run_id, sub=SUB, org_id=ORG, label="enrichissement")
    row = _claim(ns, run_id)["row"]
    assert row and row["_id"] == "r0"
    assert _bail(ns_id, "r0")["claimed_run"] == run_id


def test_la_description_servie_annonce_le_run_requis():
    """Le texte servi pilote l'agent : il doit savoir AVANT l'appel qu'un claim
    se fait dans un run."""
    d = " ".join(_outil("data_claim_next").description.split())
    assert "run_start" in d and "REFUSED" in d, d
