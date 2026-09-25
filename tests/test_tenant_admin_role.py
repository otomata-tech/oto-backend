"""L-clés, PR 2 — le rôle « admin de tenant » (ADR 0052, régime transitoire du 27/08).

Le partenaire pose et retire la clé de SON tenant et voit ses orgs, sans être admin
de la plateforme. Ce que ce fichier tient :

1. **Le rôle se lit sur le sub qualifié, jamais sur le rattachement d'org** : un admin
   déclaré sur `pilote` doit être un compte `pilote:…` — un compte nu (tenant `oto`)
   ou d'un autre tenant est refusé à la déclaration ET à l'appel.
2. **Le tenant primaire n'a pas d'admin de tenant** : ses admins sont ceux de la
   plateforme.
3. **La console MCP garde son plancher opérateur.** Un plancher `None` la ferait
   entrer dans le handshake de chaque compte ; l'admin de tenant agit par la face
   REST (son tableau de bord).
4. **Le rôle est additif et réversible** : une table neuve, une règle d'autz neuve,
   rien de retiré — et le retrait du rôle ramène à #603.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from oto_mcp import access, tenancy
from oto_mcp.capabilities import _authz, tenant_admins as tad, tenants_admin as ta
from oto_mcp.capabilities._authz import PLATFORM_ADMIN, SUPER_ADMIN
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx
from oto_mcp.capabilities.registry import CAPABILITIES
from oto_mcp.db import _schema

PILOTE = "pilote"
ADMIN_T = f"{PILOTE}:admin"
MEMBRE_T = f"{PILOTE}:membre"
AUTRE_T = "autre:admin"
NU = "usr_nu"
SUPER = ResolvedCtx(sub="operateur", role="super_admin")


def _cap(key: str):
    return next(c for c in CAPABILITIES if c.key == key)


@pytest.fixture
def registre(monkeypatch):
    monkeypatch.setattr(tenancy, "_INSTALLED", tenancy.IssuerRegistry(tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": PILOTE, "issuer": "https://auth.pilote.test/oidc"},
                 {"slug": "autre", "issuer": "https://auth.autre.test/oidc"}])),
        raising=False)


@pytest.fixture
def personne_plateforme(monkeypatch):
    monkeypatch.setattr(access, "is_super_admin", lambda sub: False)
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: False)
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(access, "get_user_role", lambda sub: "member")


@pytest.fixture
def admins(monkeypatch):
    """Les admins déclarés, en mémoire."""
    etat = {(PILOTE, ADMIN_T)}
    monkeypatch.setattr(_authz.db, "is_tenant_admin", lambda slug, sub: (slug, sub) in etat)
    monkeypatch.setattr(tad.db, "tenant_exists", lambda slug: slug in (PILOTE, "autre", "oto"))
    monkeypatch.setattr(tad.db, "list_tenant_admins",
                        lambda slug: [{"sub": s, "granted_by": "operateur", "granted_at": None}
                                      for t, s in sorted(etat) if t == slug])
    monkeypatch.setattr(tad.db, "add_tenant_admin",
                        lambda slug, sub, granted_by=None: etat.add((slug, sub)))
    monkeypatch.setattr(tad.db, "remove_tenant_admin",
                        lambda slug, sub: (etat.discard((slug, sub)) or True)
                        if (slug, sub) in etat else False)
    return etat


# ── 1. le DDL ─────────────────────────────────────────────────────────────────

def test_la_table_des_admins_de_tenant_suit_tenants_et_porte_sa_pk():
    src = _schema._SCHEMA
    assert src.index("CREATE TABLE IF NOT EXISTS tenants") < src.index(
        "CREATE TABLE IF NOT EXISTS tenant_admins"), "FK vers tenants : l'ordre compte"
    bloc = src[src.index("CREATE TABLE IF NOT EXISTS tenant_admins"):]
    bloc = bloc[:bloc.index("\n);")]
    assert "PRIMARY KEY (slug, sub)" in bloc
    assert "REFERENCES tenants(slug)" in bloc


def test_la_colonne_sub_des_admins_est_triee_par_migrate_sub():
    from oto_mcp.db.users import _PK_SUB_TABLES, _SUB_COLUMNS
    assert ("tenant_admins", "sub", ("slug",)) in _PK_SUB_TABLES
    assert ("tenant_admins", "granted_by") in _SUB_COLUMNS


# ── 2. la règle d'autz ────────────────────────────────────────────────────────

def test_l_admin_de_son_tenant_passe_les_autres_non(registre, personne_plateforme, admins):
    rule = _authz.TENANT_ADMIN_OF("slug", platform=SUPER_ADMIN)
    assert rule(RawCtx(sub=ADMIN_T), SimpleNamespace(slug=PILOTE)).sub == ADMIN_T
    for sub in (MEMBRE_T, AUTRE_T, NU):
        with pytest.raises(AuthzDenied) as e:
            rule(RawCtx(sub=sub), SimpleNamespace(slug=PILOTE))
        assert e.value.status == 403, sub


def test_la_regle_plateforme_prime(registre, admins, monkeypatch):
    """Un super admin passe sans être admin du tenant — la règle `platform` est
    essayée d'abord, et son refus (403) laisse la main au rôle de tenant."""
    monkeypatch.setattr(access, "is_super_admin", lambda sub: sub == "operateur")
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(access, "get_user_role", lambda sub: "super_admin")
    rule = _authz.TENANT_ADMIN_OF("slug", platform=SUPER_ADMIN)
    assert rule(RawCtx(sub="operateur"), SimpleNamespace(slug=PILOTE)).sub == "operateur"


def test_un_admin_de_tenant_ne_passe_pas_sur_un_autre_tenant(registre, personne_plateforme,
                                                              admins):
    rule = _authz.TENANT_ADMIN_OF("slug", platform=SUPER_ADMIN)
    with pytest.raises(AuthzDenied) as e:
        rule(RawCtx(sub=ADMIN_T), SimpleNamespace(slug="autre"))
    assert e.value.status == 403


def test_sans_slug_la_regle_refuse_en_400(registre, personne_plateforme, admins):
    rule = _authz.TENANT_ADMIN_OF("slug", platform=SUPER_ADMIN)
    with pytest.raises(AuthzDenied) as e:
        rule(RawCtx(sub=ADMIN_T), SimpleNamespace(slug=None))
    assert e.value.status == 400 and e.value.code == "missing_slug"


def test_la_regle_n_a_pas_de_plancher_plateforme():
    """Le rôle dépend d'une CIBLE (le tenant) que le handshake ne connaît pas."""
    assert _authz.platform_floor(_authz.TENANT_ADMIN_OF("slug", platform=SUPER_ADMIN)) is None


# ── 3. les surfaces ───────────────────────────────────────────────────────────

def test_les_cles_et_la_fiche_du_tenant_acceptent_son_admin(registre, personne_plateforme,
                                                             admins):
    for key in ("admin.tenant", "admin.tenant_keys", "admin.tenant_key_set",
                "admin.tenant_key_clear", "admin.tenant_apps", "admin.tenant_app_set",
                "admin.tenant_app_clear", "admin.tenant_connectors",
                "admin.tenant_connector_set", "admin.tenant_connector_clear",
                "admin.tenant_org_grants",
                "admin.tenant_org_grant", "admin.tenant_org_revoke", "admin.tenant_admins"):
        rule = _cap(key).authz
        assert rule(RawCtx(sub=ADMIN_T), SimpleNamespace(slug=PILOTE)).sub == ADMIN_T, key
        with pytest.raises(AuthzDenied):
            rule(RawCtx(sub=MEMBRE_T), SimpleNamespace(slug=PILOTE))


def test_declarer_un_admin_reste_super_admin(registre, personne_plateforme, admins):
    for key in ("admin.tenant_admin_add", "admin.tenant_admin_remove"):
        cap = _cap(key)
        assert cap.authz is SUPER_ADMIN, key
    assert (_cap("admin.tenant_admin_add").rest.verb,
            _cap("admin.tenant_admin_add").rest.path) == ("POST", "/api/admin/tenants/{slug}/admins")
    assert (_cap("admin.tenant_admin_remove").rest.verb,
            _cap("admin.tenant_admin_remove").rest.path) == (
        "DELETE", "/api/admin/tenants/{slug}/admins/{sub}")
    assert (_cap("admin.tenant_admins").rest.verb,
            _cap("admin.tenant_admins").rest.path) == ("GET", "/api/admin/tenants/{slug}/admins")


def test_la_console_mcp_garde_son_plancher_operateur():
    """Le plancher d'un combinateur est le plus BAS de ses branches : une seule op
    au rôle de tenant ferait entrer `oto_admin_tenant` dans le handshake de chaque
    compte de la plateforme. L'admin de tenant agit par la face REST."""
    assert _authz.platform_floor(_cap("admin.tenant_console").authz) == "operator"


# ── 4. les gardes de déclaration ──────────────────────────────────────────────

def test_declarer_un_compte_hors_du_tenant_est_refuse(registre, admins):
    for sub, code in ((NU, "sub_not_of_tenant"), (AUTRE_T, "sub_not_of_tenant")):
        with pytest.raises(AuthzDenied) as e:
            tad._add(SUPER, tad.TenantAdminAddInput(slug=PILOTE, sub=sub))
        assert e.value.status == 400 and e.value.code == code, sub
    assert (PILOTE, NU) not in admins and (PILOTE, AUTRE_T) not in admins


def test_le_tenant_primaire_n_a_pas_d_admin_de_tenant(registre, admins):
    with pytest.raises(AuthzDenied) as e:
        tad._add(SUPER, tad.TenantAdminAddInput(slug="oto", sub=NU))
    assert e.value.status == 400 and e.value.code == "primary_tenant"


def test_un_slug_inconnu_est_un_404(registre, admins):
    with pytest.raises(AuthzDenied) as e:
        tad._add(SUPER, tad.TenantAdminAddInput(slug="fantome", sub="fantome:x"))
    assert e.value.status == 404


def test_declarer_lister_retirer(registre, admins):
    out = tad._add(SUPER, tad.TenantAdminAddInput(slug=PILOTE, sub=MEMBRE_T))
    assert out == {"ok": True, "slug": PILOTE, "sub": MEMBRE_T}
    assert [a["sub"] for a in tad._list(SUPER, tad.TenantAdminsInput(slug=PILOTE))["admins"]] == [
        ADMIN_T, MEMBRE_T]
    out = tad._remove(SUPER, tad.TenantAdminRemoveInput(slug=PILOTE, sub=MEMBRE_T))
    assert out == {"ok": True, "slug": PILOTE, "sub": MEMBRE_T, "removed": True}
    out = tad._remove(SUPER, tad.TenantAdminRemoveInput(slug=PILOTE, sub=MEMBRE_T))
    assert out["removed"] is False                       # idempotent


def test_la_console_dispatch_admins(registre, admins):
    out = ta._console(SUPER, ta.TenantConsoleInput(op="admins", slug=PILOTE))
    assert [a["sub"] for a in out["admins"]["admins"]] == [ADMIN_T]
    out = ta._console(SUPER, ta.TenantConsoleInput(op="admin_add", slug=PILOTE, sub=MEMBRE_T))
    assert out["admin_add"]["ok"] is True
    out = ta._console(SUPER, ta.TenantConsoleInput(op="admin_remove", slug=PILOTE, sub=MEMBRE_T))
    assert out["admin_remove"]["removed"] is True
    with pytest.raises(AuthzDenied) as e:
        ta._console(SUPER, ta.TenantConsoleInput(op="admin_add", slug=PILOTE))
    assert e.value.code == "missing_sub"


def test_la_console_admin_add_est_super_admin(monkeypatch):
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: True)
    monkeypatch.setattr(access, "is_super_admin", lambda sub: False)
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(access, "get_user_role", lambda sub: "admin")
    rule = _cap("admin.tenant_console").authz
    assert rule(RawCtx(sub="op"), SimpleNamespace(op="admins")).sub == "op"
    for op in ("admin_add", "admin_remove", "org_grant", "org_revoke"):
        with pytest.raises(AuthzDenied) as ei:
            rule(RawCtx(sub="op"), SimpleNamespace(op=op))
        assert ei.value.status == 403, op
