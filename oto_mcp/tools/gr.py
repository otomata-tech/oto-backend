"""Greece company data — identity via public registers (open data, no key).

MCP envelope of `oto.tools.gr.GrClient` (oto-core): GEMI search through the publicity
portal's autocomplete, VIES enrichment for a single result. The logic (browser
User-Agent the registry demands, throttling backoff, normalisation, VIES failure named
in `vat_check_error`) lives in the lib; here only the MCP tool and the translation of a
local refusal (`ValueError`) into `INVALID_PARAMS`. An upstream refusal
(`UpstreamHTTPError`) is left to the error envelope, like every lib-backed connector.

Open-data connector: no credential. Exposed only if activated in the DB (activation
gate, ADR 0010) — register_all gates on `connector_activation`.
"""
from __future__ import annotations

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS
from oto.tools.gr import GrClient

from ..mcp_errors import McpError


def _client() -> GrClient:
    return GrClient()


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def gr_lookup(query: str) -> dict:
        """Look up a Greek entity in open data (GEMI registry + VIES).

        `query` accepts a **company name**, a **GEMI no.** or a Greek **VAT no.**
        (ΑΦΜ, with or without the `EL` prefix). Returns the matching companies
        (name, GEMI no., VAT, active/inactive status). For a single result, adds
        the **address** and the VAT validity via VIES; a VIES outage is named in
        `vat_check_error`, never read as an invalid number.
        """
        try:
            return _client().lookup(query)
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e))) from e
