"""Le verrou d'org d'un jeton de délégation (`oto_mcp/verrou_org.py`), sur une vraie base.

Un porteur membre des orgs A et B ; son travail appartient à A. Son jeton, émis par
`runner_jobs._delegue`, ne doit résoudre AUCUNE route vers B — jeton d'appel, projet,
équipe, instance, run, partage reçu, rôle d'org, maison — et toutes vers A. Un tiers et
un jeton ordinaire gardent leur chemin d'avant. Le verrou est posé par le VRAI bord :
la vérification du jeton (`server._verify_api_token`), le middleware MCP et
l'authentification REST.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

PORTEUR = "sub-verrou-porteur"
TIERS = "sub-verrou-tiers"


@pytest.fixture(scope="module")
def orgs(live):
    from oto_mcp import db, group_store, org_store
    for s in (PORTEUR, TIERS):
        db.upsert_user(s, email=f"{s}@acme.test", name=s)
    a = org_store.create_org(f"Acme A {uuid.uuid4().hex[:4]}", created_by=PORTEUR)
    b = org_store.create_org(f"Acme B {uuid.uuid4().hex[:4]}", created_by=PORTEUR)
    for o in (a, b):
        org_store.add_org_member(o, PORTEUR, "org_admin")
    org_store.add_org_member(b, TIERS)
    equipe_b = group_store.create_group(b, f"Team B {uuid.uuid4().hex[:4]}")
    equipe_b = equipe_b["group_id"] if isinstance(equipe_b, dict) else equipe_b
    group_store.add_group_member(int(equipe_b), PORTEUR)
    return {"a": a, "b": b, "equipe_b": int(equipe_b)}


@pytest.fixture
def jeton(orgs):
    """Un jeton émis par le VRAI chemin du runner pour un travail de l'org A."""
    from oto_mcp.capabilities import runner_jobs
    job = {"id": 900000 + uuid.uuid4().int % 99999, "sub": PORTEUR, "org_id": orgs["a"]}
    out = runner_jobs._delegue(dict(job), bail_s=60, claimant="test")
    return out["delegated_token"], job


@pytest.fixture
def verrouille(jeton):
    """Le verrou posé depuis la ligne que la VRAIE vérification du jeton rend."""
    from oto_mcp import db, verrou_org
    row = db.verify_api_token(jeton[0])
    t = verrou_org.poser(verrou_org.depuis_ligne(row))
    yield row
    verrou_org.lever(t)


def _code(exc) -> str:
    data = getattr(getattr(exc, "error", None), "data", None) or {}
    return data.get("code")


def test_le_jeton_emis_par_le_runner_porte_son_travail_et_son_org(jeton, orgs):
    from oto_mcp import db
    row = db.verify_api_token(jeton[0])
    assert row["token_kind"] == "delegation" and row["verrou_org"] is True
    assert row["verrou_org_id"] == orgs["a"] and row["job_id"] == jeton[1]["id"]


def test_les_claims_mcp_portent_le_verrou(jeton, orgs):
    from oto_mcp import server
    verifier = object.__new__(server._IatGatedVerifier)
    tok = asyncio.run(verifier._verify_api_token(jeton[0]))
    assert tok.claims["verrou_org"] is True and tok.claims["verrou_org_id"] == orgs["a"]


def test_le_middleware_mcp_pose_le_verrou_pour_la_requete(jeton, orgs, monkeypatch):
    from fastmcp.server import dependencies

    from oto_mcp import db, verrou_org
    from oto_mcp.middleware.verrou_org import VerrouOrgMiddleware
    row = db.verify_api_token(jeton[0])

    class _Tok:
        claims = {"sub": PORTEUR, **{k: row[k] for k in
                                     ("job_id", "verrou_org", "verrou_org_id")}}
    monkeypatch.setattr(dependencies, "get_access_token", lambda: _Tok())
    vu = {}

    async def suite(_ctx):
        vu["v"] = verrou_org.courant()
        return "ok"
    asyncio.run(VerrouOrgMiddleware().on_request(None, suite))
    assert vu["v"].org_id == orgs["a"] and verrou_org.courant() is None


def test_l_authentification_rest_pose_le_verrou(jeton, orgs):
    from starlette.requests import Request

    from oto_mcp import verrou_org
    from oto_mcp.api import base
    scope = {"type": "http", "method": "GET", "path": "/api/me", "headers": [
        (b"authorization", f"Bearer {jeton[0]}".encode())], "query_string": b""}

    async def _auth():
        sub, err = await base._authenticate(Request(scope), verifier=None)
        return sub, err, verrou_org.courant()
    sub, err, v = asyncio.run(_auth())
    assert err is None and sub == PORTEUR and v.org_id == orgs["a"]


def test_aucun_role_hors_de_l_org_du_travail(verrouille, orgs):
    from oto_mcp import roles
    assert roles.is_org_member(PORTEUR, orgs["a"])
    assert not roles.is_org_member(PORTEUR, orgs["b"])
    assert not roles.is_org_admin(PORTEUR, orgs["b"])
    # Un TIERS garde son chemin : le verrou ne vise que le porteur.
    assert roles.is_org_member(TIERS, orgs["b"])


def test_l_escalade_plateforme_ne_franchit_pas_le_verrou(verrouille, orgs, monkeypatch):
    from oto_mcp import access, roles
    monkeypatch.setattr(access, "is_super_admin", lambda s: True)
    autre = orgs["b"] + 10_000
    assert not roles.is_org_admin(PORTEUR, autre)
    assert roles.is_org_admin(PORTEUR, orgs["a"])


def test_l_equipe_d_une_autre_org_est_hors_de_portee(verrouille, orgs):
    from oto_mcp import roles
    assert not roles.can_read_group(PORTEUR, orgs["equipe_b"])
    assert roles.effective_group_role(PORTEUR, orgs["equipe_b"]) is None


def test_toute_pose_d_org_hors_du_travail_est_refusee(verrouille, orgs):
    from oto_mcp import session_org
    from oto_mcp.mcp_errors import McpError
    for poser in (session_org.set_call_org, session_org.set_call_run_org):
        with pytest.raises(McpError) as e:
            poser(orgs["b"])
        assert _code(e.value) == "org_out_of_job"
    session_org.reset_call_org(session_org.set_call_org(orgs["a"]))


def test_le_jeton_d_appel_org_est_refuse_nommement(verrouille, orgs, monkeypatch):
    from oto_mcp import call_axes
    from oto_mcp.mcp_errors import McpError
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: PORTEUR)
    with pytest.raises(McpError) as e:
        asyncio.run(call_axes.resolve_org_guarded(orgs["b"]))
    assert _code(e.value) == "org_out_of_job"
    assert asyncio.run(call_axes.resolve_org_guarded(orgs["a"])) == orgs["a"]


def test_l_org_de_l_appel_est_celle_du_travail_jamais_la_maison(verrouille, orgs):
    from oto_mcp import access, org_store
    org_store.set_active_org(PORTEUR, orgs["b"])
    assert access.current_org(PORTEUR) == orgs["a"]


def test_les_partages_recus_ne_comptent_que_dans_l_org_du_travail(verrouille, orgs):
    from oto_mcp import ownership
    scope = ownership.accessor_scope(PORTEUR)
    assert scope.org_ids == [orgs["a"]] and orgs["equipe_b"] not in scope.group_ids


def test_un_tableau_d_une_autre_org_est_hors_de_portee(verrouille, orgs):
    from oto_mcp import db, ownership
    ns_b = db.create_datastore("org", str(orgs["b"]), f"verrou-b-{uuid.uuid4().hex[:6]}")
    ns_a = db.create_datastore("org", str(orgs["a"]), f"verrou-a-{uuid.uuid4().hex[:6]}")
    assert not ownership.can_access(PORTEUR, ownership.TYPE_RESSOURCE_DATASTORE, str(ns_b))
    assert ownership.can_access(PORTEUR, ownership.TYPE_RESSOURCE_DATASTORE, str(ns_a))


def test_report_laisse_passer_off_coupe(verrouille, orgs, monkeypatch):
    from oto_mcp import access, org_store, ownership, roles, session_org
    monkeypatch.setenv("OTO_VERROU_ORG_DELEGATION", "report")
    assert roles.is_org_member(PORTEUR, orgs["b"])
    assert roles.can_read_group(PORTEUR, orgs["equipe_b"])
    session_org.reset_call_org(session_org.set_call_org(orgs["b"]))
    # Les points qui BORNENT laissent passer aussi : rien n'est appliqué en `report`.
    org_store.set_active_org(PORTEUR, orgs["b"])
    assert access.current_org(PORTEUR) == orgs["b"]
    assert orgs["b"] in ownership.accessor_scope(PORTEUR).org_ids
    monkeypatch.setenv("OTO_VERROU_ORG_DELEGATION", "off")
    assert roles.is_org_member(PORTEUR, orgs["b"])


def test_un_jeton_ordinaire_n_est_pas_verrouille(orgs):
    from oto_mcp import db, roles, verrou_org
    tok = db.create_api_token(PORTEUR, label="cli")
    row = db.verify_api_token(tok)
    assert row["verrou_org"] is False and verrou_org.depuis_ligne(row) is None
    assert roles.is_org_member(PORTEUR, orgs["b"])


def test_un_travail_sans_org_n_a_acces_a_aucune_org(orgs):
    from oto_mcp import access, roles, verrou_org
    t = verrou_org.poser(verrou_org.Verrou(sub=PORTEUR, org_id=None, job_id=1))
    try:
        assert not roles.is_org_member(PORTEUR, orgs["a"])
        assert access.current_org(PORTEUR) is None
    finally:
        verrou_org.lever(t)


def test_un_jeton_org_niche_dans_oto_call_est_refuse(verrouille, orgs, monkeypatch):
    """Un jeton `_org` posé dans les `arguments` d'`oto_call` est refusé."""
    from fastmcp.server.providers.base import Provider

    from oto_mcp import call_axes
    from oto_mcp.mcp_errors import McpError
    from oto_mcp.tools import meta
    from _mcp_app import static_mcp
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: PORTEUR)
    monkeypatch.setattr(meta, "current_user_sub_from_token", lambda: PORTEUR)

    class _Outil:
        name, description, output_schema = "zoho_record", "fake", None
        parameters = {"type": "object", "properties": {}}
        ran = False

        async def run(self, arguments):
            _Outil.ran = True

    class _Ctx:
        class fastmcp:
            transforms: list = []

            @staticmethod
            async def _list_tools():
                return [_Outil()]
    monkeypatch.setattr(Provider, "list_tools", lambda inst: inst._list_tools())
    fn = next(t.fn for t in asyncio.run(static_mcp().list_tools(run_middleware=False))
              if t.name == "oto_call")
    with pytest.raises(McpError) as e:
        asyncio.run(fn(ctx=_Ctx(), name="zoho_record",
                       arguments={"module": "Contacts", "_org": orgs["b"]}))
    assert _code(e.value) == "org_out_of_job" and not _Outil.ran


def test_tout_jeton_de_delegation_est_emis_verrouille():
    """Garde-fou : un nouveau point d'émission `kind="delegation"` sans verrou ferait
    renaître le passage d'une org à l'autre."""
    import ast
    import pathlib
    racine = pathlib.Path(__file__).resolve().parent.parent / "oto_mcp"
    fautifs = []
    for f in racine.rglob("*.py"):
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if not (isinstance(n, ast.Call) and getattr(n.func, "attr",
                    getattr(n.func, "id", None)) == "create_api_token"):
                continue
            kw = {k.arg: k.value for k in n.keywords}
            kind = kw.get("kind")
            if isinstance(kind, ast.Constant) and kind.value == "delegation":
                v = kw.get("verrou_org")
                if not (isinstance(v, ast.Constant) and v.value is True):
                    fautifs.append(f"{f.name}:{n.lineno}")
    assert not fautifs, f"jeton de délégation émis sans verrou d'org : {fautifs}"


def test_voir_en_tant_que_est_refuse_sous_un_jeton_verrouille(jeton, monkeypatch):
    from starlette.requests import Request

    from oto_mcp import session_org
    from oto_mcp.api import base
    scope = {"type": "http", "method": "GET", "path": "/api/me", "headers": [
        (b"authorization", f"Bearer {jeton[0]}".encode())], "query_string": b""}

    async def _auth():
        t = session_org.set_view_user(TIERS)
        try:
            return await base._authenticate(Request(scope), verifier=None)
        finally:
            session_org.reset_view_user(t)
    sub, err = asyncio.run(_auth())
    assert sub is None and err.status_code == 403 and b"org_out_of_job" in err.body
