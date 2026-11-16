"""n8n — workflow automation (workflows + executions).

Wraps `oto.tools.n8n.N8nClient`. Reads and switches are one tool each
(`n8n_list_workflows`, `n8n_activate_workflow`…, before the `<ns>`/`<ns>_act` rule);
what writes a workflow is `n8n_act` (create, update, delete, tags). 2-field credential (API key + instance base URL,
self-hosting/n8n Cloud requires its own URL) → generic multi-field model
(ADR 0011), resolved per call via `access.resolve_credential_fields("n8n")`.
byo_user (no platform quota: the credential IS the grant).
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, egress
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError
from .lecture import ACTE

_ACT_OPS = ("create_workflow", "update_workflow", "delete_workflow", "set_workflow_tags")
#: What `n8n_act` returns of a workflow it wrote: the workflow itself carries its whole
#: graph (`nodes`, `connections`, pinned data), which the caller just sent.
_SUMMARY = ("id", "name", "active", "activeVersionId", "versionId", "isArchived",
            "createdAt", "updatedAt")


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _need(value, name: str, op: str):
    if value is None or value == "":
        raise _bad(f"op='{op}' requires `{name}`. Nothing was done.")
    return value


def _refuse_ignored(op: str, **provided) -> None:
    """An argument supplied that THIS op does not use is an intent error."""
    for name, value in provided.items():
        if value is not None:
            raise _bad(f"op='{op}' does not use `{name}`. Nothing was done.")


def _summary(workflow: Any, ignored: list[str]) -> Any:
    if not isinstance(workflow, dict):
        return workflow
    out = {k: workflow[k] for k in _SUMMARY if k in workflow}
    if isinstance(workflow.get("nodes"), list):
        out["node_count"] = len(workflow["nodes"])
    if ignored:
        out["ignored_fields"] = ignored
    return out


def _verify(fields: dict, config: dict | None = None) -> None:
    """"Test the connection" probe — otomata-tech/oto#69. Covers `auth` ALONE.

    `GET /workflows` (already in the client — `list_workflows`), `limit=1` — the
    smallest format available, n8n exposing neither `/me` nor a balance.

    ⚠️ `N8nClient._request` raises a BARE `Exception` on an HTTP refusal (no typed
    `status_code`) — like ashby, but WITHOUT its wall: no trace of a
    per-resource scoped key model at n8n (an n8n API key is tied to the
    user account that created it, not restricted per endpoint). The refusal
    therefore falls into `unknown` (never `unauthorized`) for lack of a typed code — honest,
    not a surrender: reopenable if the client starts typing its errors.

    **Authenticated ≠ usable** (oto#69 class): does not distinguish scopes —
    an n8n key carries the account's entire perimeter.
    """
    from oto.tools.n8n import N8nClient

    egress.check_url(fields["base_url"], connector="n8n")
    N8nClient(
        api_key=fields["api_key"], base_url=fields["base_url"],
    ).list_workflows(limit=1)


def register(mcp: FastMCP) -> None:
    from oto.tools.n8n import N8nClient

    connector_verify.register("n8n", _verify)

    def _client() -> N8nClient:
        creds = access.resolve_credential_fields("n8n")
        # n8n is self-hosted: an instance on the platform's internal
        # network is exactly the case the guard must refuse without a
        # declared exception (`oto_mcp/egress.py`).
        egress.check_url(creds.get("base_url") or "", connector="n8n")
        return N8nClient(api_key=creds.get("api_key"),
                         base_url=creds.get("base_url"))

    @mcp.tool()
    def n8n_list_workflows(
        limit: int = 50,
        active: Optional[bool] = None,
        tags: Optional[str] = None,
        cursor: Optional[str] = None,
    ) -> dict:
        """List workflows (paginated via nextCursor).

        Args:
            active: keep only active/inactive workflows.
            tags: comma-separated tag names to filter by.
            cursor: pagination cursor (nextCursor from previous page).
        """
        return _client().list_workflows(
            limit=limit, active=active, tags=tags, cursor=cursor)

    @mcp.tool()
    def n8n_get_workflow(workflow_id: str) -> dict:
        """Get one workflow (nodes, connections, settings)."""
        return _client().get_workflow(workflow_id)

    @mcp.tool()
    def n8n_activate_workflow(workflow_id: str) -> dict:
        """Activate a workflow (its triggers/cron start running)."""
        return _client().activate_workflow(workflow_id)

    @mcp.tool()
    def n8n_deactivate_workflow(workflow_id: str) -> dict:
        """Deactivate a workflow."""
        return _client().deactivate_workflow(workflow_id)

    @mcp.tool()
    def n8n_list_executions(
        limit: int = 50,
        workflow_id: Optional[str] = None,
        status: Optional[str] = None,
        cursor: Optional[str] = None,
    ) -> dict:
        """List workflow executions (paginated).

        Args:
            workflow_id: filter by workflow.
            status: "success" | "error" | "waiting".
            cursor: pagination cursor.
        """
        return _client().list_executions(
            limit=limit, workflow_id=workflow_id, status=status, cursor=cursor)

    @mcp.tool()
    def n8n_get_execution(
        execution_id: int, include_data: bool = False,
    ) -> dict:
        """Get one execution. `include_data` includes per-node run data (large)."""
        return _client().get_execution(execution_id, include_data=include_data)

    @mcp.tool()
    def n8n_list_tags(limit: int = 50, cursor: Optional[str] = None) -> dict:
        """List workflow tags."""
        return _client().list_tags(limit=limit, cursor=cursor)

    @mcp.tool(name="n8n_act", annotations=ACTE)
    def n8n_act(
        op: Literal["create_workflow", "update_workflow", "delete_workflow",
                    "set_workflow_tags"],
        workflow_id: Optional[str] = None,
        workflow: Optional[dict[str, Any]] = None,
        tag_ids: Optional[list[str]] = None,
        publish_if_active: Optional[bool] = None,
    ) -> Any:
        """Write a workflow of your n8n instance.

        `op`:
        - `create_workflow` — `workflow` {name, nodes, connections, settings}; it is
          created inactive (`n8n_activate_workflow` starts it).
        - `update_workflow` — `workflow_id` + `workflow`: the keys given replace the
          current ones, and `name`, `nodes`, `connections` or `settings` left out keep
          their value. To change a node: `n8n_get_workflow`, edit, send it back. A
          published workflow goes live with the change unless `publish_if_active=false`.
        - `delete_workflow` — `workflow_id`, deleted for good.
        - `set_workflow_tags` — `workflow_id` + `tag_ids` (`n8n_list_tags`) replace its
          tags; `[]` removes them.

        Keys n8n refuses in a body (read-only ones: `id`, `active`, `tags`, `createdAt`…;
        a null where n8n takes no null) are not sent and named in `ignored_fields`.
        Returns the workflow's id, name, state and `node_count`.

        Args:
            op: What to do (see above).
            workflow_id: The workflow to change or delete.
            workflow: The workflow, as `n8n_get_workflow` returns it, or only the keys to change.
            tag_ids: `set_workflow_tags` only: the ids of the tags it keeps.
            publish_if_active: `update_workflow` only: false saves the change as a draft of a published workflow.
        """
        from oto.tools.n8n import workflow_body

        if op not in _ACT_OPS:
            raise _bad(f"Unknown op {op!r}: expected one of {', '.join(_ACT_OPS)}. "
                       "Nothing was done.")
        if op == "create_workflow":
            _refuse_ignored(op, workflow_id=workflow_id, tag_ids=tag_ids,
                            publish_if_active=publish_if_active)
            wf = _need(workflow, "workflow", op)
            body, ignored = workflow_body(wf, create=True)
            missing = [k for k in ("name", "nodes", "connections") if k not in body]
            if missing:
                raise _bad(f"op='create_workflow': `workflow` needs "
                           f"{', '.join(missing)}. Nothing was done.")
            return _summary(_client().create_workflow(wf), ignored)
        wid = _need(workflow_id, "workflow_id", op)
        if op == "update_workflow":
            _refuse_ignored(op, tag_ids=tag_ids)
            wf = _need(workflow, "workflow", op)
            body, ignored = workflow_body(wf)
            if not body:
                raise _bad("op='update_workflow': `workflow` has no key n8n accepts "
                           f"(ignored: {', '.join(ignored)}). Nothing was done.")
            return _summary(_client().update_workflow(
                wid, wf, publish_if_active=publish_if_active), ignored)
        if op == "delete_workflow":
            _refuse_ignored(op, workflow=workflow, tag_ids=tag_ids,
                            publish_if_active=publish_if_active)
            return _summary(_client().delete_workflow(wid), [])
        _refuse_ignored(op, workflow=workflow, publish_if_active=publish_if_active)
        if tag_ids is None:
            raise _bad("op='set_workflow_tags' requires `tag_ids` ([] removes every "
                       "tag). Nothing was done.")
        return _client().set_workflow_tags(wid, list(tag_ids))
