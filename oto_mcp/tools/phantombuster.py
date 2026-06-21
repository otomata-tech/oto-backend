"""Phantombuster — automation agents (launch + monitor + results).

Wrappe `oto.tools.phantombuster.PhantombusterClient`. Clé résolue par appel via
`access.resolve_api_key("phantombuster")` — byo.

Note : `phantombuster_launch_agent` déclenche un run (peut consommer des crédits
Phantombuster et agir sur des comptes tiers). Les autres tools sont en lecture.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.phantombuster.client import PhantombusterClient

    def _client() -> PhantombusterClient:
        key, _ = access.resolve_api_key("phantombuster")
        return PhantombusterClient(api_key=key)

    @mcp.tool()
    async def phantombuster_get_agent(agent_id: str) -> dict:
        """Get an agent's configuration and status."""
        return _client().get_agent(agent_id)

    @mcp.tool()
    async def phantombuster_list_containers(
        agent_id: Optional[str] = None, limit: int = 10,
    ) -> dict:
        """List recent containers (runs), optionally filtered to one agent."""
        return {"containers": _client().list_containers(agent_id=agent_id, limit=limit)}

    @mcp.tool()
    async def phantombuster_get_container(container_id: str) -> dict:
        """Get a container (run) status and metadata."""
        return _client().get_container(container_id)

    @mcp.tool()
    async def phantombuster_container_results(container_id: str) -> dict:
        """Get the parsed JSON results produced by a finished container."""
        return {"results": _client().get_container_results(container_id)}

    @mcp.tool()
    async def phantombuster_container_output(container_id: str) -> dict:
        """Get a container's output logs (text)."""
        return {"output": _client().get_container_output(container_id)}

    @mcp.tool()
    async def phantombuster_launch_agent(
        agent_id: str, config: Optional[dict] = None,
    ) -> dict:
        """Launch an agent (starts a run). Returns the new containerId.

        Args:
            config: optional overrides (argument, bonusArgument…) merged into the
                launch payload.
        """
        return _client().launch_agent(agent_id, config=config)
