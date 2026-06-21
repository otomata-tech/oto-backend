"""n8n — automatisation de workflows (workflows + exécutions).

Wrappe `oto.tools.n8n.N8nClient`. Credential à 2 champs (API key + base URL de
l'instance, le self-hosting/n8n Cloud impose une URL propre) → modèle générique
multi-champs (ADR 0011), résolu par appel via `access.resolve_credential_fields("n8n")`.
byo_user (pas de quota plateforme : le credential EST le grant).
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.n8n import N8nClient

    def _client() -> N8nClient:
        creds = access.resolve_credential_fields("n8n")
        return N8nClient(api_key=creds.get("api_key"),
                         base_url=creds.get("base_url"))

    @mcp.tool()
    async def n8n_list_workflows(
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
    async def n8n_get_workflow(workflow_id: str) -> dict:
        """Get one workflow (nodes, connections, settings)."""
        return _client().get_workflow(workflow_id)

    @mcp.tool()
    async def n8n_activate_workflow(workflow_id: str) -> dict:
        """Activate a workflow (its triggers/cron start running)."""
        return _client().activate_workflow(workflow_id)

    @mcp.tool()
    async def n8n_deactivate_workflow(workflow_id: str) -> dict:
        """Deactivate a workflow."""
        return _client().deactivate_workflow(workflow_id)

    @mcp.tool()
    async def n8n_list_executions(
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
    async def n8n_get_execution(
        execution_id: int, include_data: bool = False,
    ) -> dict:
        """Get one execution. `include_data` includes per-node run data (large)."""
        return _client().get_execution(execution_id, include_data=include_data)

    @mcp.tool()
    async def n8n_list_tags(limit: int = 50, cursor: Optional[str] = None) -> dict:
        """List workflow tags."""
        return _client().list_tags(limit=limit, cursor=cursor)
