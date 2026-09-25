"""L'app OAuth d'un tenant — ce que la revue de #1063 a trouvé, figé.

Le cœur tenait (pose réservée à l'admin du tenant, secret au coffre, state HMAC lié au
sub, scopes fixes). Restaient des bords où la nouvelle surface débordait sur la
plateforme, ou cassait en silence :

1. **Liste fermée** : la face du tenant acceptait tout connecteur à consentement, alors
   que seul `google` lit une app rangée par tenant. Une app `zoho` posée par un tenant
   n'était lue par personne (zoho lit une RÉGION), mais suffisait à allumer le bouton
   « Autoriser oto chez Zoho » pour toute la plateforme (`_has_editor_app` compte les
   lignes du connecteur sans regarder la clé).
2. **Espace de noms** : régions zoho (`eu`, `com`…) et slugs de tenant partageaient
   `editor:<clé>`. Un tenant nommé `eu` aurait posé, lu ou retiré l'app Zoho de la
   plateforme pour la région `eu`.
3. **Jeton émis par une autre app** : poser, changer ou retirer l'app d'un tenant rend
   inutilisables les jetons émis par l'app précédente — le refresh doit passer par le
   client qui a émis le jeton. Rien ne le disait : le client refusé par Google finissait
   en `HTTPError`, que les outils Google ne traduisent pas (ils n'attrapent que
   `RuntimeError`) → « erreur interne », sans marque de santé ni « reconnecte ».
4. **Le rappel n'est posé que sur un host que ce tenant TIENT** : un host réclamé par
   deux tenants reste au premier ; le second ne doit jamais y faire rappeler Google.
5. **Scopes** : `include_granted_scopes` ramènerait, sous le client d'un partenaire, des
   scopes accordés à ses autres produits.
6. Le secret de l'app n'apparaît pas dans son `repr`.
7. `credentials_for` ne lit l'app qu'UNE fois par appel (lecture + déchiffrement du
   coffre pour un compte tenant).
"""
from __future__ import annotations

import os
from urllib.parse import parse_qs, urlsplit

import pytest

os.environ.setdefault("GOOGLE_WORKSPACE_CLIENT_ID", "cid-env")
os.environ.setdefault("GOOGLE_WORKSPACE_CLIENT_SECRET", "secret-env")
os.environ.setdefault("OTO_MCP_OAUTH_STATE_SECRET", "state-secret-test")
os.environ.setdefault("OTO_MCP_PUBLIC_URL", "https://mcp.oto.cx")

from oto_mcp import access, credentials_store, tenancy  # noqa: E402
from oto_mcp.auth import google as google_oauth  # noqa: E402
from oto_mcp.tools import zoho as _zoho  # noqa: E402,F401 — déclare le flux zoho
from oto_mcp.capabilities import editor_apps  # noqa: E402
from oto_mcp.capabilities import tenant_apps as tap  # noqa: E402
from oto_mcp.capabilities import tenant_keys as tk  # noqa: E402
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx  # noqa: E402
from oto_mcp.connectors import health as connector_health  # noqa: E402

CTX = ResolvedCtx(sub="operateur", role="super_admin")
TULINA = tenancy.TenantIssuer(
    slug="tulina", issuer="https://auth.tulina.ai/oidc",
    jwks_uri="https://auth.tulina.ai/oidc/jwks", name="Tulina", hosts=("mcp.tulina.ai",))
# Un tenant dont le slug est aussi une RÉGION zoho.
EU = tenancy.TenantIssuer(
    slug="eu", issuer="https://auth.eu.test/oidc",
    jwks_uri="https://auth.eu.test/oidc/jwks", name="Eu", hosts=("mcp.eu.test",))
# Déclare en PREMIER un host que Tulina tient déjà : sa réclamation est ignorée.
INTRUS = tenancy.TenantIssuer(
    slug="intrus", issuer="https://auth.intrus.test/oidc",
    jwks_uri="https://auth.intrus.test/oidc/jwks", name="Intrus",
    hosts=("mcp.tulina.ai", "mcp.intrus.test"))

RAPPEL_TULINA = "https://mcp.tulina.ai/api/google/oauth/callback"
RAPPEL_NOTRE = "https://mcp.oto.cx/api/google/oauth/callback"
APP_TULINA = {"client_id": "cid-tulina", "client_secret": "secret-tulina"}
APP_INTRUS = {"client_id": "cid-intrus", "client_secret": "secret-intrus"}


@pytest.fixture()
def registre(monkeypatch):
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "cid-env")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "secret-env")
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "state-secret-test")
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.oto.cx")
    # Tulina d'abord : c'est elle qui tient `mcp.tulina.ai`.
    monkeypatch.setattr(tenancy, "_INSTALLED", tenancy.IssuerRegistry(
        {TULINA.issuer: TULINA, EU.issuer: EU, INTRUS.issuer: INTRUS}))
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    monkeypatch.setattr(tk.db, "tenant_exists",
                        lambda slug: slug in ("tulina", "eu", "intrus", "oto"))


def _coffre(monkeypatch, apps: dict) -> list:
    """Le coffre réduit aux apps d'éditeur posées ; rend la liste des lectures."""
    lectures: list = []

    def _get(connector, key):
        lectures.append((connector, key))
        return apps.get((connector, key))
    monkeypatch.setattr(credentials_store, "get_editor_app", _get)
    return lectures


def _params(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


# ─── 1. liste fermée ──────────────────────────────────────────────────────────

def test_un_tenant_ne_pose_pas_d_app_sur_un_connecteur_qui_ne_la_lit_pas(
        registre, monkeypatch):
    ecrit = []
    monkeypatch.setattr(credentials_store, "set_editor_app", lambda *a, **k: ecrit.append(a))
    with pytest.raises(AuthzDenied) as e:
        tap._set_app(CTX, tap.TenantAppSetInput(
            slug="tulina", connector="zoho", client_id="cid", client_secret="sec"))
    assert e.value.status == 400 and e.value.code == "tenant_app_unsupported"
    assert ecrit == [], "rien ne doit être écrit au coffre"


def test_un_tenant_ne_retire_pas_d_app_sur_un_connecteur_qui_ne_la_lit_pas(
        registre, monkeypatch):
    retire = []
    monkeypatch.setattr(credentials_store, "clear_editor_app",
                        lambda *a, **k: retire.append(a) or True)
    with pytest.raises(AuthzDenied) as e:
        tap._clear_app(CTX, tap.TenantAppClearInput(slug="eu", connector="zoho"))
    assert e.value.status == 400 and e.value.code == "tenant_app_unsupported"
    assert retire == []


def test_eligible_nomme_seulement_les_connecteurs_qui_lisent_l_app_du_tenant(
        registre, monkeypatch):
    from oto_mcp.connectors import flow as connector_flow
    assert connector_flow.supports("zoho"), "banc : le flux zoho doit être déclaré"
    monkeypatch.setattr(credentials_store, "list_editor_apps", lambda connector=None: [])
    assert tap._list_apps(CTX, tap.TenantAppsInput(slug="tulina"))["eligible"] == ["google"]


# ─── 2. un espace de noms à part ──────────────────────────────────────────────

def test_l_app_d_un_tenant_ne_se_range_jamais_sous_une_cle_de_region(
        registre, monkeypatch):
    """Un tenant `eu` pose, lit et retire SON app — jamais l'app Zoho de la région `eu`."""
    vu = {}
    monkeypatch.setattr(credentials_store, "set_editor_app",
                        lambda connector, key, fields, set_by=None: vu.update(pose=key))
    monkeypatch.setattr(credentials_store, "clear_editor_app",
                        lambda connector, key: vu.update(retrait=key) or True)
    tap._set_app(CTX, tap.TenantAppSetInput(
        slug="eu", connector="google", client_id="cid", client_secret="sec"))
    tap._clear_app(CTX, tap.TenantAppClearInput(slug="eu", connector="google"))
    assert vu == {"pose": "tenant:eu", "retrait": "tenant:eu"}


def test_la_liste_du_tenant_ignore_une_region_homonyme(registre, monkeypatch):
    monkeypatch.setattr(credentials_store, "list_editor_apps", lambda connector=None: [
        {"connector": "google", "data_center": "eu", "set_at": None},
        {"connector": "zoho", "data_center": "eu", "set_at": None},
        {"connector": "google", "data_center": "tenant:eu", "set_at": None}])
    out = tap._list_apps(CTX, tap.TenantAppsInput(slug="eu"))
    assert [a["connector"] for a in out["apps"]] == ["google"]


def test_app_for_lit_l_app_du_tenant_dans_son_espace_de_noms(registre, monkeypatch):
    _coffre(monkeypatch, {("google", "tenant:tulina"): APP_TULINA,
                          ("google", "tulina"): {"client_id": "piege", "client_secret": "x"}})
    assert google_oauth.app_for("tulina:abc").client_id == "cid-tulina"


def test_une_cle_de_region_n_est_jamais_lue_comme_un_slug(registre, monkeypatch):
    """La face plateforme : poser une app zoho pour la région `eu` rend NOTRE rappel,
    même s'il existe un tenant nommé `eu` ; seule `tenant:<slug>` désigne un tenant."""
    monkeypatch.setattr(credentials_store, "set_editor_app", lambda *a, **k: None)
    region = editor_apps._set(CTX, editor_apps.SetInput(
        connector="zoho", data_center="eu", client_id="x", client_secret="y"))
    assert "mcp.eu.test" not in (region["callback_url"] or "")
    tenant = editor_apps._set(CTX, editor_apps.SetInput(
        connector="google", data_center="tenant:tulina", client_id="x", client_secret="y"))
    assert tenant["callback_url"] == RAPPEL_TULINA


# ─── 3. un jeton émis par une autre app ───────────────────────────────────────

@pytest.fixture()
def cablage(registre, monkeypatch):
    """`credentials_for` sans base ni réseau : la ligne du coffre, les marques."""
    row = {"google_email": "a@b.com", "refresh_token": "RT", "access_token": "at-valide",
           "expires_at": "2999-01-01T00:00:00+00:00", "scopes": "s1"}
    monkeypatch.setattr(google_oauth.db, "get_google_oauth",
                        lambda sub, org, account=None: row)
    appels = {"mark": [], "post": []}
    monkeypatch.setattr(connector_health, "mark_rejected",
                        lambda et, eid, prov, acct, err: appels["mark"].append(acct))
    monkeypatch.setattr(connector_health, "record_health", lambda *a, **k: None)
    monkeypatch.setattr(google_oauth.db, "update_google_access_token", lambda *a, **k: None)
    import requests
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: appels["post"].append(k) or pytest.fail("réseau"))
    return row, appels


def test_un_jeton_d_avant_la_pose_demande_de_reconnecter(cablage, monkeypatch):
    """Jeton sans client émetteur noté = émis par NOTRE app. L'app du tenant est
    posée : il ne se rafraîchira plus → « reconnecte », compte marqué, aucun réseau."""
    row, appels = cablage
    _coffre(monkeypatch, {("google", "tenant:tulina"): APP_TULINA})
    with pytest.raises(google_oauth.GoogleReauthRequired) as e:
        google_oauth.credentials_for("tulina:abc", account="a@b.com")
    assert isinstance(e.value, RuntimeError)
    assert "a@b.com" in str(e.value) and "reconnecte" in str(e.value)
    assert appels["mark"] == ["a@b.com"] and appels["post"] == []


def test_un_jeton_de_l_app_retiree_demande_de_reconnecter(cablage, monkeypatch):
    row, appels = cablage
    row["client_id"] = "cid-tulina"
    _coffre(monkeypatch, {})
    with pytest.raises(google_oauth.GoogleReauthRequired):
        google_oauth.credentials_for("tulina:abc", account="a@b.com")
    assert appels["mark"] == ["a@b.com"]


def test_un_jeton_de_l_app_courante_sert(cablage, monkeypatch):
    row, appels = cablage
    row["client_id"] = "cid-tulina"
    _coffre(monkeypatch, {("google", "tenant:tulina"): APP_TULINA})
    creds = google_oauth.credentials_for("tulina:abc", account="a@b.com")
    assert creds.client_id == "cid-tulina" and appels["mark"] == []


def test_un_jeton_d_avant_sans_app_de_tenant_sert_comme_avant(cablage, monkeypatch):
    """Le chemin plateforme : aucun client noté, aucune app de tenant → rien ne change."""
    row, appels = cablage
    _coffre(monkeypatch, {})
    creds = google_oauth.credentials_for("nu-sub", account="a@b.com")
    assert creds.client_id == "cid-env" and appels["mark"] == []


def test_la_pose_d_un_jeton_note_son_client_emetteur(registre, monkeypatch):
    vu = {}
    monkeypatch.setattr(google_oauth, "_fetch_email", lambda at, scopes=(): "a@b.com")
    monkeypatch.setattr(google_oauth.db, "set_google_oauth",
                        lambda *a, **k: vu.update(k))
    google_oauth.persist_token("tulina:abc", 7, {"refresh_token": "RT",
                                                 "access_token": "at", "expires_in": 3600},
                               client_id="cid-tulina")
    assert vu["client_id"] == "cid-tulina"


class _Rep:
    def __init__(self, status, text):
        self.status_code, self.text = status, text

    def json(self):
        return {}

    def raise_for_status(self):
        import requests
        raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.mark.parametrize("code", ["unauthorized_client", "invalid_client"])
def test_un_client_refuse_au_refresh_est_un_refus_nomme(registre, monkeypatch, code):
    """Le client refusé par Google ne finit plus en `HTTPError` (« erreur interne » côté
    outil) : c'est une `RuntimeError` nommée — mais PAS un grant mort (config fausse ≠
    grant révoqué : on ne marque pas, cf. `oauth_flow.grant_is_dead`)."""
    _coffre(monkeypatch, {})
    import requests
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: _Rep(401, f'{{"error":"{code}"}}'))
    with pytest.raises(google_oauth.GoogleClientRejected) as e:
        google_oauth._refresh_access_token("rt", "nu-sub")
    assert isinstance(e.value, RuntimeError)
    assert not isinstance(e.value, google_oauth.GoogleReauthRequired)
    assert code in str(e.value)


# ─── 4. le rappel n'est posé que sur un host tenu ─────────────────────────────

def test_un_host_reclame_mais_tenu_par_un_autre_ne_recoit_jamais_le_rappel(
        registre, monkeypatch):
    _coffre(monkeypatch, {("google", "tenant:intrus"): APP_INTRUS,
                          ("google", "tenant:tulina"): APP_TULINA})
    assert google_oauth.app_for("intrus:x").redirect_uri != RAPPEL_TULINA
    assert google_oauth.app_for("tulina:abc").redirect_uri == RAPPEL_TULINA


# ─── 5. scopes ────────────────────────────────────────────────────────────────

def test_l_app_d_un_tenant_demande_l_identite_seule_et_le_coffre_filtre(
        registre, monkeypatch):
    """Révisé au split du 2026-09-26. La garde « pas d'`include_granted_scopes` sous
    l'app d'un tenant » tenait tant qu'un consentement demandait tout d'un coup ;
    depuis que chaque service demande SES scopes sur le même compte, l'autorisation
    incrémentale est ce qui fait tenir le split — sous tout client. Ce qui reste
    garanti, et se vérifie ailleurs : le COMPTE ne demande que l'identité sous l'app
    d'un tenant (`scopes_for`), et un scope étranger n'entre jamais au coffre
    (`persist_token` filtre sur `KNOWN_SCOPES`, `test_google_split.py`)."""
    _coffre(monkeypatch, {("google", "tenant:tulina"): APP_TULINA})
    tenant = _params(google_oauth.build_auth_url("tulina:abc"))
    assert set(tenant["scope"].split()) == set(google_oauth.IDENTITY_SCOPES)
    assert tenant["include_granted_scopes"] == "true"
    notre = _params(google_oauth.build_auth_url("nu-sub"))
    assert set(google_oauth.SCOPES) <= set(notre["scope"].split())
    assert notre["include_granted_scopes"] == "true"


# ─── 6. le secret ne sort pas par le repr ─────────────────────────────────────

def test_le_secret_de_l_app_n_apparait_pas_dans_son_repr(registre, monkeypatch):
    app = google_oauth.OAuthApp(client_id="cid", client_secret="un-secret-a-ne-pas-voir",
                                redirect_uri=RAPPEL_TULINA, origin="tenant:tulina")
    assert "un-secret-a-ne-pas-voir" not in repr(app)


# ─── 7. une seule lecture du coffre par appel ─────────────────────────────────

def test_credentials_for_ne_lit_l_app_qu_une_fois(cablage, monkeypatch):
    row, _ = cablage
    row.update(client_id="cid-tulina", access_token=None, expires_at=None)

    class _OK:
        status_code, text = 200, ""

        def json(self):
            return {"access_token": "at-neuf", "expires_in": 3600}

        def raise_for_status(self):
            pass
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _OK())
    lectures = _coffre(monkeypatch, {("google", "tenant:tulina"): APP_TULINA})
    google_oauth.credentials_for("tulina:abc", account="a@b.com")
    assert len(lectures) == 1, lectures
