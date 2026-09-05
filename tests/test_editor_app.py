"""App OAuth de l'éditeur : le repli « un clic », et l'invariant qui le rend sûr.

Le risque de ce cran n'est pas qu'il ne marche pas — c'est qu'il marche TROP : une app
d'éditeur rangée au scope plateforme du coffre ne doit jamais être servie comme
credential d'APPEL. Sinon un membre qui n'a pas consenti hériterait d'une app nue (sans
refresh_token) et se prendrait un échec OAuth opaque, au lieu de s'entendre dire de se
connecter. D'où le premier test, qui figera cet invariant même si quelqu'un ajoute un
jour `platform` aux auth_modes de zoho.
"""
from __future__ import annotations

import pytest

from oto_mcp import access, providers, credentials_store
from oto_mcp.auth import zoho as zoho_oauth


# --- l'invariant ---------------------------------------------------------------

def test_zoho_has_no_platform_auth_mode():
    """Ce que le rangement de l'app d'éditeur suppose : zoho n'accepte PAS de
    credential plateforme d'accès. C'est ce qui rend `walk_cascade` aveugle à elle."""
    for name in ("zoho", "zohodesk", "zohoanalytics"):
        con = providers.REGISTRY.get(name)
        assert con is not None, name
        assert "platform" not in con.auth_modes, (
            f"{name} a gagné le mode 'platform' : l'app d'éditeur deviendrait "
            "résolvable comme credential d'appel (app NUE sans refresh_token servie à "
            "qui n'a pas consenti). Relire credentials_store §app d'éditeur.")


def test_cascade_never_yields_platform_for_zoho():
    """Le walker lui-même : même en `want='auto'` et avec une sonde qui dit OUI à tous
    les barreaux, aucun barreau plateforme pour zoho. Sonde stubbée = test pur (le
    chemin SQL est vérifié au déploiement, convention du repo)."""
    yes = access.CascadeProbe(
        member=lambda s, o, p: (True, ""), member_cross=lambda s, o, p: True,
        legacy_user=lambda s, p: True,
        group=lambda g, p: True, org=lambda o, p: True, tenant=lambda t, p: True,
        platform=lambda s, p, o: {"secret": "x", "label": "l"})
    modes = [r.mode for r in access.walk_cascade(
        "sub-x", "zoho", org=1, group=2, probe=yes, want="auto")]
    assert "platform" not in modes, modes
    # Contre-épreuve : sur un connecteur qui DÉCLARE le mode plateforme, le même
    # walker le propose — sinon ce test passerait pour une raison sans rapport.
    assert "platform" in [r.mode for r in access.walk_cascade(
        "sub-x", "serper", org=1, group=2, probe=yes, want="auto")]


# --- le repli ------------------------------------------------------------------

class _FakeCred:
    def __init__(self, fields):
        self.fields = fields


def test_app_fields_prefers_byo_over_editor(monkeypatch):
    """L'app APPORTÉE prime : une org qui pose la sienne ne bascule pas sur la nôtre."""
    monkeypatch.setattr(access, "resolve_credential",
                        lambda *a, **k: _FakeCred({"client_id": "byo", "client_secret": "s"}))
    monkeypatch.setattr(credentials_store, "get_editor_app",
                        lambda *a, **k: {"client_id": "oto", "client_secret": "s2"})
    assert zoho_oauth.app_fields("zoho", "sub-x", "eu")["client_id"] == "byo"


def test_app_fields_falls_back_to_editor_app(monkeypatch):
    """Rien d'apporté → l'app d'éditeur de la RÉGION demandée."""
    monkeypatch.setattr(access, "resolve_credential",
                        lambda *a, **k: _FakeCred({}))
    monkeypatch.setattr(credentials_store, "get_editor_app",
                        lambda c, dc: {"client_id": f"oto-{dc}", "client_secret": "s"}
                        if dc == "eu" else None)
    assert zoho_oauth.app_fields("zoho", "sub-x", "eu")["client_id"] == "oto-eu"
    # Une autre région ne tombe PAS sur l'app EU (un client `.eu` sur accounts.zoho.com
    # est rejeté par un `invalid_client` opaque — le silence coûterait un diagnostic).
    assert zoho_oauth.app_fields("zoho", "sub-x", "com") == {}


def test_app_fields_ignores_editor_app_without_region(monkeypatch):
    """Sans région, pas de repli : l'app d'éditeur est keyée par data center."""
    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: _FakeCred({}))
    monkeypatch.setattr(credentials_store, "get_editor_app",
                        lambda *a, **k: {"client_id": "oto", "client_secret": "s"})
    assert zoho_oauth.app_fields("zoho", "sub-x") == {}


def test_build_auth_url_uses_editor_app(monkeypatch):
    """Bout en bout : l'URL de consentement porte le client_id de l'app d'éditeur."""
    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: _FakeCred({}))
    monkeypatch.setattr(credentials_store, "get_editor_app",
                        lambda c, dc: {"client_id": "1000.OTO", "client_secret": "s"})
    monkeypatch.setattr(zoho_oauth, "redirect_uri",
                        lambda: "https://mcp.oto.cx/api/zoho/oauth/callback")
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "test-secret")
    url = zoho_oauth.build_auth_url("sub-x", 1, "zoho", "eu")
    assert url.startswith("https://accounts.zoho.eu/oauth/v2/auth?")
    assert "client_id=1000.OTO" in url


def test_resolve_app_message_is_actionable():
    with pytest.raises(zoho_oauth.ZohoOAuthError) as e:
        zoho_oauth.resolve_app({})
    assert "self client" in str(e.value).lower()


# --- le rangement --------------------------------------------------------------

def test_editor_label_is_keyed_by_region():
    assert credentials_store.editor_label("EU") == "editor:eu"
    assert credentials_store.editor_label(" com ") == "editor:com"


def test_set_editor_app_requires_both_halves():
    for fields in ({}, {"client_id": "x"}, {"client_secret": "y"}):
        with pytest.raises(ValueError):
            credentials_store.set_editor_app("zoho", "eu", fields)


def test_set_editor_app_requires_region():
    with pytest.raises(ValueError):
        credentials_store.set_editor_app(
            "zoho", "", {"client_id": "x", "client_secret": "y"})


# --- ce que le consentement produit, et ce qu'il ne produira jamais ---------------

def test_persisted_fields_match_what_persist_writes(monkeypatch):
    """Tripwire : `PERSISTED_FIELDS` sert à décider quels champs requis restent à la
    charge de l'utilisateur. S'il dérive de ce que `persist` écrit vraiment, un champ
    réapparaît comme « manquant » alors qu'il est rempli — ou l'inverse, plus grave."""
    ecrit = {}
    monkeypatch.setattr(credentials_store, "secret_from_input",
                        lambda c, k, fields: ecrit.update(fields) or "packed")
    monkeypatch.setattr(credentials_store, "member_id", lambda o, s: f"{o}:{s}")
    monkeypatch.setattr(credentials_store, "set_credential", lambda *a, **k: None)
    zoho_oauth.persist("sub-x", 1, "zoho", "eu", {"refresh_token": "rt"},
                       app={"client_id": "cid", "client_secret": "sec"})
    assert set(ecrit) == set(zoho_oauth.PERSISTED_FIELDS)


def test_analytics_org_id_is_reported_as_missing():
    """Le cas qui motive tout : Analytics exige un `org_id` que l'OAuth ne produit pas.
    Après consentement, le credential est donc VALIDE mais inutilisable — et le dire
    est la seule façon d'éviter un échec opaque au premier appel."""
    # Import EXPLICITE : les hooks d'état sont posés à l'import du module connecteur.
    # Sans lui, le test ne passait que si un autre fichier de la suite l'avait importé
    # avant — donc vert en suite complète, rouge en isolation.
    from oto_mcp import status_hints
    import oto_mcp.tools.zoho  # noqa: F401 — enregistre les hooks des 3 connecteurs
    consenti = {"client_id": "c", "client_secret": "s", "refresh_token": "rt",
                "data_center": "eu"}
    st = status_hints.credential_state("zohoanalytics", consenti)
    assert st is not None and not st.complete
    assert st.missing == ("org_id",)
    assert "org id" in (st.next_action or "").lower()
    # Le même credential sur le CRM, lui, est complet : rien d'autre n'est requis.
    assert status_hints.credential_state("zoho", consenti).complete
