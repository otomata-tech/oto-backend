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
from ..connectors import verify as connector_verify


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion » — otomata-tech/oto#69. Couvre `auth` SEUL.

    `GET /containers` (déjà dans le client — `list_containers`), sans
    `agent_id` : liste les exécutions récentes du COMPTE entier, `limit=1` —
    le plus petit format disponible, Phantombuster n'exposant ni `/me` ni
    solde à cet appel. Une liste VIDE (aucune exécution) est un état normal.

    **Authentifié ≠ utilisable** (classe oto#69) : ne distingue pas de scope —
    une clé Phantombuster porte le périmètre entier du compte.
    """
    from oto.tools.phantombuster.client import PhantombusterClient

    PhantombusterClient(api_key=fields["key"]).list_containers(limit=1)


def register(mcp: FastMCP) -> None:
    from oto.tools.phantombuster.client import PhantombusterClient

    connector_verify.register("phantombuster", _verify)

    def _client() -> PhantombusterClient:
        key, _ = access.resolve_api_key("phantombuster")
        return PhantombusterClient(api_key=key)

    @mcp.tool()
    def phantombuster_get_agent(agent_id: str) -> dict:
        """Get an agent's configuration and status."""
        return _client().get_agent(agent_id)

    @mcp.tool()
    def phantombuster_list_containers(
        agent_id: Optional[str] = None, limit: int = 10,
    ) -> dict:
        """List recent containers (runs), optionally filtered to one agent."""
        return {"containers": _client().list_containers(agent_id=agent_id, limit=limit)}

    @mcp.tool()
    def phantombuster_get_container(container_id: str) -> dict:
        """Get a container (run) status and metadata."""
        return _client().get_container(container_id)

    @mcp.tool()
    def phantombuster_container_results(container_id: str) -> dict:
        """Get the parsed JSON results produced by a finished container."""
        return {"results": _client().get_container_results(container_id)}

    @mcp.tool()
    def phantombuster_container_output(container_id: str) -> dict:
        """Get a container's output logs (text)."""
        return {"output": _client().get_container_output(container_id)}

    @mcp.tool()
    def phantombuster_launch_agent(
        agent_id: str, config: Optional[dict] = None,
    ) -> dict:
        """Launch an agent (starts a run). Returns the new containerId.

        Args:
            config: optional overrides (argument, bonusArgument…) merged into the
                launch payload.
        """
        return _client().launch_agent(agent_id, config=config)
