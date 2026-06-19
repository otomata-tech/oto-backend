"""WhatsApp — messagerie hébergée via Unipile (compte WhatsApp connecté par l'user).

Ex-Baileys (self-hosted, subprocess Node.js) → remplacé par Unipile : le compte
WhatsApp vit chez Unipile (linked-device), connecté par l'user via le hosted-auth
(dashboard, `?channel=whatsapp`). Outils `whatsapp_*` dérivés de la factory
messagerie commune (cf. `tools/unipile.register_messaging_tools`). L'engine Baileys
reste archivé dans oto-core (`oto.tools.whatsapp`) + la CLI `oto whatsapp`.
"""
from __future__ import annotations

from fastmcp import FastMCP

from .unipile import register_messaging_tools


def register(mcp: FastMCP) -> None:
    register_messaging_tools(mcp, "WHATSAPP")
