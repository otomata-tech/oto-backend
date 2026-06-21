"""Cloro — veille AI-search & SERP Google en JSON (cloro.dev).

Wrappe `oto.tools.cloro.CloroClient`. Surfaces métier :
- **moteurs IA** (ChatGPT, Gemini, Perplexity, Copilot, Grok, Google AI Mode) :
  interroge le moteur et capture sa réponse + sources/citations → veille de marque
  « AI SEO » (ce que l'IA dit d'une marque/produit), intelligence concurrentielle.
- **Google SERP** en JSON (organique + AI Overview + People Also Ask) et **Google
  News**.

Clé résolue par appel via `access.resolve_api_key("cloro")` : user/org key sinon
clé plateforme + quota daily pour les members. NB : les appels moteurs IA peuvent
prendre ~30-45 s.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access

# Moteurs IA → (slug API Cloro, libellé humain). Le nom du tool dérive du slug
# (aimode → cloro_ai_mode) ; chacun est un tool « métier » nommé.
_AI_ENGINES = {
    "chatgpt": "ChatGPT (OpenAI)",
    "perplexity": "Perplexity",
    "gemini": "Google Gemini",
    "copilot": "Microsoft Copilot",
    "grok": "Grok (xAI)",
    "aimode": "Google AI Mode",
}


def register(mcp: FastMCP) -> None:
    from oto.tools.cloro.client import CloroClient

    def _client() -> tuple[CloroClient, bool]:
        key, is_platform = access.resolve_api_key("cloro")
        return CloroClient(api_key=key), is_platform

    def _run(method: str, **kwargs) -> dict:
        """Résout la clé, appelle la méthode du client, compte l'usage plateforme."""
        client, is_platform = _client()
        result = getattr(client, method)(**kwargs)
        if is_platform:
            access.record_platform_usage("cloro")
        return result

    # --- moteurs IA : un tool nommé par moteur (factory) --------------------
    for _slug, _label in _AI_ENGINES.items():
        _tool_name = "cloro_ai_mode" if _slug == "aimode" else f"cloro_{_slug}"

        def _make(slug: str, label: str, tool_name: str):
            @mcp.tool(
                name=tool_name,
                description=(
                    f"Ask {label} and capture its answer + sources/citations "
                    f"(AI-search brand monitoring / AI SEO — what {label} says about "
                    f"a brand, product or topic).\n\n"
                    "Args:\n"
                    f"    prompt: question/query to send to {label} (1-10000 chars).\n"
                    "    country: ISO country code (e.g. 'US', 'FR').\n"
                    "    markdown: return a markdown rendition of the answer.\n"
                    "    search_queries: also return the engine's internal fan-out "
                    "queries (costs extra credits)."
                ),
            )
            async def _engine_tool(
                prompt: str,
                country: Optional[str] = None,
                markdown: bool = True,
                search_queries: bool = False,
            ) -> dict:
                include: dict = {"markdown": markdown}
                if search_queries:
                    include["searchQueries"] = True
                return _run("monitor", provider=slug, prompt=prompt,
                            country=country, include=include)

            return _engine_tool

        _make(_slug, _label, _tool_name)

    # --- Google SERP / News : tools nommés (corps `query`) ------------------
    @mcp.tool()
    async def cloro_google_serp(
        query: str,
        country: Optional[str] = None,
        ai_overview: bool = True,
        organic: bool = True,
        people_also_ask: bool = False,
    ) -> dict:
        """Google SERP as clean JSON via Cloro (AI SEO / SERP monitoring).

        Args:
            query: search query.
            country: ISO country code (e.g. 'US', 'FR').
            ai_overview: include Google's AI Overview block.
            organic: include organic results.
            people_also_ask: include People Also Ask.
        """
        include = {
            "aiOverview": ai_overview,
            "organicResults": organic,
            "peopleAlsoAsk": people_also_ask,
        }
        return _run("google", query=query, country=country, include=include)

    @mcp.tool()
    async def cloro_google_news(query: str, country: Optional[str] = None) -> dict:
        """Google News as JSON via Cloro.

        Args:
            query: search query.
            country: ISO country code (e.g. 'US', 'FR').
        """
        return _run("google_news", query=query, country=country)
