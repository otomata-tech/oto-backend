"""Hunter.io — emails par domaine + email finder + verifier. Nécessite HUNTER_API_KEY."""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    from oto.tools.hunter.client import HunterClient

    client = HunterClient()

    @mcp.tool()
    async def hunter_domain_search(domain: str, limit: int = 10) -> dict:
        """List public emails found on a company domain (Hunter domain-search).

        Useful to discover existing email patterns and contacts.
        Coût : 1 crédit Hunter par tranche de 10 emails.
        """
        return client.domain_search(domain=domain, limit=limit)

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
        return client.email_finder(
            domain=domain, first_name=first_name, last_name=last_name, full_name=full_name,
        )

    @mcp.tool()
    async def hunter_email_verify(email: str) -> dict:
        """Verify a single email's deliverability (Hunter email-verifier).

        Coût : 1 crédit Hunter par appel.
        """
        return client.email_verifier(email=email)
