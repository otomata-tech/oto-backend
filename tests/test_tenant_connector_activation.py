"""Le plafond d'activation d'un TENANT (2026-09-26) — le cran entre la plateforme et l'org.

Un partenaire qui sert oto sous sa marque n'offre pas tout le catalogue (un service
Google que son projet Google Cloud ne déclare pas). Ses deux leviers étaient faux :
le master (coupe pour tout le monde) et l'override d'org (une ligne par org, et
qu'un admin d'org défait). Le cran tenant est un PLAFOND : coupé là, rien en dessous
ne rouvre ; à `true`, il n'expose rien que la plateforme ne donne pas.

Deux bancs : la résolution contre un vrai PostgreSQL (`exposed_connectors`,
`cran_qui_coupe` — c'est du SQL, un stub validerait la forme et laisserait passer
une ligne tenant lue sous le mauvais scope), et la face admin, scopée au slug.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from oto_mcp import access
from oto_mcp.capabilities import _authz
from oto_mcp.capabilities import tenant_connectors as tc
from oto_mcp.capabilities import tenant_keys as tk
from oto_mcp.capabilities.connectors import activation as cap_act
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx
from oto_mcp.capabilities.registry import CAPABILITIES
from oto_mcp.connectors import activation as act

CTX = ResolvedCtx(sub="operateur", role="super_admin")
TULINA = "tulina"


def _cap(key: str):
    return next(c for c in CAPABILITIES if c.key == key)


# ─── 1. la résolution, contre PostgreSQL ──────────────────────────────────────

@pytest.fixture()
def base(pg_module_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_module_dsn)
    monkeypatch.setenv("OTO_CONFIG_DISABLE_SOPS", "1")
    from oto_mcp.db import _conn
    monkeypatch.setattr(_conn, "_database_url", lambda: pg_module_dsn)
    _conn._pool = None
    from oto_mcp import db
    db.init_db()
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    with psycopg.connect(pg_module_dsn, row_factory=dict_row, autocommit=True) as c:
        c.execute("DELETE FROM connector_availability")
        c.execute("INSERT INTO connector_availability (scope_type, scope_id, connector, enabled) "
                  "VALUES ('platform', '', 'gmail', TRUE), ('platform', '', 'chat', TRUE), "
                  "       ('platform', '', 'serper', TRUE), "
                  "       ('tenant', 'tulina', 'chat', FALSE), "
                  "       ('org', '7', 'chat', TRUE), "          # l'org 7 avait forcé ON
                  "       ('org', '7', 'serper', FALSE)")
        yield c


def _org_du_tenant(monkeypatch, par_org: dict):
    from oto_mcp import db
    monkeypatch.setattr(db, "org_tenant_slug", lambda org_id: par_org.get(int(org_id), "oto"))


def test_le_tenant_coupe_pour_ses_orgs_et_pas_pour_les_autres(base, monkeypatch):
    _org_du_tenant(monkeypatch, {7: TULINA, 8: TULINA, 9: "oto"})
    assert act.exposed_connectors(8) == {"gmail", "serper"}          # chat coupé par le tenant
    assert act.exposed_connectors(7) == {"gmail"}                    # + serper coupé par l'org ;
    #                                                                  chat : l'override ON ne rouvre pas
    assert act.exposed_connectors(9) == {"gmail", "chat", "serper"}  # une org d'oto ne voit rien
    assert act.exposed_connectors(None) == {"gmail", "chat", "serper"}


def test_le_cran_qui_coupe_nomme_le_tenant(base, monkeypatch):
    _org_du_tenant(monkeypatch, {7: TULINA, 9: "oto"})
    assert act.cran_qui_coupe("chat", 7) == "tenant"     # même avec l'override d'org ON
    assert act.cran_qui_coupe("serper", 7) == "org"
    assert act.cran_qui_coupe("gmail", 7) is None
    assert act.cran_qui_coupe("chat", 9) is None
    assert act.is_exposed("chat", org_id=None)


def test_la_ligne_tenant_se_pose_se_lit_et_se_retire(base, monkeypatch):
    _org_du_tenant(monkeypatch, {8: TULINA})
    act.set_tenant_activation(TULINA, "gmail", False, set_by="admin")
    assert act.list_tenant_activations(TULINA) == {"chat": False, "gmail": False}
    assert act.exposed_connectors(8) == {"serper"}
    act.set_tenant_activation(TULINA, "gmail", True, set_by="admin")
    assert act.exposed_connectors(8) == {"gmail", "serper"}
    act.clear_tenant_activation(TULINA, "chat")
    assert act.list_tenant_activations(TULINA) == {"gmail": True}
    assert act.exposed_connectors(8) == {"gmail", "chat", "serper"}


def test_une_ligne_tenant_a_true_nexpose_pas_au_dela_de_la_plateforme(base, monkeypatch):
    _org_du_tenant(monkeypatch, {8: TULINA})
    act.set_tenant_activation(TULINA, "hunter", True)     # hunter : aucun master
    assert "hunter" not in act.exposed_connectors(8)


# ─── 2. la face admin, scopée au slug ─────────────────────────────────────────

@pytest.fixture()
def tenants(monkeypatch):
    monkeypatch.setattr(tk.db, "tenant_exists", lambda slug: slug in (TULINA, "pilote", "oto"))
    # Le rôle d'admin de tenant se lit sur le sub QUALIFIÉ, donc sur le registre.
    from oto_mcp import tenancy
    entree = tenancy.TenantIssuer(slug=TULINA, issuer="https://auth.tulina.ai/oidc",
                                  jwks_uri="https://auth.tulina.ai/oidc/jwks")
    pilote = tenancy.TenantIssuer(slug="pilote", issuer="https://auth.pilote.test/oidc",
                                  jwks_uri="https://auth.pilote.test/oidc/jwks")
    monkeypatch.setattr(tenancy, "_INSTALLED", tenancy.IssuerRegistry(
        {entree.issuer: entree, pilote.issuer: pilote}))


def test_les_trois_capacites_et_leurs_planchers(tenants, monkeypatch):
    for key, verbe, chemin in (
            ("admin.tenant_connectors", "GET", "/api/admin/tenants/{slug}/connectors/activation"),
            ("admin.tenant_connector_set", "PUT",
             "/api/admin/tenants/{slug}/connectors/activation/{name}"),
            ("admin.tenant_connector_clear", "DELETE",
             "/api/admin/tenants/{slug}/connectors/activation/{name}")):
        cap = _cap(key)
        assert (cap.rest.verb, cap.rest.path) == (verbe, chemin)
        assert cap.Output is not None
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: False)
    monkeypatch.setattr(access, "is_super_admin", lambda sub: False)
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(access, "get_user_role", lambda sub: "member")
    monkeypatch.setattr(_authz.db, "is_tenant_admin",
                        lambda slug, sub: (slug, sub) == (TULINA, "tulina:admin"))
    for key in ("admin.tenant_connectors", "admin.tenant_connector_set",
                "admin.tenant_connector_clear"):
        rule = _cap(key).authz
        assert rule(RawCtx(sub="tulina:admin"), SimpleNamespace(slug=TULINA)).sub == "tulina:admin"
        with pytest.raises(AuthzDenied):
            rule(RawCtx(sub="tulina:admin"), SimpleNamespace(slug="pilote"))
        with pytest.raises(AuthzDenied):
            rule(RawCtx(sub="nu-sub"), SimpleNamespace(slug=TULINA))


def test_la_liste_montre_le_plafond_et_la_ligne_du_tenant(tenants, monkeypatch):
    monkeypatch.setattr(act, "list_activations", lambda: [
        {"connector": "serper", "org_id": None, "enabled": True},
        {"connector": "google", "org_id": None, "enabled": True},
        {"connector": "hunter", "org_id": None, "enabled": False},
        {"connector": "google", "org_id": 7, "enabled": True}])
    monkeypatch.setattr(act, "list_tenant_activations",
                        lambda slug: {"google": False, "hunter": False} if slug == TULINA else {})
    out = tc._list(CTX, tc.TenantConnectorsInput(slug=TULINA))
    par = {r["connector"]: r for r in out["connectors"]}
    assert par["serper"]["effective"] is True and par["serper"]["tenant_enabled"] is None
    assert par["google"]["effective"] is False and par["google"]["tenant_enabled"] is False
    # Master OFF mais ligne tenant posée : listé, pour rester retirable.
    assert par["hunter"]["effective"] is False and par["hunter"]["master_enabled"] is False
    assert "slack" not in par                                    # jamais posé : invisible


def test_couper_et_rouvrir_sous_le_plafond_plateforme(tenants, monkeypatch):
    vu = []
    monkeypatch.setattr(act, "set_tenant_activation",
                        lambda slug, name, enabled, set_by=None: vu.append((slug, name, enabled, set_by)))
    monkeypatch.setattr(act, "is_exposed", lambda name, org_id=None: name == "serper")
    out = tc._set(CTX, tc.TenantConnectorSetInput(slug=TULINA, name="google", enabled=False))
    assert out == {"slug": TULINA, "connector": "google", "enabled": False}
    assert vu == [(TULINA, "google", False, "operateur")]
    with pytest.raises(AuthzDenied) as e:      # rouvrir ce que la plateforme n'expose pas
        tc._set(CTX, tc.TenantConnectorSetInput(slug=TULINA, name="google", enabled=True))
    assert e.value.code == "platform_disabled"
    tc._set(CTX, tc.TenantConnectorSetInput(slug=TULINA, name="serper", enabled=True))
    assert vu[-1] == (TULINA, "serper", True, "operateur")


def test_le_primaire_et_un_connecteur_inconnu_sont_refuses(tenants, monkeypatch):
    with pytest.raises(AuthzDenied) as e:
        tc._set(CTX, tc.TenantConnectorSetInput(slug="oto", name="serper", enabled=False))
    assert e.value.code == "primary_tenant_activation"
    with pytest.raises(AuthzDenied) as e:
        tc._clear(CTX, tc.TenantConnectorClearInput(slug=TULINA, name="fantome"))
    assert e.value.status == 404


def test_une_org_ne_rouvre_pas_ce_que_son_tenant_a_coupe(monkeypatch):
    """Le cockpit d'org : le plafond se voit (`tenant_enabled`), et la pose ON
    est refusée — le geste est chez l'hébergeur."""
    monkeypatch.setattr(act, "tenant_of_org", lambda org_id: TULINA)
    monkeypatch.setattr(act, "list_tenant_activations", lambda slug: {"google": False})
    monkeypatch.setattr(act, "is_exposed", lambda name, org_id=None: True)
    with pytest.raises(AuthzDenied) as e:
        cap_act._org_set(CTX, cap_act.OrgActivationSetInput(org_id=7, name="google", enabled=True))
    assert e.value.code == "tenant_disabled"
    from oto_mcp import org_store
    monkeypatch.setattr(org_store, "get_org", lambda oid: {"id": oid})
    monkeypatch.setattr(org_store, "get_org_default_connectors", lambda oid: [])
    monkeypatch.setattr(act, "list_activations", lambda: [
        {"connector": "google", "org_id": None, "enabled": True},
        {"connector": "google", "org_id": 7, "enabled": True}])
    monkeypatch.setattr(access, "paid_option_for", lambda name: None)
    rows = cap_act._org_list(CTX, cap_act.OrgActivationListInput(org_id=7))["connectors"]
    chat = next(r for r in rows if r["connector"] == "google")
    assert chat["org_enabled"] is True and chat["tenant_enabled"] is False
    assert chat["effective"] is False
