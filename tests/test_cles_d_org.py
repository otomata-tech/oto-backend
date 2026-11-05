"""Les clés d'API d'org (#1188, `docs/cles-d-org.md`) : des jetons qui appartiennent à
l'org, portés par son compte de service.

1. La surface : trois routes d'admin d'org, session interactive seule, sans MCP.
2. L'émission : toujours à portée, rangée dans l'org de la clé, verrouillée sur elle,
   et ce qu'émet une clé émettrice l'est aussi.
3. La base : le compte de service est `org_member` de son org sans ligne de membre,
   ne reçoit pas d'org perso, porte le préfixe de son tenant, et perd ses clés quand
   l'org est archivée.
"""
from __future__ import annotations

import pytest

from _datastore_rest import call, cap, stub_authz

from oto_mcp import verrou_org
from oto_mcp.auth import token_scopes
from oto_mcp.capabilities import _authz
from oto_mcp.capabilities import api_tokens as at
from oto_mcp.capabilities import org_api_keys as ok
from oto_mcp.datastore import core as datastore


@pytest.fixture(autouse=True)
def _portee_neuve():
    token_scopes.set_current(None)
    yield
    token_scopes.set_current(None)


# --- 1. La surface ------------------------------------------------------------

@pytest.mark.parametrize("cle,verbe,chemin", [
    ("org.api_key.list", "GET", "/api/orgs/{id}/api-keys"),
    ("org.api_key.create", "POST", "/api/orgs/{id}/api-keys"),
    ("org.api_key.delete", "DELETE", "/api/orgs/{id}/api-keys/{token_id}")])
def test_trois_routes_d_admin_d_org_sans_jeton_ni_mcp(cle, verbe, chemin):
    c = cap(cle)
    (b,) = c.rest_bindings()
    assert (b.verb, b.path, b.path_map) == (verbe, chemin, {"id": "org_id"})
    assert b.allow_api_token is False and b.allow_issuer_token is False, (
        "une clé ne gère pas les clés de son org")
    assert c.mcp is None


def test_un_membre_simple_ne_gere_pas_les_cles(monkeypatch):
    stub_authz(monkeypatch)
    monkeypatch.setattr(_authz.roles, "is_org_admin", lambda sub, org: False)
    # La phrase du refus nomme les admins (lecture en base) : hors sujet ici.
    monkeypatch.setattr(_authz, "_refus_org_admin",
                        lambda org, *a, **kw: _authz.AuthzDenied(403, "forbidden"))
    for cle, pp in (("org.api_key.list", {"id": "2"}),
                    ("org.api_key.create", {"id": "2"}),
                    ("org.api_key.delete", {"id": "2", "token_id": "9"})):
        code, _ = call(cle, path_params=pp, body={})
        assert code == 403, cle


# --- 2. L'émission ------------------------------------------------------------

@pytest.fixture
def admin(monkeypatch):
    stub_authz(monkeypatch)
    monkeypatch.setattr(_authz.roles, "is_org_admin", lambda sub, org: True)
    monkeypatch.setattr(ok.org_store, "is_archived_org", lambda org: False)
    vus: list = []
    monkeypatch.setattr(ok.org_store, "assurer_compte_de_service",
                        lambda org, by: vus.append(("compte", org, by)) or f"org-{org}")
    monkeypatch.setattr(ok.org_store, "compte_de_service_de", lambda org: f"org-{org}")
    monkeypatch.setattr(ok.db, "create_api_token",
                        lambda sub, **kw: vus.append(("create", sub, kw)) or "oto_CLE")
    monkeypatch.setattr(ok.db, "list_api_tokens",
                        lambda sub, **kw: vus.append(("list", sub, kw)) or [])
    monkeypatch.setattr(ok.db, "revoke_api_token",
                        lambda sub, tid, **kw: vus.append(("revoke", sub, tid, kw)) or True)
    monkeypatch.setattr(at.roles, "is_org_member", lambda sub, org: str(org) == "2")
    vu_par_le_store: list = []

    class _Store:
        def __init__(self, sub):
            vu_par_le_store.append((sub, verrou_org.courant()))

        def list_datastores(self):
            return [{"id": 12, "datastore": "clients"}]

    monkeypatch.setattr(datastore, "make_store", _Store)
    vus.append(("store", vu_par_le_store))
    return vus


def _cree(body):
    return call("org.api_key.create", path_params={"id": "2"}, body=body, sub="admin-1")


def test_la_cle_est_portee_par_le_compte_de_l_org_et_verrouillee(admin):
    code, out = _cree({"label": "crm", "scopes": {"namespaces": {"clients": "write"}}})
    assert code == 201, out
    assert out["token"] == "oto_CLE" and out["scopes"] == {"namespaces": {"12": "write"}}
    _, sub, kw = admin[-1]
    assert sub == "org-2"
    assert (kw["kind"], kw["verrou_org"], kw["verrou_org_id"], kw["created_by"]) == (
        "org", True, 2, "admin-1")
    assert ("compte", 2, "admin-1") in admin


def test_la_portee_se_range_dans_l_org_de_la_cle(admin):
    """Quelle que soit l'org active de l'admin : le store voit le compte de l'org,
    verrouillé sur elle, et le verrou ne survit pas à l'émission."""
    _cree({"scopes": {"namespaces": {"clients": "read"}}})
    (vus_store,) = [e[1] for e in admin if e[0] == "store"]
    assert vus_store == [("org-2", verrou_org.Verrou(sub="org-2", org_id=2))]
    assert verrou_org.courant() is None


@pytest.mark.parametrize("scopes,erreur", [
    (None, "scopes_required"), ({"runner": True}, "invalid_scopes"),
    ({"namespaces": {"inconnu": "read"}}, "unknown_namespace")])
def test_une_cle_d_org_est_toujours_portee(admin, scopes, erreur):
    code, out = _cree({"scopes": scopes})
    assert code == 400 and out["error"] == erreur, out
    assert not [e for e in admin if e[0] == "create"]


def test_une_cle_emettrice_exige_une_echeance_et_son_org(admin):
    plafond = {"issue": {"orgs": {"2": "write"}}}
    assert _cree({"scopes": plafond})[1]["error"] == "issuer_ttl_required"
    code, out = _cree({"scopes": {"issue": {"orgs": {"3": "write"}}}, "ttl_days": 90})
    assert code == 400 and out["error"] == "unknown_org", out
    code, out = _cree({"scopes": plafond, "ttl_days": 90})
    assert code == 201 and out["scopes"] == plafond, out


def test_la_liste_montre_les_cles_et_leurs_enfants(admin):
    code, out = call("org.api_key.list", path_params={"id": "2"}, sub="admin-1")
    assert code == 200 and out == {"tokens": []}
    _, sub, kw = admin[-1]
    assert sub == "org-2" and kw["kinds"] == ("org", "user")


def test_la_revocation_vise_le_compte_de_l_org_et_trace_l_admin(admin):
    code, out = call("org.api_key.delete", path_params={"id": "2", "token_id": "9"},
                     body={"reason": "fuite"}, sub="admin-1")
    assert code == 200, out
    _, sub, tid, kw = admin[-1]
    assert (sub, tid, kw["revoked_by"], kw["reason"]) == ("org-2", 9, "admin-1", "fuite")


def test_une_org_sans_compte_n_a_ni_cle_ni_revocation(admin, monkeypatch):
    monkeypatch.setattr(ok.org_store, "compte_de_service_de", lambda org: None)
    assert call("org.api_key.list", path_params={"id": "2"})[1] == {"tokens": []}
    code, out = call("org.api_key.delete", path_params={"id": "2", "token_id": "9"},
                     body={})
    assert code == 404 and out["error"] == "unknown_token"


def test_ce_qu_emet_une_cle_emettrice_reste_verrouille(monkeypatch):
    stub_authz(monkeypatch)
    vus: list = []
    monkeypatch.setattr(at.db, "create_api_token",
                        lambda sub, **kw: vus.append(kw) or "oto_ENFANT")
    monkeypatch.setattr(at.org_store, "org_du_compte_de_service",
                        lambda sub: 2 if sub == "org-2" else None)

    class _Store:
        def list_datastores(self):
            return [{"id": 12, "datastore": "clients"}]

    monkeypatch.setattr(datastore, "make_store", lambda sub: _Store())
    token_scopes.set_current({"issue": {"namespaces": {"12": "write"}}}, 41)
    code, out = call("me.token.create", sub="org-2",
                     body={"scopes": {"namespaces": {"12": "write"}}, "ttl_days": 30})
    assert code == 201, out
    assert (vus[-1]["verrou_org"], vus[-1]["verrou_org_id"]) == (True, 2)


# --- 3. La base -----------------------------------------------------------------

@pytest.fixture()
def base(pg_module_dsn, monkeypatch):
    import psycopg
    from psycopg.rows import dict_row

    monkeypatch.setenv("DATABASE_URL", pg_module_dsn)
    from oto_mcp.db import _conn
    monkeypatch.setattr(_conn, "_database_url", lambda: pg_module_dsn)
    _conn._pool = None
    from oto_mcp import db, org_store
    db.init_db()

    def sql(q, params=None):
        with psycopg.connect(pg_module_dsn, row_factory=dict_row, autocommit=True) as c:
            cur = c.execute(q, params)
            return cur.fetchall() if cur.description else None

    yield db, org_store, sql
    _conn._pool = None


def test_le_compte_est_membre_de_son_org_sans_ligne_de_membre(base):
    db, org_store, sql = base
    org = org_store.create_org("Clés d'org A")
    sub = org_store.assurer_compte_de_service(org, by="admin-1")
    assert sub == f"org-{org}"
    assert org_store.assurer_compte_de_service(org, by="admin-2") == sub, "idempotent"
    assert org_store.get_org_role(org, sub) == "org_member"
    assert org_store.get_active_org(sub) == org
    assert [o["org_id"] for o in org_store.list_orgs_for_user(sub)] == [org]
    assert org_store.org_du_compte_de_service(sub) == org
    assert org_store.compte_de_service_de(org) == sub
    assert sql("SELECT 1 FROM org_members WHERE sub = %s", (sub,)) == []
    autre = org_store.create_org("Clés d'org B")
    assert org_store.get_org_role(autre, sub) is None


def test_le_compte_ne_recoit_pas_d_org_perso(base):
    db, org_store, sql = base
    org = org_store.create_org("Clés d'org C")
    sub = org_store.assurer_compte_de_service(org, by="admin-1")
    org_store.backfill_personal_orgs()
    db.create_api_token(sub, "k", scopes={"namespaces": {"1": "read"}}, kind="org")
    # Aucune org posée pour lui : sa seule org est la sienne (le compte de service).
    assert [int(o["org_id"]) for o in org_store.list_orgs_for_user(sub)] == [org]
    assert org_store.home_org(sub) == org
    assert org_store.get_active_org(sub) == org


def test_le_compte_d_une_org_de_tenant_porte_son_prefixe(base):
    db, org_store, sql = base
    org = org_store.create_org("Clés d'org D")
    (t,) = sql("INSERT INTO tenants (slug, name) VALUES ('tiers-cles', 'Tiers') "
               "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name RETURNING id")
    sql("UPDATE orgs SET tenant_id = %s WHERE id = %s", (t["id"], org))
    assert org_store.assurer_compte_de_service(org, by="a") == f"tiers-cles:org-{org}"


def test_archiver_l_org_revoque_ses_cles(base):
    db, org_store, sql = base
    org = org_store.create_org("Clés d'org E")
    sub = org_store.assurer_compte_de_service(org, by="admin-1")
    cle = db.create_api_token(sub, "k", scopes={"namespaces": {"1": "read"}}, kind="org",
                              verrou_org=True, verrou_org_id=org, created_by="admin-1")
    assert db.verify_api_token(cle)["verrou_org_id"] == org
    (ligne,) = db.list_api_tokens(sub, kinds=("org", "user"))
    assert ligne["created_by"] == "admin-1"
    assert org_store.archive_org(org)
    assert db.verify_api_token(cle) is None
    (ligne,) = db.list_api_tokens(sub, include_revoked=True, kinds=("org",))
    assert ligne["revoked_reason"] == "org archivée"
    with pytest.raises(ValueError):
        org_store.assurer_compte_de_service(org, by="admin-1")
