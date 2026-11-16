"""Bout en bout, côté AGENT : un connecteur personnalisé (« Custom API ») configuré une
fois, puis appelé par nom, contre une VRAIE API publique et un VRAI PostgreSQL.

Le parcours d'une cliente, sans un seul faux client : un admin d'org pose la carte
`api` (vrai chemin de la pose d'org, vrai coffre), importe les actions depuis le
document OpenAPI public de l'API, enregistre la définition (description, en-tête
constant, variable), puis un agent découvre le connecteur, l'appelle par nom avec
chaque verbe, en brut, et les refus (traversée, egress) tiennent. Cible : la démo
publique Swagger Petstore v3 — partagée : un identifiant de pet aléatoire et élevé, et
on retire ce qu'on crée.

Ne tourne QUE sur demande (`OTO_E2E_NETWORK=1`) : la suite n'ouvre aucune connexion
sortante (`conftest`, garde `reseau_reel`), et un test qui dépend d'un service tiers
ne doit jamais rougir la CI pour une panne qui n'est pas la nôtre. Base : `pg_dsn`.
Seules les coutures d'identité MCP (sub du jeton, org/équipe de l'appel) sont posées,
comme dans les autres bancs live.
"""
from __future__ import annotations

import asyncio
import base64
import os
import random
import uuid

import pytest
import requests

pytestmark = [
    pytest.mark.skipif(os.environ.get("OTO_E2E_NETWORK") != "1",
                       reason="bout en bout réseau : OTO_E2E_NETWORK=1 pour le jouer"),
    pytest.mark.reseau_reel("le parcours se prouve contre une vraie API publique : "
                            "import OpenAPI réel, verbes réels, en-têtes réellement émis"),
]

BASE = "https://petstore3.swagger.io/api/v3"
DOC = BASE + "/openapi.json"
_KEY = base64.b64encode(b"\x33" * 32).decode()
GARDEES = ("get_pet_by_id", "add_pet", "update_pet", "delete_pet")


@pytest.fixture(scope="module")
def base(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_e2e_customapi_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    avant = {k: os.environ.get(k) for k in ("DATABASE_URL", "OTO_MCP_MASTER_KEY")}
    avant_pool = dbconn._pool
    os.environ["DATABASE_URL"] = pg_dsn.rsplit("/", 1)[0] + "/" + name
    os.environ["OTO_MCP_MASTER_KEY"] = _KEY
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = avant_pool
        for cle, valeur in avant.items():
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


@pytest.fixture(scope="module")
def monde(base):
    """Une org, son admin, le connecteur `api` exposé (master ON)."""
    from oto_mcp import db, org_store
    from oto_mcp.connectors import activation
    u = uuid.uuid4().hex[:8]
    admin = f"admin_{u}"
    db.upsert_user(admin, email=f"{admin}@example.test")
    org = org_store.create_org(f"Org {u}", created_by=admin)
    org_store.add_org_member(org, admin, "org_admin")
    activation.set_activation("api", True, org_id=None, set_by=admin)
    return {"org": org, "admin": admin}


@pytest.fixture
def agent(monde, monkeypatch):
    """Les coutures d'identité d'un appel MCP, et les vrais outils montés à nu."""
    from fastmcp import FastMCP
    from oto_mcp import access
    from oto_mcp.tools import api as t_http, api_calls, api_ops
    sub, org = monde["admin"], monde["org"]
    monkeypatch.setattr(t_http, "current_user_sub_from_token", lambda: sub)
    monkeypatch.setattr(api_ops, "current_user_sub_from_token", lambda: sub)
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: sub)
    monkeypatch.setattr(access, "current_org", lambda s: org)
    monkeypatch.setattr(access, "current_group", lambda s: None)
    monkeypatch.setattr(access, "project_pinned_instance", lambda p: None)
    m = FastMCP("e2e")
    for mod in (t_http, api_ops, api_calls):
        mod.register(m)
    return lambda name: asyncio.run(m.get_tool(name)).fn


@pytest.fixture
def emis(monkeypatch):
    """Les requêtes RÉELLEMENT parties — capturées au `Session.send`, le transport
    reste le vrai."""
    vues = []
    original = requests.Session.send

    def send(self, request, **kw):
        vues.append(request)
        return original(self, request, **kw)
    monkeypatch.setattr(requests.Session, "send", send)
    return vues


def _poser_carte(monde, account: str, fields: dict):
    """La pose d'org réelle (capacité `org.secret.set` : validation, pose partielle,
    garde de compte, coffre chiffré, instance)."""
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.orgs import secrets as cap
    return cap._set_secret(ResolvedCtx(sub=monde["admin"], org_id=monde["org"]),
                           cap.SetSecretInput(org_id=monde["org"], provider="api",
                                              account=account, fields=fields))


def _sous_compte(account: str):
    from oto_mcp import session_org
    return session_org.set_call_account(account), session_org.reset_call_account


PET = random.randint(10**12, 9 * 10**12)


def test_le_parcours_complet(monde, agent, emis):
    from oto_mcp import credentials_store as cs
    from oto_mcp.mcp_errors import McpError

    # 1. l'admin pose la carte — le nom affiché garde son espace, en clair dans meta
    assert _poser_carte(monde, "petstore", {
        "base_url": BASE, "auth_mode": "header", "header_name": "api_key",
        "token": "special-key", "label": "Pet Store"})["ok"]
    [ligne] = cs.list_accounts("org", str(monde["org"]), "api")
    assert ligne["meta"]["label"] == "Pet Store"

    # 2. import depuis l'URL publique du document OpenAPI
    imp = agent("api_act")(op="import", source="url", url=DOC)
    par_nom = {o["name"]: o for o in imp["operations"]}
    assert set(GARDEES) <= set(par_nom), sorted(par_nom)
    assert (par_nom["get_pet_by_id"]["method"], par_nom["get_pet_by_id"]["path"]) == \
        ("GET", "/pet/{petId}")
    assert par_nom["get_pet_by_id"]["input"]["properties"]["petId"]["type"] == "integer"
    assert par_nom["get_pet_by_id"]["input"]["required"] == ["petId"]
    assert (par_nom["add_pet"]["method"], par_nom["add_pet"]["path"]) == ("POST", "/pet")
    assert {"name", "photoUrls"} <= set(par_nom["add_pet"]["input"]["required"])
    assert (par_nom["update_pet"]["method"], par_nom["delete_pet"]["method"]) == \
        ("PUT", "DELETE")
    assert imp["saved"] is False

    # 3. la définition, enregistrée une fois
    # + une action écrite à la main, à identifiant TEXTE : la traversée se joue sur elle
    a_la_main = {"name": "get_pet_text", "description": "Read a pet (text id).",
                 "method": "GET", "path": "/pet/{id}",
                 "input": {"type": "object", "properties": {"id": {"type": "string"}},
                           "required": ["id"]}}
    out = agent("api_act")(
        op="set_definition",
        operations=[par_nom[n] for n in GARDEES] + [a_la_main],
        description="Swagger demo pet store: pets, orders, users.",
        logo_domain="swagger.io", headers={"X-Request-Source": "oto-e2e"},
        variables={"petId": {"value": str(PET), "description": "the e2e pet"}})
    assert out["actions"] == [*GARDEES, "get_pet_text"]
    with pytest.raises(McpError, match="authenticates"):
        agent("api_act")(op="set_definition", headers={"api_key": "x"})   # l'en-tête de la clé

    # 4. découverte : la liste de l'org, et `oto_connector(name=…)`
    liste = agent("api")()
    [c] = liste["connectors"]
    assert (c["label"], c["account"], c["actions_count"]) == ("Pet Store", "petstore", 5)
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.connectors import selection as CS
    trouve = CS._me(ResolvedCtx(sub=monde["admin"], org_id=monde["org"]),
                    CS.MyConnectorsInput(name="pet store"))
    assert trouve["custom_connector"]["label"] == "Pet Store"
    assert trouve["custom_connector"]["actions_count"] == 5

    act = agent("api_act")

    def call(**kw):
        return act(op="call", **kw)
    pet = {"id": PET, "name": "e2e-dog", "photoUrls": [], "status": "available"}
    try:
        # 5. chaque verbe : dry_run puis réel, compte résolu par le nom de l'action
        assert call(operation="add_pet", args=pet, dry_run=True)["body"] == pet
        n = len(emis)
        assert call(operation="add_pet", args=pet)["id"] == PET
        envoi = emis[n]
        assert envoi.method == "POST"
        assert envoi.headers["X-Request-Source"] == "oto-e2e"
        assert envoi.headers["api_key"] == "special-key"

        sec = call(operation="get_pet_by_id", args={"petId": PET}, dry_run=True)
        assert sec["path"] == f"/pet/{PET}" and sec["account"] == "petstore"
        assert call(operation="get_pet_by_id", args={"petId": PET})["name"] == "e2e-dog"
        # …puis depuis la variable enregistrée
        assert call(operation="get_pet_by_id", dry_run=True)["path"] == f"/pet/{PET}"
        assert call(operation="get_pet_by_id")["id"] == PET

        maj = {**pet, "name": "e2e-dog-2", "status": "sold"}
        assert call(operation="update_pet", args=maj, dry_run=True)["method"] == "PUT"
        assert call(operation="update_pet", args=maj)["status"] == "sold"

        # 6. appel brut (PATCH n'existe pas chez Petstore : DELETE brut, après un
        # second pet créé à cette fin)
        def brut(**kw):
            return act(op="request", **kw)
        assert brut(method="delete", path=f"/pet/{PET + 1}", dry_run=True) == {
            "dry_run": True, "account": "petstore", "method": "DELETE",
            "path": f"/pet/{PET + 1}", "query": {}, "body": None}
        call(operation="add_pet", args={**pet, "id": PET + 1})
        brut(method="delete", path=f"/pet/{PET + 1}")

        # 7. la traversée est refusée avant tout envoi
        n = len(emis)
        with pytest.raises(McpError, match="type integer"):
            call(operation="get_pet_by_id", args={"petId": "../store/inventory"})
        for evasion in ("../store/inventory", "..", "%2e%2e", "a%2Fb"):
            with pytest.raises(McpError, match="single segment"):
                call(operation="get_pet_text", args={"id": evasion})
        assert len(emis) == n
        assert call(operation="get_pet_text", args={"id": str(PET)})["id"] == PET
    finally:
        assert call(operation="delete_pet", args={"petId": PET}, dry_run=True)["method"] \
            == "DELETE"
        call(operation="delete_pet", args={"petId": PET})
    assert emis[-1].method == "DELETE"
    with pytest.raises(McpError, match="HTTP 404"):
        call(operation="get_pet_by_id", args={"petId": PET})

    # 7 bis. une carte vers une adresse interne est refusée de bout en bout
    _poser_carte(monde, "interne", {"base_url": "http://127.0.0.1:9", "auth_mode": "none",
                                    "label": "Interne"})
    jeton, reset = _sous_compte("interne")
    try:
        n = len(emis)
        with pytest.raises(McpError, match="OTO_EGRESS_ALLOW|interne|internal"):
            agent("api_act")(op="request", method="GET", path="/x")
        assert len(emis) == n
    finally:
        reset(jeton)

    # 8. l'export de la définition ne porte aucun secret
    from oto_mcp import providers
    from _datastore_rest import call as rest_call
    code, export = rest_call("custom_api.definition.get",
                             path_params={"id": str(monde["org"])},
                             query=b"account=petstore", sub=monde["admin"])
    assert code == 200, export
    secrets = {f.name for f in providers.REGISTRY["api"].secret_fields if f.secret}
    assert not secrets & set(export) and "special-key" not in str(export)
    assert (export["label"], export["base_url"], export["header_name"]) == \
        ("Pet Store", BASE, "api_key")
    assert export["headers"] == {"X-Request-Source": "oto-e2e"}
    assert [o["name"] for o in export["operations"]] == [*GARDEES, "get_pet_text"]
