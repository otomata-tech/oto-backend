"""Un jeton de délégation n'appelle pas la face REST (`delegation_rest_forbidden`, #1499).

La liste d'outils d'un travail hébergé n'est tenue que sur la face MCP
(`outils_du_travail`) : sous `/api/*`, rien ne la regardait, et le jeton de délégation
ouvrait tout ce que son porteur peut faire. Il y est refusé juste après sa vérification,
sauf sur les routes que le worker appelle LUI-MÊME avec ce jeton
(`api.base.ROUTES_DE_LA_DELEGATION`) — chacune rejouée ici.

Sur une vraie base (`live`) : le jeton est émis par le vrai chemin du runner
(`runner_jobs._delegue`) et présenté à la vraie authentification REST (`_authenticate`),
puis à l'adaptateur d'une capacité. Un jeton d'utilisateur et la face MCP gardent leur
chemin d'avant.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

SUB = "sub-delegation-rest"


@pytest.fixture(scope="module")
def org(live):
    from oto_mcp import db
    from oto_mcp.org_store import members, orgs
    db.upsert_user(SUB, email=f"{SUB}@acme.test", name=SUB)
    o = orgs.create_org(f"Acme rest {uuid.uuid4().hex[:4]}", created_by=SUB)
    members.add_org_member(o, SUB, "org_admin")
    return o


@pytest.fixture
def jeton(org):
    """Le jeton d'un travail enfilé, émis par le VRAI chemin du runner."""
    from oto_mcp import db
    from oto_mcp.capabilities import runner_jobs
    job = db.enqueue_job(org, "start", sub=SUB,
                         payload={"tools": ["acme_people"], "procedure": "p", "input": "x"})
    return runner_jobs._delegue({"id": job["id"], "sub": SUB, "org_id": org},
                                bail_s=60, claimant="test")["delegated_token"]


def _scope(methode: str, chemin: str, jeton: str) -> dict:
    return {"type": "http", "method": methode, "path": chemin, "query_string": b"",
            "root_path": "", "scheme": "http", "server": ("test", 80),
            "http_version": "1.1",
            "headers": [(b"authorization", f"Bearer {jeton}".encode()),
                        (b"content-type", b"application/json")]}


def _auth(methode: str, chemin: str, jeton: str):
    from starlette.requests import Request

    from oto_mcp import verrou_org
    from oto_mcp.api import base
    from oto_mcp.auth import token_scopes

    async def _go():
        try:
            return await base._authenticate(Request(_scope(methode, chemin, jeton)),
                                            verifier=None)
        finally:
            token_scopes.set_current(None)
            verrou_org.poser(None)
    return asyncio.run(_go())


@pytest.mark.parametrize("methode,chemin", [
    ("GET", "/api/me"),
    ("POST", "/api/me/runner/jobs"),
    ("POST", "/api/me/runner/triggers"),
    ("POST", "/api/me/recipes"),
    ("GET", "/api/datastores"),
    ("GET", "/api/me/runs/thread"),   # la route exceptée, sous un autre verbe
])
def test_toute_route_hors_exception_est_refusee(jeton, methode, chemin):
    from oto_mcp.api import base
    sub, err = _auth(methode, chemin, jeton)
    assert sub is None and err.status_code == 403
    assert json.loads(err.body)["error"] == base.CODE_DELEGATION_REST


def test_les_exceptions_sont_celles_du_worker_et_nommees():
    """Le worker (oto-runner) n'envoie son jeton délégué qu'au fil du run
    (`Backend.thread_read` / `thread_append`). Une exception de plus se justifie ici."""
    from oto_mcp.api import base
    assert set(base.ROUTES_DE_LA_DELEGATION) == {("POST", "/api/me/runs/thread")}
    assert all(raison.strip() for raison in base.ROUTES_DE_LA_DELEGATION.values())


def test_le_fil_du_run_reste_ouvert_au_jeton_delegue(jeton):
    sub, err = _auth("POST", "/api/me/runs/thread", jeton)
    assert err is None and sub == SUB


def test_le_fil_du_run_est_servi_par_la_capacite(jeton, org):
    """Rejoué par l'adaptateur REST, avec la vraie authentification : le worker relit le
    fil d'un run de son porteur sous le jeton délégué."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from oto_mcp import db
    from oto_mcp.api import base
    from oto_mcp.capabilities import _rest_adapter, registry
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    db.insert_run(run_id, sub=SUB, org_id=org, label="t", guide=None)
    cap = next(c for c in registry.CAPABILITIES if c.key == "runs.thread")
    (binding,) = cap.rest_bindings()

    class _Verifier:
        async def verify_token(self, token):
            return None
    handler = _rest_adapter._make_handler(
        cap, binding, _Verifier(), base._authenticate,
        lambda _r, p, status=200: JSONResponse(p, status_code=status),
        lambda _r, s, code, d=None, details=None: JSONResponse({"error": code},
                                                               status_code=s))
    corps = json.dumps({"op": "read", "run_id": run_id}).encode()

    async def _recevoir():
        return {"type": "http.request", "body": corps, "more_body": False}
    rep = asyncio.run(handler(Request(_scope("POST", "/api/me/runs/thread", jeton),
                                      receive=_recevoir)))
    assert rep.status_code == 200, rep.body
    assert json.loads(rep.body)["messages"] == []


def test_un_jeton_d_utilisateur_garde_son_chemin(org):
    from oto_mcp import db
    tok = db.create_api_token(SUB, label="cli")
    sub, err = _auth("GET", "/api/me", tok)
    assert err is None and sub == SUB


def test_un_jeton_delegue_invalide_reste_un_401(org):
    sub, err = _auth("GET", "/api/me", "oto_" + "0" * 40)
    assert sub is None and err.status_code == 401


def test_la_face_mcp_n_est_pas_gardee_par_ce_refus(jeton):
    """`/mcp` vérifie le jeton par son propre chemin (`server._verify_api_token`) : un
    agent hébergé y travaille, liste d'outils tenue."""
    from oto_mcp import server
    verifier = object.__new__(server._IatGatedVerifier)
    tok = asyncio.run(verifier._verify_api_token(jeton))
    assert tok is not None and tok.claims["token_kind"] == "delegation"
