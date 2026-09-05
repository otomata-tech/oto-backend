"""Google entre dans l'aide partagée « marquer plutôt que purger » (oto#25 lot
b2) — un lot séparé, après que le WIP concurrent sur `auth/google.py` a été
poussé et tagué (#877). Même garde de portée qu'atlassian/folk/salesforce/zoho,
mais un chemin différent : google passe par `db.get_google_oauth` /
`db.update_google_access_token` (scope MEMBRE `(org, sub)`, account=email),
jamais directement par `credentials_store` — donc un fichier à part plutôt
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

os.environ.setdefault("GOOGLE_WORKSPACE_CLIENT_ID", "cid-test")
os.environ.setdefault("GOOGLE_WORKSPACE_CLIENT_SECRET", "secret-test")

from oto_mcp import access, credentials_store  # noqa: E402
from oto_mcp.auth import google as google_oauth  # noqa: E402


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
    row = {"google_email": "a@b.com", "refresh_token": "REFRESH-1",
           "access_token": None, "expires_at": None, "scopes": "s1 s2"}
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    monkeypatch.setattr(google_oauth.db, "get_google_oauth", lambda sub, org, account=None: row)
    calls = {"update": [], "mark": [], "record": []}
    monkeypatch.setattr(google_oauth.db, "update_google_access_token",
                        lambda sub, org, email, token, exp: calls["update"].append(
                            (sub, org, email, token, exp)))
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


def test_un_refresh_reussi_demarque(monkeypatch, wiring):
    """`update_google_access_token` MERGE le meta (contrairement à atlassian/folk
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
    assert calls["update"] == [("sub-1", 7, "a@b.com", "AT-NEW", calls["update"][0][4])]


def test_une_erreur_de_config_ne_marque_rien(monkeypatch, wiring):
    """Contre-épreuve (même garde qu'atlassian/folk/zoho) : `invalid_client` n'est
    PAS un grant mort — ni marquage, ni démarquage, l'erreur remonte telle quelle."""
    row, calls = wiring
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(400, '{"error":"invalid_client"}'))

    with pytest.raises(RuntimeError):
        google_oauth.credentials_for("sub-1", account="a@b.com")

    assert calls["mark"] == [] and calls["record"] == [] and calls["update"] == []
