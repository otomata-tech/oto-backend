"""Sonde `verify` d'atlassian (oto-backend#876) — accessible SEULEMENT depuis
que le walker de cascade voit le scope legacy `("user", sub)` (cf.
`tests/test_cascade_legacy_user_rung.py`, qui verrouille CE côté-là).

Ce fichier verrouille l'autre moitié : la sonde elle-même. Un refresh_token
peut rafraîchir avec succès (`access_token_for`) pendant que le site a révoqué
l'app côté Atlassian Admin — invisible tant qu'on ne demande rien à l'API. La
sonde doit donc faire les DEUX : refresh transparent PUIS un appel réel.
"""
import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("OTO_MCP_OAUTH_STATE_SECRET", "test-secret")
os.environ.setdefault("OTO_MCP_PUBLIC_URL", "https://mcp.oto.ninja")
os.environ.setdefault("ATLASSIAN_OAUTH_CLIENT_ID", "cid-test")

from oto_mcp.auth import atlassian as atlassian_oauth  # noqa: E402


def test_verify_appelle_access_token_for_avec_le_sub_de_linstance(monkeypatch):
    """`instance` = `(entity_type, entity_id, account)` de la ligne RÉELLEMENT
    résolue par le walker (cf. `_fields_config_scope`) — `entity_id` EST le sub :
    la sonde doit lire CELUI-LÀ, jamais un sub ambiant."""
    seen = []
    monkeypatch.setattr(atlassian_oauth, "access_token_for",
                        lambda sub: seen.append(sub) or "TOK")
    resp = MagicMock(status_code=200)
    resp.raise_for_status.return_value = None
    monkeypatch.setattr("requests.get", lambda *a, **k: resp)
    atlassian_oauth._verify({}, {}, instance=("user", "sub-A", ""))
    assert seen == ["sub-A"]


def test_verify_interroge_bien_lapi_pas_seulement_le_refresh(monkeypatch):
    """Un `access_token_for` qui rend un token ne suffit PAS à dire `ok` — la
    sonde doit vraiment appeler l'API (sinon un site révoqué reste vert)."""
    monkeypatch.setattr(atlassian_oauth, "access_token_for", lambda sub: "TOK")
    called = {}

    def _get(url, headers=None, timeout=None):
        called["url"] = url
        called["auth"] = headers.get("Authorization")
        resp = MagicMock(status_code=200)
        resp.raise_for_status.return_value = None
        return resp

    monkeypatch.setattr("requests.get", _get)
    atlassian_oauth._verify({}, {}, instance=("user", "sub-A", ""))
    assert called["url"] == atlassian_oauth._ACCESSIBLE_RESOURCES_URL
    assert called["auth"] == "Bearer TOK"


def test_verify_sans_instance_leve_proprement():
    with pytest.raises(RuntimeError):
        atlassian_oauth._verify({}, {}, instance=None)


def test_verify_sans_token_leve(monkeypatch):
    """`access_token_for` rend `None` quand le grant est mort (marqué rejeté par
    ailleurs, oto#25 lot a) — la sonde ne doit jamais taper l'API sans token."""
    called = []
    monkeypatch.setattr(atlassian_oauth, "access_token_for", lambda sub: None)
    monkeypatch.setattr("requests.get", lambda *a, **k: called.append(1))
    with pytest.raises(RuntimeError):
        atlassian_oauth._verify({}, {}, instance=("user", "sub-A", ""))
    assert called == []


def test_verify_relance_lechec_http(monkeypatch):
    import requests
    monkeypatch.setattr(atlassian_oauth, "access_token_for", lambda sub: "TOK")
    resp = MagicMock(status_code=403)
    resp.raise_for_status.side_effect = requests.HTTPError("403 forbidden")
    monkeypatch.setattr("requests.get", lambda *a, **k: resp)
    with pytest.raises(requests.HTTPError):
        atlassian_oauth._verify({}, {}, instance=("user", "sub-A", ""))


def test_la_sonde_est_enregistree():
    from oto_mcp.connectors import verify as connector_verify
    assert connector_verify.supports("atlassian")
    assert connector_verify.probe_for("atlassian") is atlassian_oauth._verify
