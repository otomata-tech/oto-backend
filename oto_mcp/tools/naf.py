"""Suggestion de codes NAF depuis une description d'activité (LLM Groq)."""
from __future__ import annotations

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    from oto.tools.naf import NAFSuggester

    suggester = NAFSuggester()

    @mcp.tool()
    async def naf_suggest(description: str, limit: int = 3) -> dict:
        """Suggest NAF activity codes from a French activity description.

        Useful in prospecting workflows : transformer un brief client
        ("fabricant de mobilier de bureau") en filtre NAF utilisable par
        `recherche_entreprises_search` / `sirene_search`.

        Returns a list of {code, label, confidence, reason}.
        """
        suggestions = suggester.suggest(description, limit=limit)
        return {
            "suggestions": [
                {"code": s.code, "label": s.label, "confidence": s.confidence, "reason": s.reason}
                for s in suggestions
            ]
        }
