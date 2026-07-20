"""Sonde `_verify` Slack (signal #217) : un token posé peut authentifier mais
manquer les scopes de lecture → on veut un diagnostic actionnable, pas un
`missing_scope` opaque au premier appel réel. Deux étages : auth.test (token
vivant ?) puis lecture channels (scope `channels:read` ?)."""
from __future__ import annotations

import pytest
from oto.tools.slack.client import SlackError

from oto_mcp.tools.slack import _verify


class _FakeClient:
    """Client Slack stubé : `calls` pilote ce que chaque appel lève."""

    calls: dict = {}

    def __init__(self, bot_token=None, user_token=None, default_as_user=False):
        _FakeClient.calls["init"] = {"bot": bot_token, "user": user_token}

    def _request(self, method, endpoint, **kw):
        exc = _FakeClient.calls.get("auth")
        if exc:
            raise exc
        return {"ok": True}

    def list_channels(self, types="public_channel", as_user=None):
        exc = _FakeClient.calls.get("channels")
        if exc:
            raise exc
        return []


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    _FakeClient.calls = {}
    monkeypatch.setattr("oto.tools.slack.client.SlackClient", _FakeClient)


def test_no_token_raises():
    with pytest.raises(ValueError, match="aucun token Slack"):
        _verify({})


def test_dead_token_flagged_as_invalid():
    _FakeClient.calls = {"auth": SlackError("invalid_auth")}
    with pytest.raises(ValueError, match="token Slack invalide"):
        _verify({"user_token": "xoxp-dead"})


def test_valid_token_missing_scope_names_channels_read():
    # auth.test passe (token vivant) mais la lecture channels manque le scope :
    # LE cas du signal #217 → message qui nomme channels:read + la ré-install.
    _FakeClient.calls = {"channels": SlackError("missing_scope")}
    with pytest.raises(ValueError, match="channels:read"):
        _verify({"bot_token": "xoxb-ok"})


def test_all_ok_does_not_raise():
    _verify({"bot_token": "xoxb-ok", "user_token": "xoxp-ok"})
    assert _FakeClient.calls["init"] == {"bot": "xoxb-ok", "user": "xoxp-ok"}


def test_legacy_raw_bot_token_routed_by_prefix():
    # credential mono-champ legacy (token brut, pas de bot_token/user_token nommé).
    _verify({"value": "xoxb-legacy"})
    assert _FakeClient.calls["init"] == {"bot": "xoxb-legacy", "user": None}
