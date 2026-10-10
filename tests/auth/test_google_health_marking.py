"""Google entre dans l'aide partagée « marquer plutôt que purger » (oto#25 lot
b2) — un lot séparé, après que le WIP concurrent sur `auth/google.py` a été
poussé et tagué (#877). Même garde de portée qu'atlassian/folk/salesforce/zoho,
mais un chemin différent : google choisit sa ligne par la résolution commune
(`access.resolve_credential`, oto-backend#1160) puis renouvelle sur CETTE ligne
(`credentials_store.update_meta`, scope MEMBRE `(org, sub)`, account=email) — donc
un fichier à part plutôt
qu'un cas de plus dans `test_oauth_dead_grant_marks_rejected.py` (taillé pour
la forme legacy `("user", sub)` d'atlassian/folk).

Contrairement à atlassian/folk, `credentials_for` ne RETOURNE PAS `None` sur un
grant mort — elle relève TOUJOURS (une exception, jamais un `None` muet) : le
seul changement de ce lot est l'appel de marquage AVANT la relève, jamais un
changement de contrat pour les appelants.
"""
from __future__ import annotations

import os

import pytest

from _coffre_google import installer  # noqa: E402
from oto_mcp import credentials_store  # noqa: E402
from oto_mcp.auth import google as google_oauth  # noqa: E402


# ⚠️ Plus d'écriture d'environnement à l'IMPORT (#1111) : un `os.environ.setdefault` ici
# valait pour tout le processus dès la collecte, et des bancs d'autres fichiers ne
# passaient que grâce à lui. Une fixture se déclare, se voit et s'annule.
@pytest.fixture(autouse=True)
def _env_google(monkeypatch):
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "cid-test")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "secret-test")


class _Resp:
    def __init__(self, status: int, text: str):
        self.status_code, self.text = status, text

    def json(self) -> dict:
        return {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _RespOK:
    def __init__(self, body: dict):
        self.status_code, self._body = 200, body

    @property
    def text(self) -> str:
        import json
        return json.dumps(self._body)

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        pass


@pytest.fixture()
def wiring(monkeypatch):
    env = installer(monkeypatch, org=7, sub="sub-1")
    env.coffre.poser("a@b.com", "REFRESH-1", defaut=True, scopes="s1 s2",
                     access_token=None, expires_at=None)
    row = env.coffre.meta("a@b.com")
    calls = {"update": [], "mark": [], "record": []}
    monkeypatch.setattr(credentials_store, "update_meta",
                        lambda et, eid, prov, email, patch, conn=None: calls["update"].append(
                            (et, eid, prov, email, patch["access_token"])))
    from oto_mcp.connectors import health as connector_health
    monkeypatch.setattr(connector_health, "mark_rejected",
                        lambda et, eid, prov, acct, err: calls["mark"].append(
                            (et, eid, prov, acct, err)))
    monkeypatch.setattr(connector_health, "record_health",
                        lambda prov, scope, ok, err: calls["record"].append(
                            (prov, scope, ok, err)))
    return row, calls


def test_un_grant_mort_marque_puis_releve(monkeypatch, wiring):
    """Le coeur du lot : `invalid_grant` → `mark_rejected` AVANT la relève —
    jamais un `None` muet (contrat inchangé de `credentials_for`)."""
    row, calls = wiring
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(
        400, '{"error":"invalid_grant","error_description":"token revoked"}'))

    with pytest.raises(google_oauth.GoogleReauthRequired):
        google_oauth.credentials_for("sub-1", account="a@b.com")

    assert len(calls["mark"]) == 1
    et, eid, prov, acct, err = calls["mark"][0]
    assert et == credentials_store.MEMBER
    assert eid == credentials_store.member_id(7, "sub-1")
    assert prov == "google" and acct == "a@b.com"
    assert "invalid_grant" in (err or "")
    assert calls["update"] == [], "un grant mort ne doit jamais persister de nouveau token"


def test_le_grant_mort_rend_un_refus_qui_nomme_le_compte(monkeypatch, wiring):
    """#875/#876 — la relève est une `RuntimeError` (la seule famille que les outils
    Google traduisent en refus lisible) et son message dit QUEL compte reconnecter, et
    où. Avant : `Exception` nue, donc « Erreur interne du serveur » côté agent."""
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(
        400, '{"error":"invalid_grant"}'))

    with pytest.raises(RuntimeError) as e:
        google_oauth.credentials_for("sub-1", account="a@b.com")

    assert isinstance(e.value, google_oauth.GoogleReauthRequired)
    assert "a@b.com" in str(e.value) and "reconnect" in str(e.value)
    assert "manage.oto.cx" in str(e.value)


@pytest.mark.parametrize("module", ["calendar", "chat", "drive", "gmail", "sheets",
                                    "tasks"])
def test_les_six_outils_google_rendent_le_refus_pas_une_erreur_interne(
        monkeypatch, module):
    """Le chemin servi : chaque `_client_for_user` doit rendre le message de réauth
    en refus d'appel, jamais laisser filer l'exception jusqu'au filet générique."""
    import importlib

    from oto_mcp.mcp_errors import McpError
    mod = importlib.import_module(f"oto_mcp.tools.{module}")
    monkeypatch.setattr(mod.access, "current_user_sub_or_raise", lambda: "sub-1")

    def _mort(sub, account=None, service=None):
        raise google_oauth.GoogleReauthRequired("jeton de a@b.com mort — reconnecte-le")
    monkeypatch.setattr(mod.google_oauth, "credentials_for", _mort)

    with pytest.raises(McpError) as e:
        mod._client_for_user("a@b.com")
    assert "reconnecte" in str(e.value.error.message)


def test_un_refresh_reussi_demarque(monkeypatch, wiring):
    """Le renouvellement MERGE le meta (contrairement à atlassian/folk
    qui le REMPLACENT) : sans cet appel explicite, un `health_ko` posé plus tôt
    survivrait à un refresh qui a pourtant réussi."""
    row, calls = wiring
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _RespOK(
        {"access_token": "AT-NEW", "expires_in": 3600}))

    creds = google_oauth.credentials_for("sub-1", account="a@b.com")

    assert creds.token == "AT-NEW"
    assert calls["mark"] == []
    assert len(calls["record"]) == 1
    prov, scope, ok, err = calls["record"][0]
    assert prov == "google" and ok is True and err is None
    assert scope == (credentials_store.MEMBER, credentials_store.member_id(7, "sub-1"), "a@b.com")
    assert calls["update"] == [(credentials_store.MEMBER, credentials_store.member_id(7, "sub-1"),
                                "google", "a@b.com", "AT-NEW")]


def test_une_erreur_de_config_ne_marque_rien(monkeypatch, wiring):
    """Contre-épreuve (même garde qu'atlassian/folk/zoho) : `invalid_client` n'est
    PAS un grant mort — ni marquage, ni démarquage, l'erreur remonte telle quelle."""
    row, calls = wiring
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(400, '{"error":"invalid_client"}'))

    with pytest.raises(RuntimeError):
        google_oauth.credentials_for("sub-1", account="a@b.com")

    assert calls["mark"] == [] and calls["record"] == [] and calls["update"] == []
