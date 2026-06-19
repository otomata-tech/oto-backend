"""Messenger — messagerie hébergée via Unipile (compte connecté par l'user).

Outils `messenger_*` dérivés de la factory messagerie commune
(cf. `tools/unipile.register_messaging_tools`). Connexion via hosted-auth
(dashboard, `?channel=messenger`).
"""
from __future__ import annotations

from fastmcp import FastMCP

from .unipile import register_messaging_tools


def register(mcp: FastMCP) -> None:
    register_messaging_tools(mcp, "MESSENGER")
