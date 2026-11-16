"""Les routes REST des opérations d'un « Custom API » — par la VRAIE chaîne de
l'adaptateur (`_datastore_rest.call`) : query, corps, refus de champ inconnu, autz,
handler.

Même partage de droits que la clé de l'org (`orgs/secrets.py`) : un membre LIT, seul
un administrateur de l'org ÉCRIT ou IMPORTE. Sans compte `api` pour ce nom : 404.
"""
from __future__ import annotations

import pytest

from _datastore_rest import call

from oto_mcp import credentials_store, egress, roles
from oto_mcp.capabilities import _authz
from oto_mcp.db import connector_instances as ci

ORG = 35
CARTE = {"base_url": "https://payroll.example.test/v1", "auth_mode": "header",
         "header_name": "X-Key", "token": "SECRET-TOKEN", "doc_path": "/openapi.json"}
OP = {"name": "list_teams", "description": "List the teams.", "method": "GET",
      "path": "/teams", "input": {"type": "object", "properties": {}}}


@pytest.fixture()
def org(monkeypatch):
    """L'org #35 a un compte `api` nommé `paie` ; u-admin l'administre, u-membre
    n'en est que membre, u-dehors n'en est pas."""
    etat = {"config": {"operations": [OP]}, "ecrit": []}
    monkeypatch.setattr(_authz.access, "get_user_role", lambda sub: "member")
    # Le refus d'admin nomme les administrateurs (`detenteurs`) : une lecture en base.
    monkeypatch.setattr(_authz.detenteurs, "admins_de_l_org", lambda sub, o: None)
    monkeypatch.setattr(roles, "is_org_member",
                        lambda sub, o: sub in ("u-admin", "u-membre") and o == ORG)
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, o: sub == "u-admin" and o == ORG)
    monkeypatch.setattr(credentials_store, "list_accounts",
                        lambda et, eid, con: [{"account": "paie"}]
                        if (et, eid, con) == ("org", str(ORG), "api") else [])
    monkeypatch.setattr(ci, "live_instance_config",
                        lambda ot, oid, con, acc="": {"id": 4, "config": etat["config"]}
                        if acc == "paie" else None)

    def _merge(iid, patch):
        etat["ecrit"].append((iid, patch))
        etat["config"] = {**etat["config"], **patch}
        return True
    monkeypatch.setattr(ci, "merge_instance_config", _merge)
    # La carte de l'org (déchiffrée en mémoire) : clé, mode, et un nom affiché.
    monkeypatch.setattr(credentials_store, "get_credential_with_meta",
                        lambda et, eid, con, acc="": {
                            "secret": credentials_store.pack_secret("api", CARTE),
                            "meta": {"label": "Acme Payroll"}})
    return etat


def _get(sub, account=b"paie"):
    return call("custom_api.operations.get", path_params={"id": str(ORG)},
                query=b"account=" + account, sub=sub)


def _put(sub, ops):
    return call("custom_api.operations.set", path_params={"id": str(ORG)},
                query=b"account=paie", body={"operations": ops}, sub=sub)


def _import(sub, body):
    return call("custom_api.operations.import", path_params={"id": str(ORG)},
                query=b"account=paie", body=body, sub=sub)


def test_un_membre_lit(org):
    code, out = _get("u-membre")
    assert code == 200 and out == {"account": "paie", "operations": [OP]}


def test_hors_de_l_org_refuse(org):
    assert _get("u-dehors")[0] == 403


def test_compte_inconnu_404(org):
    code, out = _get("u-membre", account=b"autre")
    assert code == 404 and out["error"] == "http_credential_not_found"


def test_seul_l_admin_ecrit(org):
    code, out = _put("u-membre", [OP])
    assert code == 403 and org["ecrit"] == []
    code, out = _put("u-admin", [OP])
    assert code == 200 and out == {"account": "paie", "operations": [OP]}
    assert org["ecrit"] == [(4, {"operations": [OP]})]


def test_liste_invalide_400_avec_chaque_probleme(org):
    code, out = _put("u-admin", [OP, {**OP, "path": "/a/../b"}])
    assert code == 400 and out["error"] == "invalid_operations"
    errs = out["details"]["errors"]
    assert any("duplicate name" in e for e in errs) and any("`..`" in e for e in errs)
    assert org["ecrit"] == []


def test_import_inline_rend_des_candidats_sans_ecrire(org):
    doc = {"openapi": "3.0.0", "paths": {
        "/teams": {"get": {"operationId": "listTeams"}, "head": {}}}}
    code, out = _import("u-admin", {"source": "inline", "document": doc})
    assert code == 200
    assert [o["name"] for o in out["operations"]] == ["list_teams"]
    assert out["skipped"] == [{"path": "/teams", "method": "HEAD",
                               "reason": "method HEAD is not supported"}]
    assert org["ecrit"] == []


def test_import_reserve_a_l_admin(org):
    assert _import("u-membre", {"source": "inline", "document": {}})[0] == 403


def test_import_url_interne_refuse(org, monkeypatch):
    monkeypatch.setattr(egress, "resolved_addresses", lambda h, p: {"10.0.0.5"})
    code, out = _import("u-admin", {"source": "url", "url": "https://docs.acme.test/o.json"})
    assert code == 400 and out["error"] == "import_failed"


def test_import_document_non_openapi_refuse(org):
    code, out = _import("u-admin", {"source": "inline", "document": {"hello": 1}})
    assert code == 400 and out["error"] == "import_failed"


def test_import_doc_path_lu_avec_l_auth_du_compte(org, monkeypatch):
    """La route de doc de la carte, lue par le MÊME client que les appels (auth,
    egress, redirections refusées), bornée en taille."""
    import json
    from unittest.mock import patch
    champs = {"base_url": "https://payroll.example.test/v1", "auth_mode": "bearer",
              "token": "T", "doc_path": "/openapi.json"}
    monkeypatch.setattr(credentials_store, "get_credential_with_meta",
                        lambda et, eid, con, acc="": {
                            "secret": credentials_store.pack_secret("api", champs)})
    monkeypatch.setattr(egress, "resolved_addresses", lambda h, p: {"93.184.216.34"})
    doc = {"openapi": "3.0.0", "paths": {"/teams": {"get": {"operationId": "listTeams"}}}}
    with patch("oto_mcp.tools.api.HttpConnectorClient") as cls:
        cls.return_value.get_raw.return_value = (json.dumps(doc).encode(), "application/json")
        code, out = _import("u-admin", {"source": "doc_path"})
    assert code == 200 and [o["name"] for o in out["operations"]] == ["list_teams"]
    assert cls.call_args.args[:2] == ("https://payroll.example.test/v1", "bearer")
    path, = cls.return_value.get_raw.call_args.args
    assert path == "/openapi.json"
    assert cls.return_value.get_raw.call_args.kwargs["max_bytes"] == 2 * 1024 * 1024


# ── la définition : export sans secret, pose par l'admin ─────────────────────

def _def_get(sub):
    return call("custom_api.definition.get", path_params={"id": str(ORG)},
                query=b"account=paie", sub=sub)


def _def_put(sub, body):
    return call("custom_api.definition.set", path_params={"id": str(ORG)},
                query=b"account=paie", body=body, sub=sub)


def test_l_export_ne_porte_aucun_champ_secret(org):
    from oto_mcp import providers
    code, out = _def_get("u-membre")
    assert code == 200
    secrets = {f.name for f in providers.REGISTRY["api"].secret_fields if f.secret}
    assert secrets and not secrets & set(out)
    assert "SECRET-TOKEN" not in str(out)
    assert out["base_url"] == CARTE["base_url"] and out["header_name"] == "X-Key"
    assert out["label"] == "Acme Payroll" and out["operations"] == [OP]
    assert out["headers"] == {} and out["variables"] == {}


def test_poser_la_definition_partielle_par_l_admin(org):
    assert _def_put("u-membre", {"description": "x"})[0] == 403
    code, out = _def_put("u-admin", {
        "description": "The payroll tool.", "logo_domain": "Acme.test",
        "headers": {"X-Api-Version": "2"},
        "variables": {"organizationId": {"value": "org_9", "description": "ours"}}})
    assert code == 200
    assert out["logo_domain"] == "acme.test" and out["operations"] == [OP]
    assert out["variables"]["organizationId"]["value"] == "org_9"
    assert set(org["ecrit"][0][1]) == {"description", "logo_domain", "headers", "variables"}


def test_une_definition_invalide_400(org):
    code, out = _def_put("u-admin", {"headers": {"x-key": "abc", "Cookie": "a=b"}})
    assert code == 400 and out["error"] == "invalid_definition"
    assert len(out["details"]["errors"]) == 2 and org["ecrit"] == []


def test_un_champ_de_carte_ne_se_pose_pas_par_la_definition(org):
    """La carte (et sa clé) se pose par la route des secrets, jamais par ici."""
    code, out = _def_put("u-admin", {"token": "abc"})
    assert code == 400 and org["ecrit"] == []


# --- les routes de l'ancien nom (`http`) : un alias 308, daté, vers la nouvelle --------

_ANCIENNES = [("GET", "operations"), ("PUT", "operations"), ("POST", "operations/import"),
              ("GET", "definition"), ("PUT", "definition")]


@pytest.fixture(scope="module")
def client_rest():
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oto_mcp.api import routes as api_routes

    class _FauxVerifieur:
        """`make_routes` ne fait que CAPTURER le verifier."""

    return TestClient(Starlette(routes=api_routes.make_routes(_FauxVerifieur(),
                                                               mcp_instance=None)))


def test_chaque_route_du_connecteur_a_son_ancien_chemin():
    from oto_mcp import deprecations
    derives = {(a.verbe, a.ancien, a.nouveau) for a in deprecations._alias_connecteur_api()}
    assert derives == {(v, f"/api/orgs/{{id}}/connectors/http/{s}",
                        f"/api/orgs/{{id}}/connectors/api/{s}") for v, s in _ANCIENNES}


@pytest.mark.parametrize("verbe,suffixe", _ANCIENNES)
def test_l_ancienne_route_redirige_en_308_vers_la_nouvelle(client_rest, verbe, suffixe):
    """Même méthode, même corps, query string reportée : la page d'hier aboutit."""
    from oto_mcp import deprecations
    r = client_rest.request(verbe, f"/api/orgs/{ORG}/connectors/http/{suffixe}?account=paie",
                            follow_redirects=False)
    assert r.status_code == 308, (verbe, suffixe, r.status_code)
    assert r.headers["location"] == f"/api/orgs/{ORG}/connectors/api/{suffixe}?account=paie"
    assert r.headers["deprecation"] == "true"
    assert r.headers["sunset"] == deprecations.RETRAIT_CONNECTEUR_API.strftime("%d/%m/%Y")


def test_le_contrat_publie_l_ancienne_route_depreciee_et_datee():
    from oto_mcp import deprecations, openapi
    doc = openapi.build()
    date = deprecations.RETRAIT_CONNECTEUR_API.strftime("%d/%m/%Y")
    for verbe, suffixe in _ANCIENNES:
        op = doc["paths"][f"/api/orgs/{{id}}/connectors/http/{suffixe}"][verbe.lower()]
        assert op["deprecated"] is True and "308" in op["responses"], (suffixe, verbe)
        assert f"/connectors/api/{suffixe}" in op["summary"] and date in op["summary"]
        assert "200" in doc["paths"][f"/api/orgs/{{id}}/connectors/api/{suffixe}"][
            verbe.lower()]["responses"]
