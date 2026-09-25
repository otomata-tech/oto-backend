"""Suivi des tenants (console plateforme) — `capabilities/tenants_admin.py`.

Ce que ces tests tiennent, dans l'ordre de ce qui coûterait le plus cher à rater :

1. **L'autz est PLATFORM_ADMIN sur les TROIS surfaces.** Le suivi expose la
   configuration d'annuaire des partenaires et la volumétrie de toute la plateforme :
   une surface qui retomberait en `SUB_ONLY` la donnerait à n'importe quel compte.
2. **Le suivi ne peut RIEN écrire.** Déclarer un tenant est un runbook (instance
   Logto dédiée, client OAuth, hosts) et le registre est bâti au boot : une capacité
   d'écriture ferait croire qu'une ligne en base suffit. Le test le vérifie sur le
   registre, pas sur une intention — n'importe quel futur `admin.tenant.*` porteur
   d'un verbe mutant tombe.
3. **L'écart base ↔ process est RENDU** (`loaded` / `pending_restart`) : un tenant
   déclaré mais non redémarré voit ses jetons rejetés tout en paraissant prêt. C'est
   le diagnostic pour lequel cet écran existe.
4. Le dispatch de la console (`op=get` sans slug, 404 d'un slug inconnu, écrêtage de
   la fenêtre) — un 500 sur `days=-1` était le mode de panne d'#300.

Le SQL des compteurs, lui, ne se stube pas : il est exercé contre un vrai PostgreSQL
dans `test_tenants_overview_pg.py` (sauté sans base).
"""
from __future__ import annotations

import pytest

from oto_mcp import tenancy
from oto_mcp.capabilities import tenants_admin as ta
from oto_mcp.capabilities._authz import PLATFORM_ADMIN
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.registry import CAPABILITIES
from oto_mcp.db import tenants as tenants_db

CTX = ResolvedCtx(sub="operateur", role="super_admin")

# Les lignes stubbées passent par `_shape_tenant` — la même dérivation que la vraie
# lecture (état d'émetteur, compteurs entiers). Stuber la forme SERVIE plutôt que la
# forme BRUTE ferait tester une réponse que le code ne produit jamais.
_OTO = tenants_db._shape_tenant(
    {"id": 1, "slug": "oto", "name": "Oto", "issuer": None, "jwks_uri": None,
     "hosts": [], "oauth_client_id": None, "dashboard_url": None,
     "link_paths": {}, "created_at": "2026-01-01 00:00:00", "orgs": 12,
     "orgs_archivees": 1, "comptes": 40, "comptes_actifs": 9, "appels": 300,
     "dernier_compte_at": None, "last_seen_at": None, "orgs_desalignees": 0})
_TIERS = tenants_db._shape_tenant(
    dict(_OTO, id=2, slug="acme", name="Acme",
         issuer="https://auth.acme.test/oidc", hosts=["mcp.acme.test"],
         orgs=3, comptes=10, comptes_actifs=2, appels=44, orgs_desalignees=2))


def test_the_shape_derives_what_authenticates_from_the_row():
    """Le primaire tient son émetteur de l'ENV (une ligne le redéclarant est ignorée
    par le registre) ; un tenant tiers n'authentifie que s'il en porte un."""
    assert _OTO["primary"] is True
    assert _OTO["issuer_source"] == "env" and _OTO["authenticates"] is True
    assert _TIERS["primary"] is False
    assert _TIERS["issuer_source"] == "db" and _TIERS["authenticates"] is True
    muet = tenants_db._shape_tenant(dict(_TIERS, slug="globex", issuer=None))
    assert muet["issuer_source"] is None and muet["authenticates"] is False


def _caps():
    """Toutes les capacités du domaine tenant, prises AU REGISTRE — pas la liste
    déclarée par ce module : une surface tenant ajoutée ailleurs doit tomber ici."""
    return [c for c in CAPABILITIES if c.key.startswith("admin.tenant")]


# ── 1. autz ─────────────────────────────────────────────────────────────────

def test_tenant_surfaces_read_platform_admin_reload_super_admin(monkeypatch):
    """Les LECTURES restent PLATFORM_ADMIN ; le `reload` (il change ce que le
    process AUTHENTIFIE — les émetteurs acceptés) est SUPER_ADMIN, y compris via
    l'op de la console (combinateur op-aware, jamais dans le handler)."""
    from oto_mcp import access
    from oto_mcp.capabilities._types import RawCtx
    from types import SimpleNamespace

    caps = {c.key: c for c in _caps()}
    assert set(caps) == {"admin.tenants", "admin.tenant", "admin.tenant_console",
                         "admin.tenants_reload", "admin.tenant_keys",
                         "admin.tenant_key_set", "admin.tenant_key_clear",
                         "admin.tenant_apps", "admin.tenant_app_set", "admin.tenant_app_clear",
                         "admin.tenant_connectors", "admin.tenant_connector_set",
                         "admin.tenant_connector_clear",
                         "admin.tenant_admins", "admin.tenant_admin_add",
                         "admin.tenant_admin_remove", "admin.tenant_org_grants",
                         "admin.tenant_org_grant", "admin.tenant_org_revoke"}
    assert caps["admin.tenants"].authz is PLATFORM_ADMIN
    # PR 2 : la fiche s'ouvre à l'admin du tenant (plancher plateforme `None`, la
    # règle plateforme PLATFORM_ADMIN essayée d'abord — `test_tenant_admin_role.py`).
    from oto_mcp.capabilities import _authz
    assert _authz.platform_floor(caps["admin.tenant"].authz) is None
    from oto_mcp.capabilities._authz import SUPER_ADMIN
    assert caps["admin.tenants_reload"].authz is SUPER_ADMIN
    # La console : un opérateur plateforme (non super) lit, mais ne recharge pas.
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: True)
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(access, "get_user_role", lambda sub: "admin")
    rule = caps["admin.tenant_console"].authz
    assert rule(RawCtx(sub="op"), SimpleNamespace(op="list")).sub == "op"
    with pytest.raises(AuthzDenied) as ei:
        rule(RawCtx(sub="op"), SimpleNamespace(op="reload"))
    assert ei.value.status == 403


# ── 2. lecture seule (sauf le geste de prise d'effet) ───────────────────────

def test_the_tracking_surface_cannot_write():
    """Le SUIVI reste sans verbe d'écriture — la seule exception est `reload`
    (`admin.tenants_reload`, POST) : il n'écrit RIEN en base, il fait relire au
    process ce que le runbook a déclaré (la moitié « prise d'effet » de 0052 B4).

    Se prouve sur le REGISTRE (ce qui est monté), pas sur les noms de ce module :
    une capacité tenant ajoutée ailleurs, avec un POST, tombe ici aussi.
    """
    from oto_mcp.capabilities._authz import SUPER_ADMIN
    # L-clés PR 1 (2026-08-29) : la CLÉ DE CONNECTEUR du tenant est la seule chose que
    # cette surface écrit — poser (PUT, REST seule : un secret brut ne traverse pas un
    # appel d'outil) et retirer (DELETE). Les deux sont SUPER_ADMIN, comme `reload` :
    # ça change ce que la résolution sert à tout un tenant. Déclarer le tenant
    # lui-même reste un runbook.
    # PR 2 (2026-08-29) : le rôle d'admin de tenant (déclaré par la plateforme,
    # SUPER_ADMIN) et l'arête tenant→org (posée par l'admin du tenant OU le super
    # admin — `TENANT_ADMIN_OF`, plancher `None`). Les clés suivent la même règle.
    from oto_mcp.capabilities import _authz
    ecritures_admises = {
        "admin.tenant_key_set": ("PUT", None), "admin.tenant_key_clear": ("DELETE", None),
        # L'app OAuth du tenant (23/09/2026) : même règle que ses clés — l'admin du
        # tenant OU le super admin, scopé au slug de la route.
        "admin.tenant_app_set": ("PUT", None), "admin.tenant_app_clear": ("DELETE", None),
        # Le plafond d'activation du tenant (26/09/2026) : même règle.
        "admin.tenant_connector_set": ("PUT", None),
        "admin.tenant_connector_clear": ("DELETE", None),
        "admin.tenant_admin_add": ("POST", "super"),
        "admin.tenant_admin_remove": ("DELETE", "super"),
        "admin.tenant_org_grant": ("PUT", None), "admin.tenant_org_revoke": ("DELETE", None),
    }
    for c in _caps():
        if c.key == "admin.tenants_reload":
            assert c.rest.verb == "POST"
            continue
        if c.key in ecritures_admises:
            verbe, plancher = ecritures_admises[c.key]
            assert c.rest.verb == verbe and _authz.platform_floor(c.authz) == plancher, c.key
            continue
        verbe = c.rest.verb if c.rest else "GET"
        assert verbe == "GET", (
            f"{c.key} expose {verbe} : déclarer un tenant est un runbook de "
            "provisioning (instance Logto + client OAuth + hosts) — une écriture "
            "ici laisserait croire qu'une ligne en base suffit.")


# ── 3. l'écart base ↔ registre du process ───────────────────────────────────

def _registry(*entries):
    return tenancy.IssuerRegistry(tenancy.build("https://auth.oto.ninja/oidc",
                                                tenants=entries))


def test_a_declared_tenant_absent_from_the_registry_is_flagged(monkeypatch):
    """Le cas qui motive l'écran : la ligne existe, le process ne l'a pas chargée
    (déclarée après le dernier boot) — donc ses jetons sont rejetés."""
    monkeypatch.setattr(ta.db, "list_tenants_overview", lambda **kw: [dict(_OTO), dict(_TIERS)])
    monkeypatch.setattr(tenancy, "_INSTALLED", _registry(), raising=False)

    par_slug = {t["slug"]: t for t in ta._tenants(CTX, ta.TenantsInput())["tenants"]}
    assert par_slug["acme"]["pending_restart"] is True
    assert par_slug["acme"]["loaded"] is False
    # Le primaire tient son émetteur de l'env : il est chargé, jamais « en attente ».
    assert par_slug["oto"]["loaded"] is True
    assert par_slug["oto"]["pending_restart"] is False


def test_a_loaded_tenant_reports_the_hosts_the_process_serves(monkeypatch):
    monkeypatch.setattr(ta.db, "list_tenants_overview", lambda **kw: [dict(_TIERS)])
    monkeypatch.setattr(tenancy, "_INSTALLED", _registry(
        {"slug": "acme", "issuer": "https://auth.acme.test/oidc",
         "hosts": ["mcp.acme.test"]}), raising=False)

    t = ta._tenants(CTX, ta.TenantsInput())["tenants"][0]
    assert t["loaded"] is True and t["pending_restart"] is False
    assert t["live_hosts"] == ["mcp.acme.test"]


def test_a_tenant_without_an_issuer_does_not_authenticate(monkeypatch):
    """Une ligne sans émetteur n'authentifie personne — et ce n'est PAS un
    « redémarrage en attente » : il n'y a rien à charger."""
    orphelin = tenants_db._shape_tenant(dict(_TIERS, slug="globex", issuer=None, hosts=[]))
    monkeypatch.setattr(ta.db, "list_tenants_overview", lambda **kw: [orphelin])
    monkeypatch.setattr(tenancy, "_INSTALLED", _registry(), raising=False)

    t = ta._tenants(CTX, ta.TenantsInput())["tenants"][0]
    assert t["authenticates"] is False
    assert t["pending_restart"] is False


_MGMT = {"token_endpoint": "https://admin.acme.test",
         "api_endpoint": "https://auth.acme.test",
         "credential": "LOGTO_ACME_MGMT"}


def _avec_mgmt(monkeypatch):
    ligne = tenants_db._shape_tenant(dict(_TIERS, logto_mgmt=dict(_MGMT)))
    monkeypatch.setattr(ta.db, "list_tenants_overview", lambda **kw: [ligne])
    monkeypatch.setattr(tenancy, "_INSTALLED", _registry(
        {"slug": "acme", "issuer": "https://auth.acme.test/oidc",
         "hosts": ["mcp.acme.test"], "logto_mgmt": dict(_MGMT)}), raising=False)


def test_un_acces_dannuaire_declare_sans_sa_cle_se_voit(monkeypatch):
    """Déclaré en base ≠ utilisable par CE process. Sans cette confrontation, un refus
    d'enregistrement (oto-backend#909) ne se rattache à rien de visible : la fiche
    montrerait un accès posé et la façade refuserait quand même."""
    _avec_mgmt(monkeypatch)
    monkeypatch.delenv("LOGTO_ACME_MGMT_ID", raising=False)
    monkeypatch.delenv("LOGTO_ACME_MGMT_SECRET", raising=False)

    t = ta._tenants(CTX, ta.TenantsInput())["tenants"][0]

    assert t["logto_mgmt"]["api_endpoint"] == "https://auth.acme.test"
    assert t["directory_admin"] is False
    # …et la fiche ne porte que le NOM du credential, jamais sa valeur.
    assert t["logto_mgmt"]["credential"] == "LOGTO_ACME_MGMT"


def test_un_acces_dannuaire_complet_est_administrable(monkeypatch):
    _avec_mgmt(monkeypatch)
    monkeypatch.setenv("LOGTO_ACME_MGMT_ID", "cid-de-test")
    monkeypatch.setenv("LOGTO_ACME_MGMT_SECRET", "csec-de-test")

    t = ta._tenants(CTX, ta.TenantsInput())["tenants"][0]

    assert t["directory_admin"] is True
    assert ta.TenantRow(**t).directory_admin is True


@pytest.mark.parametrize("declare, charge, allume", [
    (True, True, True),
    ("true", True, False),    # la chaîne s'affiche dans `logto_mgmt`… éteinte
    (1, True, False),
    (None, True, False),
    (True, False, False),     # déclaré, pas encore rechargé : rien ne s'applique
])
def test_la_fiche_rend_l_opt_in_refresh_tokens_EFFECTIF(monkeypatch, declare, charge, allume):
    """Relecture de #1030 : la fiche ne montrait que le déclaré — `"true"` en chaîne s'y
    lisait « allumé » alors que seul le booléen `true` allume."""
    mgmt = dict(_MGMT) if declare is None else dict(_MGMT, refresh_tokens=declare)
    ligne = tenants_db._shape_tenant(dict(_TIERS, logto_mgmt=mgmt))
    monkeypatch.setattr(ta.db, "get_tenant_overview", lambda slug, **kw: dict(ligne))
    entree = {"slug": "acme", "issuer": "https://auth.acme.test/oidc",
              "hosts": ["mcp.acme.test"]}
    monkeypatch.setattr(tenancy, "_INSTALLED", _registry(
        dict(entree, logto_mgmt=mgmt) if charge else entree), raising=False)

    t = ta._tenant(CTX, ta.TenantInput(slug="acme"))["tenant"]

    assert t["refresh_tokens_effectif"] is allume
    assert ta.TenantSheet(**t).refresh_tokens_effectif is allume
    if declare is not None:
        assert t["logto_mgmt"]["refresh_tokens"] == declare   # le déclaré reste lisible


def test_sans_declaration_lannuaire_nest_pas_administrable(monkeypatch):
    """Le défaut, et il le reste : authentifier les comptes d'un tenant ne donne aucun
    droit d'écrire dans son annuaire."""
    monkeypatch.setattr(ta.db, "list_tenants_overview", lambda **kw: [dict(_TIERS)])
    monkeypatch.setattr(tenancy, "_INSTALLED", _registry(
        {"slug": "acme", "issuer": "https://auth.acme.test/oidc"}), raising=False)

    t = ta._tenants(CTX, ta.TenantsInput())["tenants"][0]
    assert t["directory_admin"] is False and t["logto_mgmt"] == {}


def test_totals_are_summed_from_the_rows(monkeypatch):
    monkeypatch.setattr(ta.db, "list_tenants_overview", lambda **kw: [dict(_OTO), dict(_TIERS)])
    out = ta._tenants(CTX, ta.TenantsInput())
    assert out["totals"] == {"tenants": 2, "orgs": 15, "comptes": 50,
                             "comptes_actifs": 11, "appels": 344}


# ── 4. dispatch de la console ───────────────────────────────────────────────

def test_get_requires_a_slug():
    with pytest.raises(AuthzDenied) as e:
        ta._console(CTX, ta.TenantConsoleInput(op="get"))
    assert e.value.code == "missing_slug"


def test_an_unknown_slug_is_a_404_not_an_empty_sheet(monkeypatch):
    """Une fiche vide se lirait « ce tenant existe et n'a rien » — le contraire du
    vrai diagnostic (« ce tenant n'existe pas »)."""
    monkeypatch.setattr(ta.db, "get_tenant_overview", lambda slug, **kw: None)
    with pytest.raises(AuthzDenied) as e:
        ta._console(CTX, ta.TenantConsoleInput(op="get", slug="fantome"))
    assert e.value.status == 404 and e.value.code == "unknown_tenant"


def test_the_window_is_clamped_never_a_500(monkeypatch):
    """`days` part au SQL : une valeur négative faisait échouer la requête (#300)."""
    vues = []
    monkeypatch.setattr(ta.db, "list_tenants_overview",
                        lambda **kw: vues.append(kw["days"]) or [])
    ta._console(CTX, ta.TenantConsoleInput(op="list", days=-5))
    ta._console(CTX, ta.TenantConsoleInput(op="list", days=99999))
    ta._console(CTX, ta.TenantConsoleInput(op="list"))
    assert vues == [1, 365, 30]


def test_get_passes_the_slug_and_window_through(monkeypatch):
    vu = {}
    monkeypatch.setattr(ta.db, "get_tenant_overview",
                        lambda slug, **kw: vu.update(slug=slug, **kw) or dict(_TIERS))
    monkeypatch.setattr(tenancy, "_INSTALLED", _registry(), raising=False)
    out = ta._console(CTX, ta.TenantConsoleInput(op="get", slug="acme", days=7))
    assert vu == {"slug": "acme", "days": 7}
    assert out["tenant"]["slug"] == "acme" and out["days"] == 7
