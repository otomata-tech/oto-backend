"""Zoho Analytics — lecture des données (workspaces, vues, export, requêtes SQL).

Credential = OAuth2 (self-client) à 5 champs : client_id + client_secret +
refresh_token + org_id (en-tête `ZANALYTICS-ORGID`) + data_center → modèle
générique multi-champs (ADR 0011), résolu par appel via
`access.resolve_credential_fields("zohoanalytics")`. byo_user OU byo_org (clé
partageable équipe data). Token d'accès dérivé/caché en mémoire côté client.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access


# Zoho héberge par data center régional ; le self-client ET le refresh token sont
# liés à leur région d'émission (un self-client `.eu` tapant `accounts.zoho.com`
# est rejeté par un `invalid_client` opaque). Le champ `data_center` sélectionne
# les domaines API Analytics + OAuth. Régions reconnues :
_DC_DOMAINS = {
    "com": ("https://analyticsapi.zoho.com", "https://accounts.zoho.com"),
    "eu": ("https://analyticsapi.zoho.eu", "https://accounts.zoho.eu"),
    "in": ("https://analyticsapi.zoho.in", "https://accounts.zoho.in"),
    "au": ("https://analyticsapi.zoho.com.au", "https://accounts.zoho.com.au"),
    "jp": ("https://analyticsapi.zoho.jp", "https://accounts.zoho.jp"),
    "ca": ("https://analyticsapi.zohocloud.ca", "https://accounts.zohocloud.ca"),
    "sa": ("https://analyticsapi.zoho.sa", "https://accounts.zoho.sa"),
}


def _resolve_dc_domains(data_center: Optional[str]) -> tuple[str, str]:
    """`(api_domain, accounts_url)` pour la région Zoho déclarée. Région manquante
    ou non reconnue → `McpError` actionnable, **jamais** de repli silencieux sur
    `com` (qui masquerait la vraie cause d'un `invalid_client`)."""
    dc = (data_center or "").strip().lower()
    if dc not in _DC_DOMAINS:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=(
            (f"Data center Zoho non reconnu : {data_center!r}." if dc
             else "Data center Zoho manquant.")
            + " Renseigne ta région dans le champ « Data center » du connecteur Zoho"
            " Analytics — l'une de : com, eu, in, au, jp, ca, sa. Elle est visible dans"
            " l'URL quand tu es connecté·e à Zoho Analytics (ex. analytics.zoho.eu → « eu »)."
        )))
    return _DC_DOMAINS[dc]


def register(mcp: FastMCP) -> None:
    from oto.tools.zohoanalytics.client import ZohoAnalyticsClient

    def _client() -> ZohoAnalyticsClient:
        creds = access.resolve_credential_fields("zohoanalytics")
        api_domain, accounts_url = _resolve_dc_domains(creds.get("data_center"))
        return ZohoAnalyticsClient(
            client_id=creds.get("client_id"),
            client_secret=creds.get("client_secret"),
            refresh_token=creds.get("refresh_token"),
            org_id=creds.get("org_id"),
            api_domain=api_domain,
            accounts_url=accounts_url,
        )

    @mcp.tool()
    def zohoanalytics_workspaces() -> dict:
        """List all Zoho Analytics workspaces accessible to the user (owned + shared)."""
        return _client().list_workspaces()

    @mcp.tool()
    def zohoanalytics_views(
        workspace_id: str, view_types: Optional[list[int]] = None,
    ) -> dict:
        """List the views (tables, charts, reports…) of a workspace.

        Args:
            view_types: optional filter by Zoho view-type code —
                0 Table, 2 Chart, 3 Pivot, 4 Summary, 6 QueryTable, 7 Dashboard.
        """
        return _client().list_views(workspace_id, view_types=view_types)

    @mcp.tool()
    def zohoanalytics_view_details(workspace_id: str, view_id: str) -> dict:
        """Get the metadata of one view (columns, type, folder…)."""
        return _client().get_view_details(workspace_id, view_id)

    @mcp.tool()
    def zohoanalytics_export(
        workspace_id: str,
        view_id: str,
        response_format: str = "json",
        criteria: Optional[str] = None,
        selected_columns: Optional[list[str]] = None,
    ) -> dict:
        """Export the data of a view (synchronous).

        Args:
            response_format: json (default) | csv | xml | xls | pdf | html | image.
            criteria: Zoho row filter, e.g. '"Sales" > 500'.
            selected_columns: restrict to these column names.

        Returns the parsed JSON for json, else {"data": <raw text>}.
        """
        out = _client().export_view(
            workspace_id, view_id, response_format=response_format,
            criteria=criteria, selected_columns=selected_columns)
        return out if isinstance(out, dict) else {"data": out}

    @mcp.tool()
    def zohoanalytics_query(
        workspace_id: str, sql_query: str, response_format: str = "json",
    ) -> dict:
        """Run a SQL SELECT query over a workspace's tables (async bulk export,
        resolved server-side: create job → poll → download).

        Args:
            sql_query: a SELECT statement over the workspace tables, e.g.
                'select Region, sum("Sales") from "Sales" group by Region'.
            response_format: json (default) | csv.

        Returns the parsed JSON for json, else {"data": <raw text>}.
        """
        out = _client().query_sql(
            workspace_id, sql_query, response_format=response_format)
        return out if isinstance(out, dict) else {"data": out}
