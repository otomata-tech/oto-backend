"""Provider `anthropic` — credential-only (clé Anthropic de l'org), aucun tool propre.

La clé n'est pas consommée par un outil MCP mais par le **substrat d'agent** :
`agent_llm.resolve_key()` la résout via `access.resolve_api_key("anthropic")`
(cascade membre > groupe > org > grant plateforme) pour faire tourner la boucle
de tool calling server-side d'un projet — et, à terme, les routines planifiées.

Ce module existe uniquement pour satisfaire l'invariant « un fichier tools/ par
provider kind=tools » (test_capabilities_drift) ; `register_all` l'importe et
appelle `register()` qui n'enregistre rien.

⚠️ Nom de module = `anthropic`, homonyme du SDK. `oto_mcp.tools.anthropic` ne
masque PAS le paquet tiers (imports absolus depuis Python 3) — et `agent_llm`
importe le SDK, jamais ce module.
"""
from __future__ import annotations

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:  # noqa: ARG001 — credential consommé par agent_llm
    return
