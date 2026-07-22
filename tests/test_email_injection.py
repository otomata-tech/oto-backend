"""Anti-injection d'en-tête email : `_send` neutralise CR/LF sur les champs
d'en-tête (sujet/to/from/reply_to) — un nom de projet user-controlled ne peut
pas injecter un en-tête (Bcc, etc.). Revue sécu automatique, oto_mcp/email.py."""
import sys
import types

import pytest

from oto_mcp import email as E


def test_send_strips_crlf_from_headers(monkeypatch):
    monkeypatch.setenv("OTO_MAILER_SEND_BEARER", "tok")
    captured = {}

    class _Resp:
        status_code = 200
        text = ""

    fake_httpx = types.SimpleNamespace(
        post=lambda url, headers=None, json=None, timeout=None: captured.update(json=json) or _Resp())
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

    ok = E._send(
        to="a@x.fr\r\nBcc: victim@evil.com",
        subject="proposition — Projet\r\nBcc: victim@evil.com",
        html="<p>ok</p>",
        reply_to="r@x.fr\nX-Injected: 1",
    )
    assert ok is True
    p = captured["json"]
    for field in ("to", "subject", "from", "reply_to"):
        assert "\r" not in (p.get(field) or "") and "\n" not in (p.get(field) or ""), field
    assert p["subject"] == "proposition — Projet Bcc: victim@evil.com"  # CR retiré, LF→espace


def test_send_no_bearer_is_noop(monkeypatch):
    monkeypatch.delenv("OTO_MAILER_SEND_BEARER", raising=False)
    assert E._send("a@x.fr", "s", "<p>x</p>") is False
