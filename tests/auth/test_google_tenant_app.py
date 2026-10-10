"""L'app Google d'un TENANT — son client, son écran de consentement, son rappel.

Un partenaire (Tulina) veut que ses utilisateurs consentent sous SA marque, dans SON
projet Google Cloud. Le module Google lisait un seul client, dans l'env : l'app de la
plateforme, pour tout le monde. Ce lot le fait lire l'app d'ÉDITEUR du connecteur
`google` rangée dans l'espace de noms des tenants (`editor:tenant:tulina`) — le cran que zoho employait
déjà par région — et pose le rappel sur le host déclaré du tenant, puisqu'un client
OAuth n'accepte que les domaines de son propriétaire.

Trois choses à figer :
1. **l'app et le rappel vont ensemble** (consentement, échange du code, refresh) — les
   séparer est un `redirect_uri_mismatch` ou un `invalid_client` opaque ;
2. **sans app posée, rien ne change** : l'env, notre rappel, à l'octet près ;
3. **une erreur de coffre remonte** — elle ne fait pas retomber un partenaire sous notre
   marque sans un mot (même leçon que `zoho_oauth.app_fields`, silences B7).

Et la régression trouvée en chemin : `pack_secret` sur `google` (schéma VIDE, flux
OAuth dédié) ne gardait que la première valeur — le `client_secret` d'une app d'éditeur
Google était perdu à la pose. Le blob d'une app d'éditeur a désormais sa forme propre.
"""
from __future__ import annotations

import json
import os
from urllib.parse import parse_qs, urlsplit

import pytest

from oto_mcp import access, credentials_store, tenancy  # noqa: E402
from oto_mcp.auth import google as google_oauth  # noqa: E402
from oto_mcp.capabilities import editor_apps  # noqa: E402


# ⚠️ Plus d'écriture d'environnement à l'IMPORT (#1111) : un `os.environ.setdefault` ici
# valait pour tout le processus dès la collecte, et des bancs d'autres fichiers ne
# passaient que grâce à lui. Une fixture se déclare, se voit et s'annule.
@pytest.fixture(autouse=True)
def _env_google(monkeypatch):
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "cid-env")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "secret-env")
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "state-secret-test")


# Ces bancs démarrent un consentement : la garde des connecteurs coupés lit la base.
pytestmark = pytest.mark.usefixtures("connecteurs_tous_disponibles")

TULINA = tenancy.TenantIssuer(
    slug="tulina", issuer="https://auth.tulina.ai/oidc",
    jwks_uri="https://auth.tulina.ai/oidc/jwks", name="Tulina",
    hosts=("mcp.tulina.ai",))
# Un tenant qui a une app mais AUCUN host déclaré : le rappel ne peut être que le nôtre.
ACME = tenancy.TenantIssuer(
    slug="acme", issuer="https://auth.acme.test/oidc",
    jwks_uri="https://auth.acme.test/oidc/jwks", name="Acme", hosts=())

RAPPEL_TULINA = "https://mcp.tulina.ai/api/google/oauth/callback"
RAPPEL_NOTRE = "https://mcp.oto.cx/api/google/oauth/callback"


@pytest.fixture()
def registre(monkeypatch):
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "cid-env")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "secret-env")
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "state-secret-test")
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.oto.cx")
    monkeypatch.setattr(tenancy, "_INSTALLED", tenancy.IssuerRegistry(
        {TULINA.issuer: TULINA, ACME.issuer: ACME}))
    monkeypatch.setattr(access, "current_org", lambda sub: 7)


def _coffre(monkeypatch, apps: dict) -> None:
    """Le coffre, réduit aux apps d'éditeur posées : `{(connecteur, clé): app}`."""
    monkeypatch.setattr(credentials_store, "get_editor_app",
                        lambda connector, key: apps.get((connector, key)))


APP_TULINA = {"client_id": "cid-tulina", "client_secret": "secret-tulina"}
APP_ACME = {"client_id": "cid-acme", "client_secret": "secret-acme"}


def _params(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


# ─── 1. l'app et le rappel vont ensemble ──────────────────────────────────────

def test_un_compte_du_tenant_consent_sous_l_app_du_tenant_et_rappelle_chez_lui(
        registre, monkeypatch):
    _coffre(monkeypatch, {("google", "tenant:tulina"): APP_TULINA})
    p = _params(google_oauth.build_auth_url("tulina:abc"))
    assert p["client_id"] == "cid-tulina"
    assert p["redirect_uri"] == RAPPEL_TULINA
    app = google_oauth.app_for("tulina:abc")
    assert app.origin == "tenant:tulina" and app.client_secret == "secret-tulina"


def test_l_echange_du_code_et_le_refresh_emploient_la_meme_app(registre, monkeypatch):
    """Le code ne s'échange qu'avec le client qui a demandé le consentement, et son
    rappel exact ; le jeton ne se rafraîchit qu'avec lui."""
    _coffre(monkeypatch, {("google", "tenant:tulina"): APP_TULINA})
    envois: list[dict] = []

    class _OK:
        status_code, text = 200, ""

        def json(self):
            return {"access_token": "at", "expires_in": 3600}

        def raise_for_status(self):
            pass

    import requests
    monkeypatch.setattr(requests, "post",
                        lambda url, data=None, **k: envois.append(data) or _OK())

    google_oauth.exchange_code("le-code", "tulina:abc")
    google_oauth._refresh_access_token("rt", "tulina:abc")
    google_oauth.exchange_code("le-code", "nu-sub")
    google_oauth._refresh_access_token("rt", "nu-sub")

    assert [e["client_id"] for e in envois] == ["cid-tulina", "cid-tulina",
                                                "cid-env", "cid-env"]
    assert [e["client_secret"] for e in envois] == ["secret-tulina", "secret-tulina",
                                                    "secret-env", "secret-env"]
    assert envois[0]["redirect_uri"] == RAPPEL_TULINA
    assert envois[2]["redirect_uri"] == RAPPEL_NOTRE


def test_credentials_for_rafraichit_et_instancie_avec_l_app_du_tenant(
        registre, monkeypatch):
    """Le chemin servi aux outils : le refresh transparent ET l'objet `Credentials`
    (qui rafraîchit aussi de lui-même, côté googleapiclient) portent l'app du tenant."""
    _coffre(monkeypatch, {("google", "tenant:tulina"): APP_TULINA})
    # Un jeton ÉMIS par l'app du tenant (le client noté à la pose) : sans cette note,
    # il serait réputé émis par la nôtre et refusé (« reconnecte »).
    from _coffre_google import installer

    env = installer(monkeypatch, org=7, sub="tulina:abc")
    env.coffre.poser("a@b.com", "RT", defaut=True, scopes="s1", access_token=None,
                     expires_at=None, client_id="cid-tulina")
    _coffre(monkeypatch, {("google", "tenant:tulina"): APP_TULINA})
    from oto_mcp.connectors import health as connector_health
    monkeypatch.setattr(connector_health, "record_health", lambda *a, **k: None)
    envois: list[dict] = []

    class _OK:
        status_code, text = 200, ""

        def json(self):
            return {"access_token": "at-neuf", "expires_in": 3600}

        def raise_for_status(self):
            pass

    import requests
    monkeypatch.setattr(requests, "post",
                        lambda url, data=None, **k: envois.append(data) or _OK())

    creds = google_oauth.credentials_for("tulina:abc", account="a@b.com")

    assert envois[0]["client_id"] == "cid-tulina"
    assert creds.client_id == "cid-tulina" and creds.client_secret == "secret-tulina"
    assert creds.token == "at-neuf"


# ─── 2. sans app posée, rien ne change ────────────────────────────────────────

@pytest.mark.parametrize("sub", ["tulina:abc", "nu-sub"])
def test_sans_app_posee_le_tenant_comme_le_primaire_restent_sur_la_notre(
        registre, monkeypatch, sub):
    _coffre(monkeypatch, {})
    p = _params(google_oauth.build_auth_url(sub))
    assert p["client_id"] == "cid-env"
    assert p["redirect_uri"] == RAPPEL_NOTRE
    assert google_oauth.app_for(sub).origin == "env"


def test_l_app_d_un_autre_tenant_ne_sert_jamais(registre, monkeypatch):
    """L'app de Tulina n'est pas celle d'Acme, ni celle du tenant primaire : la clé est
    le slug LU SUR LE SUB, jamais « une app quelque part »."""
    _coffre(monkeypatch, {("google", "tenant:tulina"): APP_TULINA})
    assert google_oauth.app_for("acme:xyz").client_id == "cid-env"
    assert google_oauth.app_for("nu-sub").client_id == "cid-env"


def test_le_tenant_primaire_ne_sonde_jamais_le_coffre(registre, monkeypatch):
    """Son app EST l'env (`tenant_vault.rung_tenant`, même règle) : un coffre qui
    lèverait — ou une ligne `editor:oto` posée par erreur — ne le concerne pas. C'est
    aussi ce qui garde le refresh d'un sub nu sans AUCUNE lecture de base."""
    def _jamais(connector, key):
        raise AssertionError(f"coffre sondé pour le primaire : {connector}/{key}")
    monkeypatch.setattr(credentials_store, "get_editor_app", _jamais)
    app = google_oauth.app_for("nu-sub")
    assert app.client_id == "cid-env" and app.redirect_uri == RAPPEL_NOTRE


def test_une_app_sans_host_declare_rappelle_chez_nous(registre, monkeypatch):
    """Sans host chez le tenant, le seul rappel possible est le nôtre — à lui de le
    déclarer chez Google (domaine autorisé). Nommé plutôt que deviné."""
    _coffre(monkeypatch, {("google", "tenant:acme"): APP_ACME})
    app = google_oauth.app_for("acme:xyz")
    assert app.client_id == "cid-acme" and app.redirect_uri == RAPPEL_NOTRE


# ─── 3. une erreur de coffre remonte ──────────────────────────────────────────

def test_une_erreur_de_coffre_remonte_sans_retomber_sur_l_env(registre, monkeypatch):
    def _casse(connector, key):
        raise credentials_store.SecretUnpackError("blob illisible")
    monkeypatch.setattr(credentials_store, "get_editor_app", _casse)
    with pytest.raises(credentials_store.SecretUnpackError):
        google_oauth.build_auth_url("tulina:abc")


# ─── le coffre garde les DEUX champs, quel que soit le schéma du connecteur ────

class _Conn:
    def __init__(self, row):
        self.row = row

    def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return self.row


class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


def test_l_app_d_editeur_google_garde_son_client_secret(monkeypatch):
    """La régression : `pack_secret("google", …)` ne garde que la première valeur
    (schéma vide) — le secret était perdu et `get_editor_app` rendait `None`."""
    perdu = credentials_store.pack_secret("google", dict(APP_TULINA))
    assert "secret-tulina" not in perdu, "contre-épreuve : pack_secret perd bien le secret"

    stocke: dict = {}
    monkeypatch.setattr(credentials_store, "_upsert",
                        lambda conn, et, eid, c, a, secret, set_by, meta:
                        stocke.__setitem__((et, eid, c), secret))
    monkeypatch.setattr(credentials_store, "_connect",
                        lambda: _Ctx(_Conn({"secret_enc": b"chiffre"})))
    monkeypatch.setattr(credentials_store, "_reveal",
                        lambda row, et, eid, c, a: stocke[(et, eid, c)])

    cle = credentials_store.tenant_app_key("tulina")
    credentials_store.set_editor_app("google", cle, dict(APP_TULINA))
    blob = stocke[(credentials_store.PLATFORM, "editor:tenant:tulina", "google")]
    assert json.loads(blob) == APP_TULINA
    assert credentials_store.get_editor_app("google", cle) == APP_TULINA


def test_le_blob_d_editeur_est_celui_que_zoho_ecrivait_deja():
    """Back-compat : les apps zoho posées avant ce lot se relisent telles quelles —
    le JSON explicite est byte-à-byte ce que `pack_secret` produisait pour ≥ 2 champs."""
    fields = {"client_id": "ci", "client_secret": "cs"}
    assert credentials_store._pack_editor_app(fields) == credentials_store.pack_secret(
        "zoho", fields)


def test_un_blob_d_editeur_illisible_est_une_erreur_de_coffre():
    with pytest.raises(credentials_store.SecretUnpackError):
        credentials_store._unpack_editor_app("pas du json")
    with pytest.raises(credentials_store.SecretUnpackError):
        credentials_store._unpack_editor_app("[1, 2]")


# ─── la pose nomme le rappel à déclarer chez le fournisseur ───────────────────

class _Ctx2:
    sub = "admin-sub"


def test_la_pose_sur_un_tenant_rend_le_rappel_du_tenant(registre, monkeypatch):
    """Ce que l'admin doit déclarer chez Google est le rappel que le flux ENVERRA :
    celui du tenant pour une app keyée `tenant:<slug>`, le nôtre sinon (région zoho)."""
    monkeypatch.setattr(credentials_store, "set_editor_app", lambda *a, **k: None)
    sur_tenant = editor_apps._set(_Ctx2(), editor_apps.SetInput(
        connector="google", data_center="tenant:tulina", client_id="x", client_secret="y"))
    assert sur_tenant["callback_url"] == RAPPEL_TULINA
    sur_region = editor_apps._set(_Ctx2(), editor_apps.SetInput(
        connector="google", data_center="eu", client_id="x", client_secret="y"))
    assert sur_region["callback_url"] == RAPPEL_NOTRE
