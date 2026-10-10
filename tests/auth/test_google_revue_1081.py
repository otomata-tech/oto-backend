"""Les connecteurs Google par service (#1081) — ce que la revue a trouvé, figé.

1. **Access token borné** : `include_granted_scopes` est gardé (le découpage par service
   en a besoin), mais sous le client d'un tenant le refresh token porte aussi les droits
   obtenus par ses autres produits. Au refresh, on passe à Google les scopes ENREGISTRÉS
   pour la ligne : l'access token ne porte que les nôtres. Un bornage refusé
   (`invalid_scope`) est un refus nommé, jamais un repli sur un jeton non borné.
2. **Pas de repli silencieux** : une réponse d'échange sans champ `scope` ouvrait les six
   services ; elle est désormais refusée, et rien n'est écrit.
3. **Pièces jointes** : une source `drive`/`gmail` vérifie le service comme un appel
   d'outil — un compte qui ne l'a pas autorisé reçoit le refus nommé, pas un 403 muet.
"""
from __future__ import annotations

import os

import pytest

from oto_mcp import file_source as fs  # noqa: E402
from oto_mcp.auth import google as google_oauth  # noqa: E402


# ⚠️ Plus d'écriture d'environnement à l'IMPORT (#1111) : un `os.environ.setdefault` ici
# valait pour tout le processus dès la collecte, et des bancs d'autres fichiers ne
# passaient que grâce à lui. Une fixture se déclare, se voit et s'annule.
@pytest.fixture(autouse=True)
def _env_google(monkeypatch):
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "cid-env")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "secret-env")
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "state-secret-test")


ENREGISTRES = " ".join(google_oauth.IDENTITY_SCOPES
                       + tuple(google_oauth.SERVICE_SCOPES["sheets"]))


class _Rep:
    def __init__(self, status, text="", payload=None):
        self.status_code, self.text, self._payload = status, text, payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture()
def envoye(monkeypatch):
    """Capture le corps POSTé au refresh."""
    import requests
    vu: dict = {}

    def _post(url, data=None, timeout=None):
        vu.update(data or {})
        return _Rep(200, payload={"access_token": "at", "expires_in": 3600})

    monkeypatch.setattr(requests, "post", _post)
    monkeypatch.setattr(google_oauth, "app_for",
                        lambda sub: google_oauth.OAuthApp("cid", "sec", "https://x/cb"))
    return vu


# ─── 1. l'access token est borné aux scopes enregistrés ───────────────────────

def test_le_refresh_envoie_les_scopes_enregistres(envoye):
    google_oauth._refresh_access_token("rt", "sub", scopes=ENREGISTRES)
    assert envoye["scope"] == ENREGISTRES


def test_credentials_for_borne_le_refresh_a_la_ligne(envoye, monkeypatch):
    """Le chemin réel : `credentials_for` passe les scopes DE LA LIGNE au refresh."""
    from _coffre_google import installer

    env = installer(monkeypatch, org=7, sub="sub")
    env.coffre.poser("a@b.com", "rt", defaut=True, scopes=ENREGISTRES,
                     access_token=None, expires_at=None, client_id="cid")
    monkeypatch.setattr(google_oauth.connector_health, "record_health", lambda *a, **k: None)
    google_oauth.credentials_for("sub", account="a@b.com", service="sheets")
    assert envoye["scope"] == ENREGISTRES


def test_un_bornage_refuse_est_un_refus_nomme(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: _Rep(400, '{"error":"invalid_scope"}'))
    monkeypatch.setattr(google_oauth, "app_for",
                        lambda sub: google_oauth.OAuthApp("cid", "sec", "https://x/cb"))
    with pytest.raises(google_oauth.GoogleScopeRejected) as e:
        google_oauth._refresh_access_token("rt", "sub", scopes=ENREGISTRES)
    assert isinstance(e.value, RuntimeError)   # les outils Google la traduisent
    assert not isinstance(e.value, google_oauth.GoogleReauthRequired)


# ─── 2. pas de repli silencieux à la pose ─────────────────────────────────────

def test_une_reponse_sans_scope_est_refusee_et_rien_n_est_ecrit(monkeypatch):
    ecrit = []
    monkeypatch.setattr(google_oauth.db, "set_google_oauth",
                        lambda *a, **k: ecrit.append(k))
    with pytest.raises(google_oauth.GoogleScopeMissing):
        google_oauth.persist_token("sub", 7, {"refresh_token": "RT", "access_token": "at",
                                              "expires_in": 3600}, client_id="cid")
    assert ecrit == []


# ─── 3. une pièce jointe vérifie son service ──────────────────────────────────

@pytest.mark.parametrize("kind,service", [("drive", "drive"), ("gmail", "gmail")])
def test_une_piece_jointe_google_verifie_son_service(monkeypatch, kind, service):
    vu = {}

    def _creds(sub, account=None, service=None, source_account=False):
        vu["service"], vu["source_account"] = service, source_account
        raise RuntimeError(f"Le compte Google n'a pas encore autorisé {service}")

    monkeypatch.setattr(fs.access, "current_user_sub_or_raise", lambda: "sub")
    monkeypatch.setattr(fs.google_oauth, "credentials_for", _creds)
    lire = {"drive": lambda: fs._from_drive({"file_id": "f1"}),
            "gmail": lambda: fs._from_gmail({"message_id": "m1", "filename": "a.pdf"})}
    with pytest.raises(RuntimeError, match="pas encore autorisé"):
        lire[kind]()
    assert vu["service"] == service
    # Le compte d'une SOURCE peut différer du `_account=` de l'appel (#1160).
    assert vu["source_account"] is True
