"""Monitoring connecteur (ADR 0017, kind='connector') : un échec de résolution de
credential émet un événement de monitoring AVANT de relever — sans masquer l'erreur,
sans fausser le signal sur les sondes (emit_on_failure=False)."""
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from oto_mcp import access


@pytest.fixture
def captured(monkeypatch):
    rows = []
    monkeypatch.setattr(access.db, "insert_tool_call", lambda row: rows.append(row))
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    return rows


def _boom(*a, **k):
    raise McpError(ErrorData(code=INVALID_PARAMS, message="Aucune clé `pennylane`"))


def test_failure_emits_connector_event_then_reraises(captured, monkeypatch):
    monkeypatch.setattr(access, "_resolve_credential_impl", _boom)
    with pytest.raises(McpError):
        access.resolve_credential("pennylane", sub="u1")
    assert len(captured) == 1
    ev = captured[0]
    assert ev["kind"] == "connector" and ev["tool"] == "pennylane"
    assert ev["sub"] == "u1" and ev["org_id"] == 7 and ev["ok"] is False


def test_probe_does_not_emit(captured, monkeypatch):
    monkeypatch.setattr(access, "_resolve_credential_impl", _boom)
    with pytest.raises(McpError):
        access.resolve_credential("unipile", sub="u1", emit_on_failure=False)
    assert captured == []  # sonde → aucun faux signal


def test_success_does_not_emit(captured, monkeypatch):
    sentinel = object()
    monkeypatch.setattr(access, "_resolve_credential_impl", lambda *a, **k: sentinel)
    assert access.resolve_credential("serper", sub="u1") is sentinel
    assert captured == []  # le chemin heureux n'émet jamais


def test_emit_failure_never_masks_original_error(monkeypatch):
    monkeypatch.setattr(access, "_resolve_credential_impl", _boom)
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    def insert_boom(row):
        raise RuntimeError("db down")
    monkeypatch.setattr(access.db, "insert_tool_call", insert_boom)
    # l'émission casse → on relève quand même la McpError d'origine (pas la RuntimeError)
    with pytest.raises(McpError):
        access.resolve_credential("pennylane", sub="u1")
