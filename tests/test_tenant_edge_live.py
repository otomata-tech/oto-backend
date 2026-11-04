"""L-clés, PR 2 — l'arête tenant→org et le rôle d'admin de tenant, contre PostgreSQL.

Base DÉDIÉE, DDL réel. Ce que le vrai SQL doit prouver, que les stubs ne prouvent pas :

1. **L'arête borne et débite.** Une org accordée avec `quota=2` sert deux appels, le
   troisième est refusé — et le compteur est celui de l'ARÊTE (`grant_counters`),
   sommé sur l'org (R10 : budget partagé, pas par membre).
2. **Révoquer coupe sans repli.** L'org retombe sur ce qui reste (ici : rien).
3. **L'anonyme n'obtient l'étage que par l'arête** — jamais par le rattachement.
4. **Le rôle d'admin de tenant se lit en base et sur le sub qualifié**, et la table
   suit `migrate_sub` (PK, pas d'UPDATE nu).
"""
from __future__ import annotations

import os
import uuid

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import access, grants_chain, tenancy, tenant_vault
from oto_mcp.capabilities import _authz
from oto_mcp.capabilities._authz import SUPER_ADMIN
from oto_mcp.capabilities._types import AuthzDenied, RawCtx
from oto_mcp.db import grants as db_grants

PILOTE = "pilote"
ISSUER = "https://auth.pilote.test/oidc"
ORG, AUTRE_ORG = 4343, 4344
SUB_T, SUB_T2 = f"{PILOTE}:usr_t", f"{PILOTE}:usr_t2"
CONN = "hunter"


def _exec(sql, params=()):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute(sql, params)


def _one(sql, params=()):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute(sql, params).fetchone()


@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_lcles2_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    prev_url, prev_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    prev_key = os.environ.get("OTO_MCP_MASTER_KEY")
    prev_registry = tenancy.current()
    os.environ["DATABASE_URL"] = dsn
    os.environ.setdefault("OTO_MCP_MASTER_KEY", "0" * 64)
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        _exec("INSERT INTO tenants (slug, name, issuer) VALUES (%s, 'Pilote', %s)",
              (PILOTE, ISSUER))
        for o in (ORG, AUTRE_ORG):
            _exec("INSERT INTO orgs (id, name) VALUES (%s, 'o') ON CONFLICT DO NOTHING", (o,))
        for s in (SUB_T, SUB_T2):
            _exec("INSERT INTO users (sub) VALUES (%s) ON CONFLICT DO NOTHING", (s,))
        tenancy.install(tenancy.IssuerRegistry(tenancy.build(
            "https://auth.oto.ninja/oidc", tenants=[{"slug": PILOTE, "issuer": ISSUER}])))
        yield
    finally:
        tenancy.install(prev_registry)
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = prev_pool
        if prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_url
        if prev_key is None:
            os.environ.pop("OTO_MCP_MASTER_KEY", None)
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


@pytest.fixture
def cle_tenant(live):
    _exec("DELETE FROM grant_counters")
    _exec("DELETE FROM grants")
    _exec("DELETE FROM connector_instances")
    _exec("DELETE FROM connector_credentials")
    _exec("DELETE FROM tenant_admins")
    tenant_vault.set_tenant_secret(PILOTE, CONN, "k-tenant", set_by="operateur")
    yield


@pytest.fixture
def contexte(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access, "project_pinned_instance", lambda p: None)


# ── 1. l'arête borne et débite ────────────────────────────────────────────────

def test_l_arete_borne_le_jour_de_l_org_et_debite_l_arete(cle_tenant, contexte):
    gid = grants_chain.tenant_grant(PILOTE, CONN, ORG, 2, created_by="operateur")
    rc = access.resolve_credential(CONN, sub=SUB_T)
    assert (rc.key, rc.mode) == ("k-tenant", "tenant")
    rc = access.resolve_credential(CONN, sub=SUB_T2)          # un AUTRE membre : même budget
    assert rc.mode == "tenant"
    assert db_grants.counter_sum_today(grants_chain.tenant_ref(PILOTE, CONN), "org", str(ORG)) == 2
    with pytest.raises(McpError) as e:
        access.resolve_credential(CONN, sub=SUB_T)
    assert "2/2" in str(e.value) and PILOTE in str(e.value)
    grants = grants_chain.tenant_org_grants(PILOTE, CONN)
    assert [(g["org_id"], g["daily_quota"], g["used_today"], g["grant_id"]) for g in grants] == [
        (ORG, 2, 2, gid)]


def test_re_poser_l_arete_archive_la_precedente_et_garde_la_consommation(cle_tenant, contexte):
    grants_chain.tenant_grant(PILOTE, CONN, ORG, 1, created_by="operateur")
    access.resolve_credential(CONN, sub=SUB_T)
    with pytest.raises(McpError):
        access.resolve_credential(CONN, sub=SUB_T)
    grants_chain.tenant_grant(PILOTE, CONN, ORG, 3, created_by="operateur")   # relevé
    assert _one("SELECT count(*) AS n FROM grants WHERE revoked_at IS NULL")["n"] == 1
    assert _one("SELECT count(*) AS n FROM grants")["n"] == 2                  # archivée, pas supprimée
    rc = access.resolve_credential(CONN, sub=SUB_T)                            # 1 (archivée) + 1 < 3
    assert rc.mode == "tenant"
    # D7 : la consommation se somme sur les arêtes, ARCHIVÉES comprises.
    assert db_grants.counter_sum_today(grants_chain.tenant_ref(PILOTE, CONN), "org", str(ORG)) == 2


# ── 2. révoquer coupe ─────────────────────────────────────────────────────────

def test_revoquer_coupe_l_org_sans_repli(cle_tenant, contexte):
    grants_chain.tenant_grant(PILOTE, CONN, ORG, None, created_by="operateur")
    assert access.resolve_credential(CONN, sub=SUB_T).mode == "tenant"
    assert grants_chain.tenant_revoke(PILOTE, CONN, ORG) == 1
    with pytest.raises(McpError):
        access.resolve_credential(CONN, sub=SUB_T)
    assert access.status_for(SUB_T, org=ORG, group=None)["providers"][CONN]["mode"] == "forbidden"
    # Une AUTRE org du tenant, sans arête (état MUET), garde la clé — PR 1.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(access, "current_org", lambda sub: AUTRE_ORG)
        assert access.resolve_credential(CONN, sub=SUB_T).mode == "tenant"


# ── 3. l'anonyme, par l'arête seule ───────────────────────────────────────────

def test_l_anonyme_ne_recoit_la_cle_que_par_une_arete_vivante(cle_tenant):
    with pytest.raises(McpError):
        access._resolve_credential_anon(CONN, "auto", ORG)
    grants_chain.tenant_grant(PILOTE, CONN, ORG, 1, created_by="operateur")
    rc = access._resolve_credential_anon(CONN, "auto", ORG)
    assert (rc.key, rc.mode, rc.entity_id) == ("k-tenant", "tenant", PILOTE)
    with pytest.raises(McpError):                                # le budget vaut aussi pour lui
        access._resolve_credential_anon(CONN, "auto", ORG)
    with pytest.raises(McpError):                                # une autre org : rien
        access._resolve_credential_anon(CONN, "auto", AUTRE_ORG)


# ── 4. le rôle d'admin de tenant ──────────────────────────────────────────────

def test_le_role_se_lit_en_base_sur_le_sub_qualifie(cle_tenant, monkeypatch):
    from oto_mcp import db
    monkeypatch.setattr(access, "is_super_admin", lambda sub: False)
    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(access, "get_user_role", lambda sub: "member")
    rule = _authz.TENANT_ADMIN_OF("slug", platform=SUPER_ADMIN)

    class _Inp:
        slug = PILOTE
    with pytest.raises(AuthzDenied):
        rule(RawCtx(sub=SUB_T), _Inp())
    db.add_tenant_admin(PILOTE, SUB_T, granted_by="operateur")
    db.add_tenant_admin(PILOTE, SUB_T, granted_by="quelqu-un-d-autre")   # idempotent
    assert rule(RawCtx(sub=SUB_T), _Inp()).sub == SUB_T
    assert [a["sub"] for a in db.list_tenant_admins(PILOTE)] == [SUB_T]
    assert _one("SELECT granted_by FROM tenant_admins")["granted_by"] == "operateur"
    assert db.remove_tenant_admin(PILOTE, SUB_T) is True
    assert db.remove_tenant_admin(PILOTE, SUB_T) is False
    with pytest.raises(AuthzDenied):
        rule(RawCtx(sub=SUB_T), _Inp())


# ── 5. un bénéficiaire HORS de l'org (projet partagé, #1499 D1) ───────────────
# Trou relevé à la conception de l'accès invité : `tenant_rung` lisait l'org de
# CONTEXTE, que `_project=` co-pose sans appartenance. Un bénéficiaire hors de l'org
# puisait dans le budget de l'arête tenant→org de celle-ci (un droit de l'org, qu'on
# ne lui a pas prêté) et perdait sa propre clé de tenant quand l'org en était
# révoquée. L'arête se lit sur l'org dont l'appelant est MEMBRE
# (`scope.org_d_appartenance`) ; rien ne la lui prête (l'héritage #480 est retiré).

def _hors_org():
    from oto_mcp import session_org
    return session_org.set_call_hors_org(SUB_T, ORG)


def _sous_verdict(jeton, fn):
    from oto_mcp import session_org
    try:
        return fn()
    finally:
        session_org.reset_call_hors_org(jeton)


def test_hors_org_il_ne_puise_pas_dans_le_budget_de_l_org(cle_tenant, contexte):
    grants_chain.tenant_grant(PILOTE, CONN, ORG, 1, created_by="operateur")
    modes = _sous_verdict(_hors_org(), lambda: [
        access.resolve_credential(CONN, sub=SUB_T).mode for _ in range(2)])
    assert modes == ["tenant", "tenant"], "sa clé de tenant, hors du budget de l'org"
    assert db_grants.counter_sum_today(
        grants_chain.tenant_ref(PILOTE, CONN), "org", str(ORG)) == 0


def test_hors_org_la_revocation_de_l_org_ne_lui_retire_pas_sa_cle(cle_tenant, contexte):
    grants_chain.tenant_grant(PILOTE, CONN, ORG, None, created_by="operateur")
    grants_chain.tenant_revoke(PILOTE, CONN, ORG)
    rc = _sous_verdict(_hors_org(), lambda: access.resolve_credential(CONN, sub=SUB_T))
    assert (rc.key, rc.mode) == ("k-tenant", "tenant")
