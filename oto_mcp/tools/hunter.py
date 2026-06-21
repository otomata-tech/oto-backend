"""Hunter.io — emails par domaine + email finder + verifier.

Clé résolue par appel : user key (`/account`) prioritaire, sinon platform
key + quota daily (member). Guest doit poser sa propre clé.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.hunter.client import HunterClient

    def _client() -> tuple[HunterClient, bool]:
        key, is_platform = access.resolve_api_key("hunter")
        return HunterClient(api_key=key), is_platform

    @mcp.tool()
    async def hunter_domain_search(domain: str, limit: int = 10) -> dict:
        """List public emails found on a company domain (Hunter domain-search).

        Useful to discover existing email patterns and contacts.
        Coût : 1 crédit Hunter par tranche de 10 emails.
        """
        client, is_platform = _client()
        result = client.domain_search(domain=domain, limit=limit)
        if is_platform:
            access.record_platform_usage("hunter")
        return result

    @mcp.tool()
    async def hunter_email_finder(
        domain: str,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        full_name: Optional[str] = None,
    ) -> dict:
        """Find a specific person's email at a company (Hunter email-finder).

        Provide either (`first_name` + `last_name`) or `full_name`.
        Coût : 1 crédit Hunter par appel.
        """
        client, is_platform = _client()
        result = client.email_finder(
            domain=domain, first_name=first_name, last_name=last_name, full_name=full_name,
        )
        if is_platform:
            access.record_platform_usage("hunter")
        return result

    @mcp.tool()
    async def hunter_email_verify(email: str) -> dict:
        """Verify a single email's deliverability (Hunter email-verifier).

        Coût : 1 crédit Hunter par appel.
        """
        client, is_platform = _client()
        result = client.email_verifier(email=email)
        if is_platform:
            access.record_platform_usage("hunter")
        return result
