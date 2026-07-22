"""FullEnrich — waterfall multi-provider contact enrichment (phones + emails).

~70% phone hit rate. Async bulk API (POST → poll). Pay-per-result.

⚠️ Surface MCP async assumée (signal #252) : l'ex-tool synchrone pollait
in-process 131-147s → tout client MCP raccroche (~60s), résultat perdu ET crédits
consommés. Désormais : `fullenrich_enrich_linkedin` SOUMET le job (~1s, bulk
jusqu'à 100 contacts) et `fullenrich_result` relève le statut/le résultat —
le polling appartient à l'agent.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.fullenrich.client import FullenrichClient

    def _client() -> tuple[FullenrichClient, bool]:
        key, is_platform = access.resolve_api_key("fullenrich")
        return FullenrichClient(api_key=key), is_platform

    @mcp.tool()
    def fullenrich_enrich_linkedin(
        contacts: list[dict],
        enrich_fields: Optional[list[str]] = None,
    ) -> dict:
        """Submit an ASYNC enrichment job (phones + emails) via FullEnrich (waterfall 20+ providers).

        Returns immediately with an `enrichment_id` — the job runs server-side for
        ~30s to 4min. THEN call `fullenrich_result(enrichment_id)` to collect (first
        poll after ~30s, then every ~20-30s until status FINISHED).

        Args:
            contacts: 1-100 contacts in ONE job (batch friends — one job for a whole
                list beats parallel single calls). Each: {"first_name": str,
                "last_name": str, "linkedin_slug": str (e.g. "alexis-laporte",
                NOT a URL — optional but strongly improves matching),
                "company_name": str (optional)}.
            enrich_fields: subset of ["contact.work_emails", "contact.phones",
                "contact.personal_emails"]. Default: work_emails + phones.
                Only ask what you need — pricing is pay-per-result:
                10 credits/phone, 1/work_email, 3/personal_email.
        """
        client, is_platform = _client()
        try:
            enrichment_id = client.submit(contacts, enrich_fields=enrich_fields)
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        if is_platform:
            for _ in contacts:
                access.record_platform_usage("fullenrich")
        return {
            "enrichment_id": enrichment_id,
            "submitted": len(contacts),
            "next_step": ("Job accepted. Call fullenrich_result(enrichment_id) "
                          "in ~30s (typical completion 30s-4min)."),
        }

    @mcp.tool()
    def fullenrich_result(enrichment_id: str) -> dict:
        """Collect the result of a FullEnrich job submitted with fullenrich_enrich_linkedin.

        Single status check, returns immediately. If `done` is false, wait ~20-30s
        and call again (jobs typically finish in 30s-4min). When done, `profiles`
        holds one entry per submitted contact: {found, linkedin_slug, full_name,
        title, company_name, phones[], work_emails[], personal_emails[], location}.
        """
        client, _ = _client()
        res = client.fetch(enrichment_id)
        if res["status"] != "FINISHED":
            return {
                "done": False,
                "status": res["status"],
                "next_step": "Still running — call fullenrich_result again in ~20-30s.",
            }
        return {
            "done": True,
            "status": "FINISHED",
            "profiles": [p.to_dict() for p in res["profiles"]],
        }
