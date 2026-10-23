"""Corps partagé du hosted-auth Unipile (`unipile_connect.hosted_auth_url`,
feedback #131) : gates (canal, clé, org, option, plafond) en ConnectRefused
typée, happy path = nonce posé + lien Unipile rendu. Clients/DB stubés."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from oto_mcp import access, unipile_connect
from oto_mcp.unipile_connect import ConnectRefused, hosted_auth_url

pytestmark = pytest.mark.usefixtures("connecteurs_tous_disponibles")


class _FakeClient:
    def __init__(self, api_key=None, dsn=None, **k):
        self.api_key, self.dsn = api_key, dsn

    def hosted_auth_link(self, **kwargs):
        _FakeClient.last_kwargs = kwargs
        return "https://account.unipile.com/auth?token=xyz"


def _wire(monkeypatch, *, byo=False, option=True, org=39, existing=None, count=0,
          limit=None, connected=None):
    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: SimpleNamespace(
        key="KEY", mode="org" if byo else "platform", config={}))
    monkeypatch.setattr(access, "current_org", lambda sub: org)
    monkeypatch.setattr(access, "has_right", lambda sub, org_id, droit: option)
    # Garde-fou anti-doublon cross-org (#172) : comptes déjà connectés du sub, tous
    # canaux/orgs confondus. [] par défaut ⇒ garde-fou inerte (chemins existants).
    monkeypatch.setattr("oto_mcp.db.list_unipile_accounts", lambda sub: connected or [])
    # Adoption (binding-par-org) : siège plateforme du sub dans une autre org, dérivé
    # de `connected` (platform_seat=True seulement — un BYO n'est jamais adoptable).
    def _seat_elsewhere(sub, prov="LINKEDIN", exclude_org=None):
        for a in (connected or []):
            if (a.get("provider") == prov and a.get("org_id") != exclude_org
                    and a.get("platform_seat")):
                return a
        return None
    monkeypatch.setattr("oto_mcp.db.seat_binding_elsewhere", _seat_elsewhere)
    monkeypatch.setattr("oto_mcp.db.set_unipile_account", lambda *a, **k: None)
    monkeypatch.setattr("oto_mcp.db.get_unipile_account",
                        lambda sub, org_id, prov: existing)
    monkeypatch.setattr("oto_mcp.db.get_org_unipile_limit", lambda org_id: limit)
    monkeypatch.setattr("oto_mcp.db.count_unipile_accounts_for_org", lambda org_id: count)
    pending = {}
    monkeypatch.setattr("oto_mcp.db.create_unipile_pending",
                        lambda nonce, sub, org_id, prov, platform_seat=False:
                        pending.update(nonce=nonce, sub=sub, org_id=org_id,
                                       provider=prov, platform_seat=platform_seat))
    monkeypatch.setattr("oto.tools.unipile.UnipileClient", _FakeClient)
    # Le refus d'option nomme les administrateurs de l'org à un membre (oto#108) : ici
    # un membre, dont l'org a une administratrice.
    monkeypatch.setattr("oto_mcp.detenteurs.est_membre", lambda sub, org_id: True)
    monkeypatch.setattr("oto_mcp.org_store.list_org_members", lambda org_id: [
        {"sub": "adm", "org_role": "org_admin"}, {"sub": "u1", "org_role": "org_member"}])
    monkeypatch.setattr("oto_mcp.db.get_user", lambda sub: {
        "adm": {"name": "Admin Exemple", "email": "admin@example.test"}}.get(sub))
    return pending


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_happy_path_returns_url_and_poses_nonce(monkeypatch):
    pending = _wire(monkeypatch)
    out = _run(hosted_auth_url("u1", "linkedin"))
    assert out["url"].startswith("https://account.unipile.com/")
    assert out["channel"] == "linkedin"
    # pending posé (la clé de la réconciliation), siège plateforme (mode revente)
    assert pending["provider"] == "LINKEDIN" and pending["platform_seat"] is True
    assert pending["nonce"] == _FakeClient.last_kwargs["name"]
    # Plus de `notify_url` (#581) : le fournisseur ne rappelle plus ce callback en v2,
    # et la route qui le recevait est retirée. En envoyer un ferait pointer le
    # fournisseur sur un 404 — et laisserait croire qu'un webhook existe.
    assert "notify_url" not in _FakeClient.last_kwargs


def test_invalid_channel_refused(monkeypatch):
    _wire(monkeypatch)
    with pytest.raises(ConnectRefused) as e:
        _run(hosted_auth_url("u1", "pigeon"))
    assert e.value.code == "invalid_channel" and e.value.status == 400


def test_no_key_refused(monkeypatch):
    _wire(monkeypatch)
    from mcp.types import ErrorData, INVALID_PARAMS
    def missing(*a, **k):
        raise unipile_connect.CredentialUnavailable(ErrorData(code=INVALID_PARAMS, message="missing"))
    monkeypatch.setattr(access, "resolve_credential", missing)
    with pytest.raises(ConnectRefused) as e:
        _run(hosted_auth_url("u1"))
    assert e.value.code == "unipile_not_configured" and e.value.status == 404


def test_option_gate_on_platform_key(monkeypatch):
    _wire(monkeypatch, option=False)
    with pytest.raises(ConnectRefused) as e:
        _run(hosted_auth_url("u1"))
    assert e.value.code == "unipile_option_required" and e.value.status == 402
    # QUI lève l'option, et OÙ — pas « un admin » sans autre précision (oto#108).
    assert "administrateur de cette org" in e.value.message
    assert "équipe de la plateforme" in e.value.message
    assert "Admin Exemple" in e.value.message
    assert "admin@example.test" not in e.value.message      # le nom seul, jamais l'adresse


def test_le_gate_lit_le_droit_de_la_personne_qui_connecte(monkeypatch):
    """Le droit est celui de la personne dans son org (org OU ligne posée sur elle,
    ADR 0070 §7) : le gate demande `has_right(sub, org)`, jamais l'org seule."""
    _wire(monkeypatch)
    lus = []
    monkeypatch.setattr(access, "has_right",
                        lambda sub, org_id, droit: lus.append((sub, org_id, droit)) or True)
    assert _run(hosted_auth_url("u1", "linkedin"))["url"]
    assert lus == [("u1", 39, "unipile")]


def test_no_org_cap_on_new_hosted_account(monkeypatch):
    # plus de plafond par org (2026-10-23) : ni la colonne de l'org ni le défaut ne refusent
    _wire(monkeypatch, existing=None, count=5, limit=5)
    assert _run(hosted_auth_url("u1"))["url"]


def test_reconnect_existing_account_bypasses_cap(monkeypatch):
    # un compte déjà connecté = remplacement, pas un nouveau siège
    _wire(monkeypatch, existing={"account_id": "A1"}, count=5, limit=5)
    out = _run(hosted_auth_url("u1"))
    assert out["url"]


def test_byo_skips_option_and_cap(monkeypatch):
    _wire(monkeypatch, byo=True, option=False, count=99, limit=1)
    # resolve_credential (lookup DSN) stubé : la clé BYO porte son dsn
    class _RC:
        key = "KEY"
        mode = "org"
        config = {"dsn": "api6.unipile.com:13616"}
    monkeypatch.setattr(access, "resolve_credential",
                        lambda *a, **k: _RC())
    out = _run(hosted_auth_url("u1"))
    assert out["url"]


# --- Garde-fou anti-doublon cross-org (#172, piste C) ------------------------

def test_refuses_when_same_channel_connected_in_another_org(monkeypatch):
    # Le sub a déjà LinkedIn connecté dans l'org 2 → connecter dans l'org 39
    # créerait un 2e account_id pour le même login (rotation du cookie). Refus 409.
    _wire(monkeypatch, org=39, connected=[
        {"provider": "LINKEDIN", "account_id": "OLD", "account_name": "laportealexis",
         "org_id": 2}])
    with pytest.raises(ConnectRefused) as e:
        _run(hosted_auth_url("u1", "linkedin"))
    assert e.value.code == "unipile_already_connected_elsewhere"
    assert e.value.status == 409 and "laportealexis" in e.value.message


def test_force_bypasses_cross_org_guard(monkeypatch):
    # force=True honore une reconnexion délibérée (compte réellement distinct).
    _wire(monkeypatch, org=39, connected=[
        {"provider": "LINKEDIN", "account_id": "OLD", "org_id": 2}])
    out = _run(hosted_auth_url("u1", "linkedin", force=True))
    assert out["url"]


def test_same_org_reconnect_not_blocked_by_guard(monkeypatch):
    # Un compte du MÊME canal dans l'org de contexte = remplacement, pas un doublon.
    _wire(monkeypatch, org=39, existing={"account_id": "A1"}, connected=[
        {"provider": "LINKEDIN", "account_id": "A1", "org_id": 39}])
    out = _run(hosted_auth_url("u1", "linkedin"))
    assert out["url"]


def test_guard_channel_scoped(monkeypatch):
    # LinkedIn connecté ailleurs ne bloque pas la connexion d'un canal DIFFÉRENT.
    _wire(monkeypatch, org=39, connected=[
        {"provider": "LINKEDIN", "account_id": "OLD", "org_id": 2}])
    out = _run(hosted_auth_url("u1", "whatsapp"))
    assert out["url"]


# --- #237 : premium reconnecte le siège existant, MÊME avec force=true --------

def test_premium_reconnects_existing_seat_even_with_force(monkeypatch):
    # L'agent passe force=true POUR dépasser l'anti-doublon (compte déjà connecté) →
    # ajouter Recruiter doit RECONNECTER le siège (rattache le produit), pas créer un
    # 2e compte (type=create qui perdait le premium et donnait le 403 Recruiter).
    _wire(monkeypatch, org=39, connected=[
        {"provider": "LINKEDIN", "account_id": "SEAT1", "org_id": 39,
         "platform_seat": True}])
    _run(hosted_auth_url("u1", "linkedin", force=True, premium="recruiter"))
    assert _FakeClient.last_kwargs["reconnect_account"] == "SEAT1"
    assert _FakeClient.last_kwargs["premium"] == "recruiter"


def test_plain_force_still_creates_new_account(monkeypatch):
    # Sans premium, force=true reste un create (compte réellement neuf) : pas de reconnect.
    _wire(monkeypatch, org=39, connected=[
        {"provider": "LINKEDIN", "account_id": "OLD", "org_id": 2, "platform_seat": True}])
    _run(hosted_auth_url("u1", "linkedin", force=True))
    assert _FakeClient.last_kwargs["reconnect_account"] is None


# --- où le wizard dépose la personne (`app`, liste fermée) ---------------------
#
# Le hosted-auth SORT du site : cette URL est la seule chose qui décide sur quel
# front on se réveille. Et ce n'est pas cosmétique — la liaison du compte se fait
# par réconciliation, sous le JWT du front d'arrivée : atterrir sur le mauvais
# front, c'est réconcilier sous un AUTRE sub, donc ne rien lier (vécu 2026-08-22).

@pytest.fixture
def front_tiers(monkeypatch):
    """Deux entrées FICTIVES dans `RETURN_APPS`, le temps du test : la liste fermée
    se comporte pareil quelle que soit l'entrée, et les négatifs ci-dessous (casse,
    origine, clé voisine) n'ont de sens que si une clé voisine EXISTE vraiment."""
    from oto_mcp.auth import flow as oauth_flow
    monkeypatch.setitem(oauth_flow.RETURN_APPS, "acme",
                        ("https://app.acme.test", "/org/{org}/connectors"))
    monkeypatch.setitem(oauth_flow.RETURN_APPS, "acme-preprod",
                        ("https://acme.oto.zone", "/org/{org}/connectors"))


def test_return_lands_on_the_front_that_asked(monkeypatch, front_tiers):
    _wire(monkeypatch)
    _run(hosted_auth_url("u1", "linkedin", app="acme"))
    kw = _FakeClient.last_kwargs
    assert kw["success_redirect_url"] == (
        "https://app.acme.test/org/39/connectors?unipile=connected&channel=linkedin")
    assert kw["failure_redirect_url"] == (
        "https://app.acme.test/org/39/connectors?unipile=failed&channel=linkedin")


def test_return_preprod_app(monkeypatch, front_tiers):
    _wire(monkeypatch, org=3)
    _run(hosted_auth_url("u1", "whatsapp", app="acme-preprod"))
    assert _FakeClient.last_kwargs["success_redirect_url"] == (
        "https://acme.oto.zone/org/3/connectors?unipile=connected&channel=whatsapp")


def test_no_app_keeps_the_historic_dashboard_destination(monkeypatch):
    """Face MCP et oto-dashboard : `app` absent ⇒ destination INCHANGÉE, à l'octet
    près. `/console/connections` est un chemin propre au dashboard que le patron
    générique `return_url` ne connaît pas — y retomber le renverrait sur
    `/connectors`, une régression pour l'appelant historique."""
    _wire(monkeypatch)
    _run(hosted_auth_url("u1", "linkedin"))
    url = _FakeClient.last_kwargs["success_redirect_url"]
    assert url.endswith("/console/connections?unipile=connected&channel=linkedin")
    assert "/org/" not in url


@pytest.fixture
def tenants():
    """Deux tenants FICTIFS : `acme` déclare sa page de connecteurs (portée par l'org),
    `beta` ne déclare rien."""
    from oto_mcp import tenancy
    avant = tenancy.current()
    tenancy.install(tenancy.IssuerRegistry(tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": "acme", "issuer": "https://auth.acme.test/oidc",
                  "dashboard_url": "https://app.acme.test",
                  "link_paths": {"connectors": "/org/{org}/connectors"}},
                 {"slug": "beta", "issuer": "https://auth.beta.test/oidc"}])))
    yield
    tenancy.install(avant)


def test_no_app_returns_a_partner_account_to_its_own_product(monkeypatch, tenants):
    """Face MCP (pas d'`app`) pour un compte d'un tenant TIERS : retour sur SA page de
    connecteurs, dérivée du sub — jamais sur notre dashboard. Vécu le 2026-09-03 : le
    client y a créé un second compte, et la réconciliation a tourné sous ce sub-là."""
    _wire(monkeypatch, org=270)
    _run(hosted_auth_url("acme:u1", "linkedin"))
    kw = _FakeClient.last_kwargs
    assert kw["success_redirect_url"] == (
        "https://app.acme.test/org/270/connectors?unipile=connected&channel=linkedin")
    assert kw["failure_redirect_url"] == (
        "https://app.acme.test/org/270/connectors?unipile=failed&channel=linkedin")


def test_partner_without_connectors_page_still_lands_somewhere(monkeypatch, tenants):
    """Une redirection doit aboutir : tenant tiers sans patron ⟹ destination historique."""
    _wire(monkeypatch)
    _run(hosted_auth_url("beta:u1", "linkedin"))
    assert _FakeClient.last_kwargs["success_redirect_url"].endswith(
        "/console/connections?unipile=connected&channel=linkedin")


def test_app_still_wins_over_the_tenant_page(monkeypatch, tenants, front_tiers):
    _wire(monkeypatch, org=3)
    _run(hosted_auth_url("acme:u1", "linkedin", app="acme-preprod"))
    assert _FakeClient.last_kwargs["success_redirect_url"] == (
        "https://acme.oto.zone/org/3/connectors?unipile=connected&channel=linkedin")


def test_connections_page_joins_an_existing_query(monkeypatch):
    monkeypatch.setattr(unipile_connect, "connections_page",
                        lambda sub, org_id: "https://app.acme.test/c?tab=msg")
    assert unipile_connect._return_to(None, 1, "?unipile=connected", "acme:u1") == (
        "https://app.acme.test/c?tab=msg&unipile=connected")


def test_connections_page_primary_is_the_historic_page(tenants):
    assert unipile_connect.connections_page("u1", 5).endswith("/console/connections")
    assert unipile_connect.connections_page("acme:u1", 5) == (
        "https://app.acme.test/org/5/connectors")
    assert unipile_connect.connections_page("beta:u1", 5) is None


@pytest.mark.parametrize("hostile", ["https://evil.test", "oto", "", "ACME",
                                     "app.acme.test", "acme-preprod-x"])
def test_unknown_app_never_becomes_a_redirect(monkeypatch, hostile, front_tiers):
    """Jamais une origine prise telle quelle : une valeur hors liste fermée retombe
    sur le défaut, elle ne voyage PAS dans l'URL (ce serait un open redirect)."""
    _wire(monkeypatch)
    _run(hosted_auth_url("u1", "linkedin", app=hostile))
    url = _FakeClient.last_kwargs["success_redirect_url"]
    assert url.endswith("/console/connections?unipile=connected&channel=linkedin")
    assert "evil.test" not in url


# ── Ce que le résolveur reçoit, et ce qu'il refuse ──────────────────────────
# Ces deux bancs ont été AJOUTÉS en reprenant ce lot. Le chantier d'origine avait
# relâché les doublures en `lambda *a, **k` : plus aucun banc n'observait le nom
# passé au résolveur, ni le refus qu'il peut rendre. Une garde que rien n'observe
# a l'air d'exister — c'est la pire des trois façons de ne pas garder.

def test_le_resolveur_recoit_le_nom_du_CANAL_pas_celui_du_porteur(monkeypatch):
    """L'ACL est réglable canal par canal : `linkedin_unipile` porte ses propres
    droits, distincts de ceux du porteur `unipile`. Si quelqu'un remplace le nom
    du canal par celui du porteur, « qui peut connecter LinkedIn » cesse d'être
    réglable — et sans ce banc, toute la suite resterait verte."""
    _wire(monkeypatch)
    vu = {}

    def _capte(nom, **kwargs):
        vu["nom"] = nom
        vu["check_usage"] = kwargs.get("check_usage")
        return SimpleNamespace(key="KEY", mode="platform", config={})

    monkeypatch.setattr(access, "resolve_credential", _capte)
    _run(hosted_auth_url("u1", "linkedin"))

    from oto_mcp import providers
    attendu = providers.connector_for_hosted_channel("linkedin")
    assert attendu is not None, "le canal linkedin doit avoir son connecteur"
    assert vu["nom"] == attendu.name, (
        f"le résolveur a reçu {vu['nom']!r} au lieu du canal {attendu.name!r} : "
        "l'autorisation ne serait plus réglable canal par canal")
    assert vu["check_usage"] is False, (
        "configurer une connexion ne consomme pas un appel fournisseur")
