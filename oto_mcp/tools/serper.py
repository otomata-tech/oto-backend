"""Serper — recherche Google Web + News.

Clé résolue par appel via `access.resolve_api_key("serper")` : user key
(`/account`) si posée, sinon platform key + quota daily pour les members.
Guests doivent obligatoirement poser leur propre clé.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    # Import au register pour fail-fast si le package n'est pas installé.
    from oto.tools.serper import SerperClient

    def _client() -> tuple[SerperClient, bool]:
        key, is_platform = access.resolve_api_key("serper", "SERPER_API_KEY")
        return SerperClient(api_key=key), is_platform

    @mcp.tool()
    async def serper_web_search(
        query: str,
        num: int = 10,
        country: Optional[str] = "fr",
        language: Optional[str] = "fr",
        site_filter: Optional[str] = None,
        tbs: Optional[str] = None,
    ) -> dict:
        """Google web search via Serper.

        Args:
            query: Search query.
            num: Number of results (max 100).
            country: Country code (default "fr").
            language: Language code (default "fr").
            site_filter: Restrict to a domain (e.g. "linkedin.com/in").
            tbs: Google time filter (e.g. "qdr:d" past day, "qdr:w" past week).
        """
        client, is_platform = _client()
        result = client.search(
            query=query, num=num, country=country, language=language,
            site_filter=site_filter, tbs=tbs,
        )
        if is_platform:
            access.record_platform_usage("serper")
        return result

    @mcp.tool()
    async def serper_news_search(
        query: str,
        num: int = 10,
        country: Optional[str] = "fr",
        language: Optional[str] = "fr",
        tbs: Optional[str] = None,
    ) -> dict:
        """Google News search via Serper.

        Useful for monitoring signals on a target company (PR, hiring, fundraising).
        """
        client, is_platform = _client()
        result = client.search_news(
            query=query, num=num, country=country, language=language, tbs=tbs,
        )
        if is_platform:
            access.record_platform_usage("serper")
        return result
