"""Make (ex-Integromat) — automatisation de workflows (scénarios + exécutions).

Wrappe `oto.tools.make.MakeClient`. Credential à 2 champs (API token + base URL
de la zone, Make est régionalisé : eu1/us1/eu2…) → modèle générique multi-champs
(ADR 0011), résolu par appel via `access.resolve_credential_fields("make")`.
byo_user (pas de quota plateforme : le credential EST le grant).

Vocabulaire Make : un workflow = un **scénario** ; il appartient à une **équipe**
(team), elle-même dans une **organisation**. Lister les scénarios exige un team_id.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.make import MakeClient

    def _client() -> MakeClient:
        creds = access.resolve_credential_fields("make")
        return MakeClient(api_token=creds.get("api_token"),
                          base_url=creds.get("base_url"))

    @mcp.tool()
    async def make_list_organizations() -> dict:
        """List organizations reachable with this token (to discover ids)."""
        return _client().list_organizations()

    @mcp.tool()
    async def make_list_teams(organization_id: int) -> dict:
        """List teams of an organization (teams own the scenarios)."""
        return _client().list_teams(organization_id)

    @mcp.tool()
    async def make_list_scenarios(
        team_id: int, limit: int = 50, offset: int = 0,
    ) -> dict:
        """List a team's scenarios (paginated).

        Args:
            team_id: team id (see make_list_teams).
        """
        return _client().list_scenarios(team_id, limit=limit, offset=offset)

    @mcp.tool()
    async def make_get_scenario(scenario_id: int) -> dict:
        """Get a scenario (metadata, scheduling, state)."""
        return _client().get_scenario(scenario_id)

    @mcp.tool()
    async def make_get_scenario_blueprint(scenario_id: int) -> dict:
        """Get a scenario's blueprint (its modules structure)."""
        return _client().get_scenario_blueprint(scenario_id)

    @mcp.tool()
    async def make_run_scenario(
        scenario_id: int,
        data: Optional[dict] = None,
        responsive: bool = True,
    ) -> dict:
        """Trigger a scenario run.

        Args:
            data: input payload passed to the scenario (depends on its modules).
            responsive: wait for the run to finish (True) or return immediately.
        """
        return _client().run_scenario(scenario_id, data=data, responsive=responsive)

    @mcp.tool()
    async def make_list_scenario_logs(
        scenario_id: int, limit: int = 50, offset: int = 0,
    ) -> dict:
        """List a scenario's execution logs (paginated)."""
        return _client().list_scenario_logs(scenario_id, limit=limit, offset=offset)
