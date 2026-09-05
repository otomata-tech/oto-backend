"""Les vrais verbes de procédure : une autz, I/O hors boucle, manifeste dans la boucle."""
from __future__ import annotations

import asyncio
import json
import threading
from contextvars import ContextVar
from dataclasses import replace

import pytest
from fastmcp import FastMCP
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse

from oto_mcp.capabilities import _authz, _mcp_adapter, _rest_adapter, admin_console, registry
from oto_mcp.capabilities._execution import execute
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx
from oto_mcp.capabilities.orgs import instructions as domain
from oto_mcp.mcp_errors import McpError
from oto_mcp import session_org


TRACE = ContextVar("instruction_execution_trace", default=None)
ORG = 7
SUB = "member-test"


def _wire(monkeypatch, *, allowed=True):
    events = []
    main_thread = threading.current_thread()

    def observe(name):
        assert threading.current_thread() is not main_thread, name
        assert TRACE.get() == "request-1", name
        assert session_org.current_call_org() == ORG
        assert session_org.current_call_group() == 12
        events.append(name)

    def guard(sub, org):
        observe("authz")
        assert (sub, org) == (SUB, ORG)
        return allowed

    monkeypatch.setattr(_authz.roles, "is_org_member", guard)
    monkeypatch.setattr(_authz.roles, "is_org_admin", guard)
    monkeypatch.setattr(_authz.access, "get_user_role", lambda sub: "member")
    monkeypatch.setattr(_authz.access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(_mcp_adapter, "current_user_sub_from_token", lambda: SUB)
    monkeypatch.setattr(domain.access, "current_project", lambda: None)
    monkeypatch.setattr(domain.access, "current_group", lambda sub: None)
    row = {"slug": "sample", "title": "Title", "description": "Description",
           "body_md": "A procedure", "version": 2, "slots": []}

    def read(*args, **kwargs):
        observe("read")
        return dict(row)

    def listing(*args, **kwargs):
        observe("list")
        return [dict(row)]

    def write(*args, **kwargs):
        observe("write")
        return 3

    def delete(*args, **kwargs):
        observe("delete")
        return True

    def org(*args, **kwargs):
        observe("org")
        return {"id": ORG, "name": "Acme"}

    async def manifest(*args, **kwargs):
        assert threading.current_thread() is main_thread
        assert TRACE.get() == "request-1"
        events.append("manifest")
        return []

    monkeypatch.setattr(domain.org_store, "get_instruction", read)
    monkeypatch.setattr(domain.org_store, "list_instructions", listing)
    monkeypatch.setattr(domain.org_store, "set_instruction", write)
    monkeypatch.setattr(domain.org_store, "delete_instruction", delete)
    monkeypatch.setattr(domain.org_store, "get_org", org)
    monkeypatch.setattr(domain.tool_registry, "manifest_for", manifest)
    return events


async def _rest(operation, args):
    async def authenticate(request, verifier):
        return SUB, None

    def response(request, payload, status=200):
        return JSONResponse(payload, status_code=status)

    def error(request, status, code, detail=None, **kwargs):
        return JSONResponse({"error": code, "detail": detail}, status_code=status)

    binding = operation.rest_bindings()[0]
    routes = _rest_adapter.make_routes(None, authenticate, response, error,
                                      lambda request: None, [operation])
    route = next(r for r in routes if binding.verb in r.methods)
    path = {"id": ORG}
    if "{slug}" in binding.path:
        path["slug"] = args.get("slug", "sample")
    body = {k: v for k, v in args.items() if k not in {"op", "org_id", "slug"}}

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode()}

    request = Request({"type": "http", "method": binding.verb, "path": route.path,
                       "path_params": path, "query_string": b"", "headers": []}, receive)
    result = await route.endpoint(request)
    return result.status_code, json.loads(result.body)


async def _mcp(args):
    mcp = FastMCP("instruction-test")
    cap = registry.by_key("admin.guide")
    _mcp_adapter.register(mcp, [cap])
    tool = next(t for t in await mcp.list_tools() if t.name == "oto_admin_guide")
    return await tool.fn(**args)


@pytest.mark.parametrize("surface", ["mcp", "rest"])
@pytest.mark.parametrize("op, expected", [("get", "read"), ("list", "list"),
                                          ("set", "write"), ("delete", "delete")])
def test_real_operation_authorizes_once_and_keeps_io_outside_loop(monkeypatch, surface, op, expected):
    events = _wire(monkeypatch)
    args = {"op": op, "org_id": ORG}
    if op != "list":
        args["slug"] = "sample"
    if op == "set":
        args["body_md"] = "A procedure"

    async def call():
        token = TRACE.set("request-1")
        org_token = session_org.set_call_org(ORG)
        group_token = session_org.set_call_group(12)
        try:
            if surface == "mcp":
                return await _mcp(args)
            status, result = await _rest(admin_console._GUIDE_OPERATIONS[op], args)
            assert status == 200
            return result
        finally:
            session_org.reset_call_group(group_token)
            session_org.reset_call_org(org_token)
            TRACE.reset(token)

    result = asyncio.run(call())
    assert result
    assert events.count("authz") == 1
    assert expected in events
    assert events.count("manifest") == (1 if op in {"get", "set"} else 0)
    assert TRACE.get() is None


@pytest.mark.parametrize("surface", ["mcp", "rest"])
@pytest.mark.parametrize("op", ["get", "list", "set", "delete"])
def test_denied_operation_never_reaches_store(monkeypatch, surface, op):
    events = _wire(monkeypatch, allowed=False)
    args = {"op": op, "org_id": ORG, "slug": "sample", "body_md": "Body"}

    async def call():
        token = TRACE.set("request-1")
        org_token = session_org.set_call_org(ORG)
        group_token = session_org.set_call_group(12)
        try:
            if surface == "mcp":
                with pytest.raises(McpError):
                    await _mcp(args)
            else:
                status, body = await _rest(admin_console._GUIDE_OPERATIONS[op], args)
                assert status == 403 and body["error"] == "forbidden"
        finally:
            session_org.reset_call_group(group_token)
            session_org.reset_call_org(org_token)
            TRACE.reset(token)

    asyncio.run(call())
    assert events == ["authz"]


def test_console_reuses_canonical_input_validation(monkeypatch):
    original = admin_console._GUIDE_OPERATIONS["list"]

    class InputWithRequiredField(original.Input):
        required_by_operation: int

    called = []
    operation = replace(original, Input=InputWithRequiredField,
                        handler=lambda *args: called.append(True))
    monkeypatch.setitem(admin_console._GUIDE_OPERATIONS, "list", operation)
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        asyncio.run(admin_console._guide(ResolvedCtx(sub=SUB, org_id=ORG),
                                         admin_console.GuideAdminInput(op="list", org_id=ORG)))
    assert called == []


def test_registry_reference_cannot_disappear_or_be_ambiguous(monkeypatch):
    with pytest.raises(ValueError):
        registry.by_key("does.not.exist")
    monkeypatch.setattr(registry, "CAPABILITIES", [registry.CAPABILITIES[0]] * 2)
    with pytest.raises(ValueError):
        registry.by_key(registry.CAPABILITIES[0].key)


def test_sync_dispatch_returning_coroutine_runs_each_part_in_its_thread():
    class Empty(BaseModel):
        pass

    thread = threading.current_thread()
    seen = []

    async def continuation():
        assert threading.current_thread() is thread
        seen.append("async")
        return {"ok": True}

    def dispatch(ctx, inp):
        assert threading.current_thread() is not thread
        seen.append("sync")
        return continuation()

    ctx, result = asyncio.run(execute(dispatch, lambda: (ResolvedCtx(sub=SUB), Empty())))
    assert result == {"ok": True} and ctx.sub == SUB
    assert seen == ["sync", "async"]
