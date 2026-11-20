"""La FORME sous laquelle un appel a désigné un tableau (`args.ref_kind`), au journal.

Le journal cite le tableau résolu : `957`, `slot:vivier` et `vivier` y laissaient la même
trace, et l'usage d'un nom de tableau ne se comptait pas avant son retrait
(`deprecations.RETRAIT_NOM_DE_TABLEAU`). La forme (`number`|`slot`|`name`, jamais le nom)
est notée au point de résolution et rejoint la ligne MCP (relevé d'appel) comme la ligne
de route REST (`RestCallLogger`). Ce banc fige les deux faces, sans base ; la mesure et son
index sont joués sur une vraie base (`test_migration_20261120_tableau_par_nom.py`).
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import deprecations, session_org

# ── La règle : la forme la plus dépréciée l'emporte, no-op sans holder ────────


def test_hors_appel_noter_une_forme_ne_fait_rien():
    session_org.noter_forme_tableau(session_org.REF_NAME)


def test_la_forme_la_plus_depreciee_l_emporte():
    trace: dict = {}
    jeton = session_org.set_call_trace(trace)
    try:
        session_org.noter_forme_tableau(session_org.REF_NUMBER)
        assert trace == {"ref_kind": "number"}
        session_org.noter_forme_tableau(session_org.REF_NAME)
        session_org.noter_forme_tableau(session_org.REF_SLOT)
        session_org.noter_forme_tableau(session_org.REF_NUMBER)
    finally:
        session_org.reset_call_trace(jeton)
    assert trace == {"ref_kind": "name"}


def test_une_forme_inconnue_est_refusee():
    with pytest.raises(KeyError):
        session_org.noter_forme_tableau("nom")


@pytest.mark.parametrize("adresse,forme,avis", [("vivier", "name", True),
                                                ("204", "number", False),
                                                (" 204 ", "number", False),
                                                # Des chiffres qui NOMMENT un tableau.
                                                ("2024", "name", True)])
def test_une_adresse_resolue_note_sa_forme_et_l_avis_du_nom(adresse, forme, avis):
    trace: dict = {}
    avis_servis: list = []
    rest: dict = {}
    jetons = (session_org.set_call_trace(trace), session_org.set_call_avis(avis_servis),
              session_org.set_ref_rest(rest))
    try:
        session_org.noter_adresse_de_tableau(adresse, 204, "vivier")
    finally:
        session_org.reset_call_trace(jetons[0])
        session_org.reset_call_avis(jetons[1])
        session_org.reset_ref_rest(jetons[2])
    assert trace == {"ref_kind": forme} == rest
    assert avis_servis == ([deprecations.avis_nom_de_tableau("vivier", 204)] if avis else [])


def test_un_slot_resolu_note_slot_meme_si_le_store_voit_un_numero(monkeypatch):
    from oto_mcp import access as scope

    monkeypatch.setattr(scope, "resolve_slot_tableau", lambda name: "204")
    trace: dict = {}
    jeton = session_org.set_call_trace(trace)
    try:
        assert scope.resolve_datastore_ref("slot:vivier") == "204"
        # Le store résout ensuite l'identifiant que le slot a rendu.
        session_org.noter_adresse_de_tableau("204", 204)
        assert scope.resolve_datastore_ref("vivier") == "vivier"
    finally:
        session_org.reset_call_trace(jeton)
    assert trace == {"ref_kind": "slot"}


# ── Le store : `_resolve` note la forme, sur le relevé MCP ────────────────────

@pytest.fixture()
def store(monkeypatch):
    from oto_mcp import access, group_store, roles
    from oto_mcp.datastore import core as D

    monkeypatch.setattr(access, "current_org", lambda sub: 99)
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, oid: False)
    monkeypatch.setattr(group_store, "list_groups_for_user", lambda sub, org_id=None: [])
    monkeypatch.setattr(D.ownership, "vue_bornee", lambda: None)
    monkeypatch.setattr(D.db, "resolve_datastore_ns",
                        lambda *a, **k: {"id": 204, "datastore": "vivier"})
    return D.make_store("u-1")


@pytest.mark.parametrize("adresse,forme", [("vivier", "name"), ("204", "number")])
def test_le_store_note_la_forme_de_l_adresse(store, adresse, forme):
    trace: dict = {}
    jeton = session_org.set_call_trace(trace)
    try:
        assert store._resolve(adresse) == 204
    finally:
        session_org.reset_call_trace(jeton)
    assert trace["ref_kind"] == forme
    # Le nom résolu va dans `ns_name` (hors journal), jamais dans `ref_kind`.
    assert trace["ns_id"] == 204


def test_la_forme_rejoint_les_args_de_la_ligne_mcp():
    from oto_mcp.calllog import apply_call_trace
    from oto_mcp.server import _TRACED_ARGS

    row = apply_call_trace({"args": {"datastore": "vivier"}},
                           {"ns_id": 204, "ns_name": "vivier", "ref_kind": "name"},
                           _TRACED_ARGS)
    assert row["args"] == {"datastore": "vivier", "ns_id": 204, "ref_kind": "name"}


# ── REST : la ligne de route porte la forme, jamais le nom ────────────────────

def _route_journalisee(monkeypatch, adresse: str) -> dict:
    """Une route `/api/datastores/{datastore}/t` dont le handler résout le tableau
    (en threadpool, comme toute capacité), derrière le journal des routes : rend la
    ligne de route écrite."""
    from pydantic import BaseModel
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse

    from oto_mcp.api import routes as ar
    from oto_mcp.capabilities import _rest_adapter
    from oto_mcp.capabilities._types import Capability, ResolvedCtx, RestBinding

    class Entree(BaseModel):
        datastore: str

    class Sortie(BaseModel):
        ok: bool

    def handler(ctx, inp):
        # Ce que fait le store une fois l'adresse résolue (cf. `DatastorePg._resolve`).
        session_org.noter_adresse_de_tableau(inp.datastore, 204, "vivier")
        return {"ok": True}

    cap = Capability(
        key="t.tableau", handler=handler, Input=Entree, Output=Sortie,
        authz=lambda raw, inp: ResolvedCtx(sub=raw.sub, org_id=None),
        rest=RestBinding(verb="GET", path="/api/datastores/{datastore}/t"))

    async def authenticate(request, verifier, **kw):
        return "u-1", None

    async def options(request):
        return JSONResponse({})

    routes = _rest_adapter.make_routes(
        None, authenticate,
        lambda request, data, status=200: JSONResponse(data, status),
        lambda request, status, code, detail=None, **kw: JSONResponse({"error": code},
                                                                      status),
        options, [cap])
    lignes: list = []
    monkeypatch.setattr(ar.db, "insert_tool_call", lignes.append)
    app = ar.RestCallLogger(Starlette(routes=routes))
    scope = {"type": "http", "path": f"/api/datastores/{adresse}/t", "method": "GET",
             "headers": [], "query_string": b"", "path_params": {}}
    envoye: list = []

    async def send(m):
        envoye.append(m)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def drive():
        await app(scope, receive, send)
        await asyncio.sleep(0)
        await asyncio.gather(*list(ar._REST_LOG_TASKS), return_exceptions=True)

    asyncio.run(drive())
    assert envoye[0]["status"] == 200, envoye
    assert len(lignes) == 1
    return lignes[0]


@pytest.mark.parametrize("adresse,forme", [("vivier", "name"), ("204", "number")])
def test_la_ligne_de_route_rest_porte_la_forme(monkeypatch, adresse, forme):
    ligne = _route_journalisee(monkeypatch, adresse)
    assert ligne["kind"] == "rest"
    assert ligne["args"] == {"ref_kind": forme}
    # Rien du nom dans les `args` : seulement sa forme.
    assert "vivier" not in str(ligne["args"])


def test_une_route_qui_ne_designe_aucun_tableau_ne_porte_rien(monkeypatch):
    from oto_mcp.api import routes as ar

    lignes: list = []
    monkeypatch.setattr(ar.db, "insert_tool_call", lignes.append)

    async def aval(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def send(m):
        pass

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def drive():
        await ar.RestCallLogger(aval)({"type": "http", "path": "/api/me", "method": "GET",
                                       "headers": [], "query_string": b""}, receive, send)
        await asyncio.sleep(0)
        await asyncio.gather(*list(ar._REST_LOG_TASKS), return_exceptions=True)

    asyncio.run(drive())
    assert lignes[0]["args"] is None
    # Le holder est retiré en sortie de requête.
    session_org.noter_forme_tableau(session_org.REF_NAME)
