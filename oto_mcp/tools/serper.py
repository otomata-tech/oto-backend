"""Serper — recherche Google Web + News. Nécessite SERPER_API_KEY côté serveur."""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    from oto.tools.serper import SerperClient

    client = SerperClient()

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
        return client.search(
            query=query,
            num=num,
            country=country,
            language=language,
            site_filter=site_filter,
            tbs=tbs,
        )

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
        return client.search_news(
            query=query, num=num, country=country, language=language, tbs=tbs,
        )
