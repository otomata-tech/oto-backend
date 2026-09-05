"""Fabrique d'acquisition OAuth (`oauth_flow`) — le noyau écrit UNE fois.

Le point le plus sensible est l'**audience** : avant la fabrique, cinq flux OAuth
signaient leur `state` avec le MÊME secret d'env, au même format, sans discriminant
(`folk` et `atlassian` partageaient jusqu'à la forme exacte du payload). Un state
émis pour un flux était donc structurellement acceptable par le callback d'un autre.
La liaison à l'audience ferme ça par construction.
"""
from __future__ import annotations

import time

import pytest

from oto_mcp.auth import flow as of


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "test-secret")
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.oto.cx")


# --- state : signature + audience ---------------------------------------------

def test_roundtrip_preserves_payload():
    st = of.sign_state("zoho", {"sub": "u1", "org": 35})
    d = of.read_state("zoho", st)
    assert d["sub"] == "u1" and d["org"] == 35 and d["aud"] == "zoho"


def test_state_of_another_flow_is_refused():
    """LA correction de fond : un state Google ne vaut pas sur le callback Zoho."""
    st = of.sign_state("google", {"sub": "u1", "org": 35})
    assert of.read_state("zoho", st) is None
    assert of.read_state("google", st) is not None


def test_tampered_payload_is_refused():
    st = of.sign_state("zoho", {"sub": "u1", "org": 35})
    body, sig = st.split(".", 1)
    forged = of._b64url(b'{"aud":"zoho","org":99,"sub":"pirate","ts":9999999999}')
    assert of.read_state("zoho", f"{forged}.{sig}") is None


def test_wrong_secret_is_refused(monkeypatch):
    st = of.sign_state("zoho", {"sub": "u1"})
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "autre")
    assert of.read_state("zoho", st) is None


def test_expired_state_is_refused(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1_000_000)
    st = of.sign_state("zoho", {"sub": "u1"})
    monkeypatch.setattr(time, "time", lambda: 1_000_000 + of.DEFAULT_STATE_TTL + 1)
    assert of.read_state("zoho", st) is None


@pytest.mark.parametrize("bad", [None, "", "abc", "a.b", "..", "x.y.z"])
def test_malformed_state_is_refused(bad):
    assert of.read_state("zoho", bad) is None


def test_missing_secret_is_explicit(monkeypatch):
    monkeypatch.delenv("OTO_MCP_OAUTH_STATE_SECRET", raising=False)
    with pytest.raises(of.OAuthFlowError, match="OTO_MCP_OAUTH_STATE_SECRET"):
        of.sign_state("zoho", {})


# --- URI de redirection --------------------------------------------------------

def test_redirect_uri_is_absolute_and_slash_safe():
    assert of.redirect_uri("/api/zoho/oauth/callback") == \
        "https://mcp.oto.cx/api/zoho/oauth/callback"
    assert of.redirect_uri("api/zoho/oauth/callback") == \
        "https://mcp.oto.cx/api/zoho/oauth/callback"


# --- échange du code -----------------------------------------------------------

class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p = status, payload

    def json(self):
        if self._p is None:
            raise ValueError("no json")
        return self._p


def test_secrets_go_in_the_body_never_the_url(monkeypatch):
    """Incident #284 : en query string, les secrets entrent dans l'URL — donc dans
    tout message d'erreur, les logs, et chez le fournisseur."""
    seen = {}
    monkeypatch.setattr(of.requests, "post", lambda url, **kw: (
        seen.update(url=url, kw=kw), _Resp(200, {"access_token": "at"}))[1])
    of.exchange_code("https://accounts.zoho.eu/oauth/v2/token", code="c",
                     client_id="cid", client_secret="SECRET", redirect="https://r")
    assert "params" not in seen["kw"]
    assert seen["kw"]["data"]["client_secret"] == "SECRET"
    assert "SECRET" not in seen["url"]


def test_http_200_with_error_body_is_an_error(monkeypatch):
    """Zoho (et d'autres) répondent 200 avec l'erreur DANS le corps."""
    monkeypatch.setattr(of.requests, "post",
                        lambda url, **kw: _Resp(200, {"error": "invalid_code"}))
    with pytest.raises(of.OAuthFlowError, match="invalid_code"):
        of.exchange_code("https://accounts.zoho.eu/oauth/v2/token", code="c",
                         client_id="cid", client_secret="s", redirect="https://r")


def test_error_message_carries_no_secret(monkeypatch):
    monkeypatch.setattr(of.requests, "post",
                        lambda url, **kw: _Resp(400, {"error": "invalid_client"}))
    with pytest.raises(of.OAuthFlowError) as e:
        of.exchange_code("https://accounts.zoho.eu/oauth/v2/token", code="c",
                         client_id="cid", client_secret="SECRET", redirect="https://r")
    msg = str(e.value)
    assert "SECRET" not in msg and "cid" not in msg
    assert "accounts.zoho.eu" in msg          # le HOST reste, il aide au diagnostic


def test_unreadable_response_is_explicit(monkeypatch):
    monkeypatch.setattr(of.requests, "post", lambda url, **kw: _Resp(502, None))
    with pytest.raises(of.OAuthFlowError, match="illisible"):
        of.exchange_code("https://x/token", code="c", client_id="i",
                         client_secret="s", redirect="https://r")


# --- retour vers le front qui a demandé la connexion ---------------------------

@pytest.fixture
def front_tiers(monkeypatch):
    """Deux entrées FICTIVES dans la liste fermée, le temps du test. Le mécanisme
    (lookup, gabarit de chemin, substitution de `{org}`) est le même quelle que
    soit l'entrée — l'exercer sur un front de test plutôt que sur l'identité d'un
    tenant réel évite de recopier ici les coordonnées d'un client."""
    monkeypatch.setitem(of.RETURN_APPS, "acme",
                        ("https://app.acme.test", "/org/{org}/connectors"))
    monkeypatch.setitem(of.RETURN_APPS, "acme-preprod",
                        ("https://acme.oto.zone", "/org/{org}/connectors"))


def test_resolve_return_app_keeps_known_key(front_tiers):
    assert of.resolve_return_app("acme") == "acme"


@pytest.mark.parametrize("bad", [None, "", "oto", "app.acme.test", "https://app.acme.test"])
def test_resolve_return_app_rejects_unknown_or_missing(bad, front_tiers):
    """Jamais une valeur de client prise telle quelle : hors de `RETURN_APPS`, chaîne
    vide — pas d'origine arbitraire, pas d'open redirect. Y compris quand la valeur
    est l'ORIGINE d'une app pourtant connue : c'est la clé qui est listée, pas l'URL."""
    assert of.resolve_return_app(bad) == ""


def test_return_url_known_app_substitutes_org(front_tiers):
    url = of.return_url("acme", "?connector=salesforce&salesforce=connected", org=178)
    assert url == "https://app.acme.test/org/178/connectors?connector=salesforce&salesforce=connected"


def test_return_url_preprod_app(front_tiers):
    url = of.return_url("acme-preprod", "?connector=zoho&zoho=connected", org=3)
    assert url == "https://acme.oto.zone/org/3/connectors?connector=zoho&zoho=connected"


def test_return_url_unknown_app_falls_back_to_oto_dashboard():
    """Comportement historique byte-à-byte : `app` vide/inconnu ⇒ `OTO_APP_URL`
    (défaut dashboard.oto.ninja) + `/connectors`, exactement ce que chaque
    `_app_url()`/`_retour()` de route callback faisait seul avant ce module."""
    url = of.return_url("", "?connector=salesforce&salesforce=connected", org=178)
    from oto_mcp import config as _cfg
    assert url == (f"{_cfg.dashboard_url()}/connectors"
                   "?connector=salesforce&salesforce=connected")


def test_return_url_respects_oto_app_url_override(monkeypatch):
    monkeypatch.setenv("OTO_APP_URL", "https://dashboard.oto.cx")
    url = of.return_url(None, "?connector=zoho&zoho=connected")
    assert url == "https://dashboard.oto.cx/connectors?connector=zoho&zoho=connected"


# --- LA convention unique de retour OAuth (oto-backend#670) --------------------
#
# Un seul fabricant remplace les cinq suffixes composés à la main dans
# `api/salesforce.py`, `api/zoho.py`, `api/atlassian.py`, `api/folk.py` et
# `api/datastore.py` — dont deux replis cassés (f-string à accolades doublées).

def test_connector_return_suffix_forme_de_base():
    assert of.connector_return_suffix("salesforce", "connected") == \
        "?connector=salesforce&connect=connected"


def test_connector_return_suffix_double_pendant_le_preavis(monkeypatch):
    """zoho/google servent déjà un suffixe LU par le dashboard : il doit coexister
    avec le neuf dans la MÊME query string, pas dans une seconde redirection."""
    monkeypatch.setattr(of.deprecations, "dans_le_preavis_retour_oauth", lambda: True)
    suffix = of.connector_return_suffix("zoho", "connected", legacy=("zoho", "connected"))
    assert suffix == "?connector=zoho&connect=connected&zoho=connected"


def test_connector_return_suffix_arrete_de_doubler_apres_le_preavis(monkeypatch):
    monkeypatch.setattr(of.deprecations, "dans_le_preavis_retour_oauth", lambda: False)
    suffix = of.connector_return_suffix("zoho", "connected", legacy=("zoho", "connected"))
    assert suffix == "?connector=zoho&connect=connected"


def test_connector_return_suffix_sans_legacy_ne_double_rien():
    """atlassian/folk : `connector=` existait déjà, `connect=` est un pur ajout —
    rien à doubler, donc pas de `legacy` à passer."""
    assert of.connector_return_suffix("atlassian", "error") == \
        "?connector=atlassian&connect=error"


def test_connector_return_url_compose_base_et_suffixe(front_tiers):
    url = of.connector_return_url("acme", "zoho", "connected", org=7,
                                  legacy=("zoho", "connected"))
    assert url == ("https://app.acme.test/org/7/connectors"
                   "?connector=zoho&connect=connected&zoho=connected")


def test_avec_connect_ajoute_le_parametre_a_une_url_existante():
    assert of.avec_connect("https://x/connectors?connector=atlassian", "connected") == \
        "https://x/connectors?connector=atlassian&connect=connected"


def test_avec_connect_pose_le_point_dinterrogation_si_absent():
    assert of.avec_connect("https://x/connectors", "error") == \
        "https://x/connectors?connect=error"


# --- la duplication doit DÉCROÎTRE --------------------------------------------

def _modules_with(pattern: str) -> list[str]:
    """Modules qui réécrivent la MÉCANIQUE (pas seulement le nom d'une fonction :
    un module migré garde des enveloppes minces `verify_state`/`exchange_code` qui
    délèguent — les compter ferait mentir la mesure)."""
    import pathlib, re
    root = pathlib.Path(__file__).resolve().parents[2] / "oto_mcp"
    return sorted(p.name for p in root.glob("*.py")
                  if p.name != "oauth_flow.py"
                  and re.search(pattern, p.read_text(encoding="utf-8")))


def test_oauth_duplication_only_shrinks():
    """Mesuré le 2026-07-28, AVANT la fabrique : `verify_state` ×6, `exchange_code`
    ×5, `build_auth_url` ×5, `_state_secret` ×5 — cinq flux réécrivant la même danse,
    chacun re-découvrant les mêmes pièges (secrets en query string, TTL, erreurs
    opaques, state sans audience).

    `zoho_oauth` est migré ; google, memento, folk/atlassian (via `oauth2_pkce`)
    restent. Ces plafonds ne doivent que BAISSER : un nouveau flux qui réécrit au
    lieu d'utiliser la fabrique casse la CI. Ce test ne prétend pas que la migration
    est finie — il empêche qu'elle recule."""
    signing = _modules_with(r"hmac\.new\(")
    exchanging = _modules_with(r"grant_type.{0,4}:.{0,4}.authorization_code")
    assert len(signing) <= 4, (
        f"Nouvelle signature de state écrite à la main : {signing}. Utilise "
        "`oauth_flow.sign_state` / `read_state` (state LIÉ à son audience).")
    assert len(exchanging) <= 6, (
        f"Nouvel échange de code écrit à la main : {exchanging}. Utilise "
        "`oauth_flow.exchange_code` (corps form-encodé, erreurs rédigées).")


def test_zoho_no_longer_reimplements_the_flow():
    """Régression de la migration du jour : Zoho ne signe plus, n'échange plus, ne
    dérive plus son URI — il délègue."""
    import inspect
    from oto_mcp.auth import zoho as zoho_oauth
    src = inspect.getsource(zoho_oauth)
    assert "hmac.new(" not in src and "base64.urlsafe" not in src
    assert "oauth_flow.sign_state" in src and "oauth_flow.read_state" in src
    assert "oauth_flow.exchange_code" in src and "oauth_flow.redirect_uri" in src
