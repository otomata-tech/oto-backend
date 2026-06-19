"""Telegram — messagerie hébergée via Unipile (compte Telegram connecté par l'user).

Le compte Telegram vit chez Unipile, connecté par l'user via le hosted-auth
(dashboard, `?channel=telegram`). Outils `telegram_*` dérivés de la factory
messagerie commune (cf. `tools/unipile.register_messaging_tools`).
"""
from __future__ import annotations

from fastmcp import FastMCP

from .unipile import register_messaging_tools


def register(mcp: FastMCP) -> None:
    register_messaging_tools(mcp, "TELEGRAM")
