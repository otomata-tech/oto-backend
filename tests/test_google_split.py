"""Le split google (2026-09-26) : le compte + ses six services, chacun son consentement.

`google` portait six namespaces, donc UNE carte, UNE activation, UN consentement qui
demandait les six scopes d'un coup — dont trois RESTRICTED chez Google. Un tenant qui
n'offre que Gmail et Drive devait faire vérifier Chat et Tasks ; une personne qui ne
voulait que son agenda livrait sa boîte mail. Même mouvement que le split unipile
(`test_unipile_split.py`), avec une pièce en plus que unipile n'avait pas : le
consentement lui-même se scinde (`SERVICE_SCOPES`, autorisation incrémentale).

Ce que ce banc tient :
1. le REGISTRE — six connecteurs, chacun son namespace, son flux, la délégation au compte ;
2. les SCOPES — un service ne demande que les siens ; le compte, tout sous notre app et
   l'identité seule sous l'app d'un tenant ;
3. le STATE — le callback ramène sur la carte qui a demandé ;
4. le COFFRE — un compte qui n'a pas autorisé un service est refusé par ses outils en
   nommant la carte, jamais un 403 Google opaque ; l'état de lien d'une carte ne compte
   que les comptes qui l'ont autorisée.
"""
from __future__ import annotations

import os
from urllib.parse import parse_qs, urlsplit

import pytest

os.environ.setdefault("GOOGLE_WORKSPACE_CLIENT_ID", "cid-env")
os.environ.setdefault("GOOGLE_WORKSPACE_CLIENT_SECRET", "secret-env")
os.environ.setdefault("OTO_MCP_OAUTH_STATE_SECRET", "state-secret-test")
os.environ.setdefault("OTO_MCP_PUBLIC_URL", "https://mcp.oto.cx")

from oto_mcp import access, credentials_store, providers  # noqa: E402
from oto_mcp.auth import google as G  # noqa: E402
from oto_mcp.connectors import flow as connector_flow  # noqa: E402
from oto_mcp.connectors import identities  # noqa: E402
from oto_mcp.connectors import link as connector_link  # noqa: E402

SERVICES = ("gmail", "drive", "sheets", "calendar", "tasks", "chat")
EMAIL = "https://www.googleapis.com/auth/userinfo.email"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "cid-env")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "secret-env")
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "state-secret-test")
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.oto.cx")
    monkeypatch.setattr(access, "current_org", lambda sub: 7)


# ─── 1. le registre ───────────────────────────────────────────────────────────

def test_les_six_services_sont_des_connecteurs_et_le_compte_reste():
    for nom in SERVICES:
        assert nom in providers.REGISTRY, nom
    assert providers.REGISTRY["google"].namespaces == ("google",)
    assert providers.credential_provider("google") == "google"


@pytest.mark.parametrize("svc", SERVICES)
def test_un_service_emprunte_le_compte_et_garde_sa_couche_1(svc):
    con = providers.REGISTRY[svc]
    assert con.credential_of == "google"
    assert providers.credential_provider(svc) == "google"
    assert providers.delegates_credential(svc)
    assert con.namespaces == (svc,)
    assert providers.connector_for_namespace(svc) is con
    assert con.auth_method == "oauth" and con.auth_multi_account
    assert con.secret_fields == ()
    assert connector_flow.supports(svc)
    assert connector_flow.describe(svc)["params"] == []
    assert connector_link.has(svc)


def test_la_population_derivee_est_bien_celle_des_six_services():
    """RATCHET : ce que le registre DÉRIVE (`credential_of == "google"`) est exactement
    la liste que ce banc et le fan-out du boot attendent — un septième service entre
    ici et dans `selection.GOOGLE_SERVICES` en même temps, ou pas du tout."""
    from oto_mcp.connectors import selection
    derives = tuple(sorted(n for n, c in providers.REGISTRY.items()
                           if c.credential_of == "google"))
    assert derives == tuple(sorted(SERVICES))
    assert tuple(sorted(selection.GOOGLE_SERVICES)) == derives
    assert tuple(sorted(G.SERVICES)) == derives


# ─── 2. les scopes ────────────────────────────────────────────────────────────

def _app(origin):
    return G.OAuthApp(client_id="cid", client_secret="s", redirect_uri="https://x/cb",
                      origin=origin)


def test_un_service_ne_demande_que_ses_scopes_et_lidentite():
    drive = G.scopes_for("drive", _app("env"))
    assert drive == list(G.IDENTITY_SCOPES) + ["https://www.googleapis.com/auth/drive"]
    assert "gmail" not in " ".join(drive)
    assert set(G.scopes_for("chat", _app("tenant:tulina"))) == set(G.IDENTITY_SCOPES) | set(
        G.SERVICE_SCOPES["chat"])


def test_le_compte_demande_tout_sous_notre_app_et_lidentite_seule_sous_celle_dun_tenant():
    """Un partenaire ne demande jamais un scope que son projet Google ne déclare pas :
    ses services les ajoutent un à un."""
    assert set(G.scopes_for("google", _app("env"))) == set(G.IDENTITY_SCOPES) | set(G.SCOPES)
    assert G.scopes_for("google", _app("tenant:tulina")) == list(G.IDENTITY_SCOPES)


def test_un_connecteur_inconnu_nobtient_aucun_scope():
    with pytest.raises(RuntimeError):
        G.scopes_for("hunter", _app("env"))


def test_services_granted_lit_les_scopes_dun_compte():
    assert G.services_granted("https://www.googleapis.com/auth/drive " + EMAIL) == ["drive"]
    # Chat a DEUX scopes : un seul ne suffit pas.
    assert G.services_granted("https://www.googleapis.com/auth/chat.messages") == []
    assert G.services_granted(" ".join(G.SCOPES)) == list(G.SERVICES)
    assert G.services_granted(None) == []


def test_lurl_de_consentement_dun_service_porte_ses_scopes_et_sa_carte(monkeypatch):
    monkeypatch.setattr(credentials_store, "get_editor_app", lambda c, k: None)
    p = {k: v[0] for k, v in parse_qs(urlsplit(G.build_auth_url("nu-sub", connector="drive")).query).items()}
    assert set(p["scope"].split()) == set(G.IDENTITY_SCOPES) | {"https://www.googleapis.com/auth/drive"}
    assert p["include_granted_scopes"] == "true"
    assert G.verify_state(p["state"])[3] == "drive"


# ─── 3. le state ──────────────────────────────────────────────────────────────

def test_le_state_porte_la_carte_et_un_state_davant_revient_au_compte():
    etat = G.make_state("sub-1", 42, "tulina", "sheets")
    assert G.verify_state(etat) == ("sub-1", 42, "tulina", "sheets")
    assert G.verify_state(G.make_state("sub-1", 42))[3] == "google"
    # Un state forgé sur un connecteur inconnu ne passe pas, même bien signé.
    import base64, hashlib, hmac, json, time
    payload = json.dumps({"sub": "sub-1", "org": 42, "ts": int(time.time()), "app": "",
                          "c": "hunter"}, separators=(",", ":")).encode()
    sig = hmac.new(b"state-secret-test", payload, hashlib.sha256).digest()
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    assert G.verify_state(f"{b64(payload)}.{b64(sig)}") is None


@pytest.mark.asyncio
async def test_le_flux_dun_service_demande_ses_scopes_et_revient_sur_sa_carte(monkeypatch):
    vus = []
    monkeypatch.setattr(G, "build_auth_url",
                        lambda sub, return_app="", connector="google":
                        vus.append((sub, return_app, connector)) or "https://x")

    class _Ctx:
        sub = "user-1"
    out = await connector_flow.start("calendar", _Ctx(), {"app": "tulina"})
    assert out.auth_url == "https://x"
    assert vus == [("user-1", "tulina", "calendar")]


# ─── 4. le coffre ─────────────────────────────────────────────────────────────

def _row(scopes):
    return {"google_email": "a@b.com", "refresh_token": "RT", "access_token": "AT",
            "expires_at": "2999-01-01T00:00:00+00:00", "scopes": scopes, "client_id": None}


def test_un_compte_sans_le_scope_du_service_est_refuse_en_nommant_la_carte(monkeypatch):
    monkeypatch.setattr(G.db, "get_google_oauth",
                        lambda sub, org, account=None: _row("https://www.googleapis.com/auth/gmail.modify"))
    monkeypatch.setattr(G, "config_dashboard", lambda sub: "https://app.tulina.ai")
    with pytest.raises(RuntimeError) as e:
        G.credentials_for("nu-sub", account="a@b.com", service="drive")
    assert "Google Drive" in str(e.value) and "app.tulina.ai" in str(e.value)
    assert "a@b.com" in str(e.value)


def test_un_compte_qui_a_le_scope_passe_sans_rafraichir(monkeypatch):
    monkeypatch.setattr(credentials_store, "get_editor_app", lambda c, k: None)
    monkeypatch.setattr(G.db, "get_google_oauth",
                        lambda sub, org, account=None: _row(" ".join(G.SCOPES)))
    creds = G.credentials_for("nu-sub", account="a@b.com", service="drive")
    assert creds.token == "AT"
    # Sans `service` (un appelant d'avant le split), rien n'est vérifié : l'API
    # Google tranche, comme avant.
    assert G.credentials_for("nu-sub", account="a@b.com").token == "AT"


def test_letat_de_lien_dune_carte_ne_compte_que_les_comptes_qui_lont_autorisee(monkeypatch):
    monkeypatch.setattr(G, "list_accounts", lambda sub: [
        {"google_email": "a@b.com", "scopes": "https://www.googleapis.com/auth/gmail.modify",
         "set_at": "2026-09-26T10:00:00+00:00"},
        {"google_email": "c@d.com", "scopes": " ".join(G.SCOPES), "set_at": "2026-09-25T10:00:00+00:00"}])
    assert connector_link.state("gmail", "s").accounts == 2
    drive = connector_link.state("drive", "s")
    assert (drive.linked, drive.accounts, drive.set_at) == (True, 1, "2026-09-25T10:00:00+00:00")
    assert connector_link.state("chat", "s").accounts == 1
    assert connector_link.state("google", "s").accounts == 2


def test_les_identites_dune_carte_sont_les_comptes_qui_lont_autorisee(monkeypatch):
    monkeypatch.setattr(G, "list_accounts", lambda sub: [
        {"google_email": "a@b.com", "is_default": True,
         "scopes": "https://www.googleapis.com/auth/gmail.modify"},
        {"google_email": "c@d.com", "is_default": False, "scopes": " ".join(G.SCOPES)}])
    assert [i["id"] for i in identities.list_identities("s", "google")] == ["a@b.com", "c@d.com"]
    assert [i["id"] for i in identities.list_identities("s", "drive")] == ["c@d.com"]
    assert [i["id"] for i in identities.list_identities("s", "gmail")] == ["a@b.com", "c@d.com"]


def test_le_compte_se_nomme_par_userinfo_des_que_lidentite_est_la(monkeypatch):
    """Un consentement Drive seul n'a pas de profil Gmail à lire : l'adresse vient de
    `userinfo`. Un jeton d'avant (Gmail seul) garde l'ancien chemin."""
    import requests
    vus = []

    class _R:
        status_code = 200

        def __init__(self, body):
            self._b = body

        def json(self):
            return self._b

        def raise_for_status(self):
            pass
    monkeypatch.setattr(requests, "get", lambda url, **k: vus.append(url) or _R(
        {"email": "u@i.com", "emailAddress": "g@mail.com"}))
    assert G._fetch_email("tok", [EMAIL, "https://www.googleapis.com/auth/drive"]) == "u@i.com"
    assert "userinfo" in vus[-1]
    assert G._fetch_email("tok", ["https://www.googleapis.com/auth/gmail.modify"]) == "g@mail.com"
    assert "gmail" in vus[-1]


def test_le_coffre_ne_garde_que_les_scopes_connus(monkeypatch):
    vu = {}
    monkeypatch.setattr(credentials_store, "get_editor_app", lambda c, k: None)
    monkeypatch.setattr(G, "_fetch_email", lambda tok, scopes=(): "u@i.com")
    monkeypatch.setattr(G.db, "set_google_oauth", lambda *a, **k: vu.update(k))
    G.persist_token("nu-sub", 7, {"refresh_token": "rt", "access_token": "at",
                                  "expires_in": 3600,
                                  "scope": "openid " + EMAIL + " https://www.googleapis.com/auth/drive "
                                           "https://www.googleapis.com/auth/foreign.product"})
    assert set(vu["scopes"].split()) == {"openid", EMAIL, "https://www.googleapis.com/auth/drive"}
