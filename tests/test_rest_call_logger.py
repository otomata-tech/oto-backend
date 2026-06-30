"""Monitoring REST (ADR 0017, kind='rest') : le middleware journalise chaque
requête /api/* dans le flux unifié, sans toucher /mcp ni le service."""
import asyncio

import pytest

from oto_mcp import api_routes as ar


def test_normalize_route_collapses_ids():
    assert ar._normalize_route("/api/orgs/7/audit-log") == "/api/orgs/:id/audit-log"
    assert ar._normalize_route("/api/me") == "/api/me"
    uuid = "/api/x/3f2504e0-4f89-41d3-9a0c-0305e82c3301/y"
    assert ar._normalize_route(uuid) == "/api/x/:id/y"


def test_claimed_sub_parses_jwt_payload_unverified():
    import base64, json
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "u-42"}).encode()).rstrip(b"=").decode()
    req = _req(headers={"authorization": f"Bearer h.{payload}.sig"})
    assert ar._claimed_sub(req) == "u-42"


def test_claimed_sub_none_for_opaque_token():
    req = _req(headers={"authorization": "Bearer oto_opaquetoken"})
    assert ar._claimed_sub(req) is None
    assert ar._claimed_sub(_req(headers={})) is None


def _req(headers: dict):
    from starlette.requests import Request
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw, "query_string": b""})


def _run_mw(monkeypatch, *, path, method="GET", status=200):
    """Exécute le middleware sur une requête simulée ; renvoie la ligne loggée (ou None)."""
    captured = {}

    def fake_insert(row):
        captured.update(row)

    monkeypatch.setattr(ar.db, "insert_tool_call", fake_insert)

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = ar.RestCallLogger(downstream)
    scope = {"type": "http", "path": path, "method": method, "headers": [], "query_string": b""}
    sent = []

    async def send(m):
        sent.append(m)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def drive():
        await mw(scope, receive, send)
        # laisse la tâche fire-and-forget s'exécuter
        await asyncio.sleep(0)
        await asyncio.gather(*list(ar._REST_LOG_TASKS), return_exceptions=True)

    asyncio.run(drive())
    return captured, sent


def test_logs_api_request_as_rest_event(monkeypatch):
    row, sent = _run_mw(monkeypatch, path="/api/orgs/7/members", method="POST", status=201)
    assert row["kind"] == "rest"
    assert row["tool"] == "POST /api/orgs/:id/members"
    assert row["ok"] is True and row["error"] is None
    assert "duration_ms" in row
    # la réponse downstream est bien passée (service intact)
    assert sent[0]["status"] == 201


def test_logs_error_status(monkeypatch):
    row, _ = _run_mw(monkeypatch, path="/api/me", status=403)
    assert row["ok"] is False and row["error"] == "HTTP 403"


def test_passthrough_non_api_does_not_log(monkeypatch):
    row, sent = _run_mw(monkeypatch, path="/mcp", status=200)
    assert row == {}  # jamais journalisé → /mcp intact
    assert sent[0]["status"] == 200


def test_options_preflight_skipped(monkeypatch):
    row, _ = _run_mw(monkeypatch, path="/api/me", method="OPTIONS", status=204)
    assert row == {}
