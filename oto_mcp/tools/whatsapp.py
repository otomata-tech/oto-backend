"""WhatsApp — Baileys via subprocess Node.js (oto.tools.whatsapp).

WhatsApp est par-utilisateur (pas de "platform session" possible). Chaque
admin a sa propre session paired vivant dans `<DATA_DIR>/whatsapp/<sub>/`.

Pour l'instant **réservé au rôle admin** parce que le pairing QR doit se
faire manuellement (rsync d'une session existante ou pairing via la CLI sur
la machine du serveur). Quand on aura une UI de pairing dans `/account`
(streamage du QR vers le browser pendant le `auth`), on pourra ouvrir aux
members. Backlog : issue séparée à créer.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access


def _data_dir() -> Path:
    return Path(os.environ.get("OTO_MCP_DATA_DIR", "/opt/oto-mcp/data"))


def _client_for_user():
    sub = access.current_user_sub_or_raise()
    role = access.get_user_role(sub)
    if role != access.ADMIN:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                "WhatsApp est réservé au rôle admin pour l'instant — le "
                "pairing QR depuis l'UI n'est pas encore livré. Contacte "
                "Alexis si tu as besoin d'utiliser ce tool."
            ),
        ))

    auth_dir = _data_dir() / "whatsapp" / sub
    if not auth_dir.exists() or not any(auth_dir.iterdir()):
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                f"Session WhatsApp introuvable côté serveur ({auth_dir}). "
                "Le pairing QR n'a pas été fait ou la session a expiré."
            ),
        ))

    from oto.tools.whatsapp import WhatsAppClient
    client = WhatsAppClient()
    client.auth_dir = str(auth_dir)
    return client


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def whatsapp_list_chats(limit: int = 20) -> dict:
        """List the user's recent WhatsApp chats (most recent first).

        Returns {jid, name, last_message_preview, last_message_at}. Pour
        prospection : voir avec qui tu as déjà discuté avant de relancer.
        """
        client = _client_for_user()
        return await asyncio.to_thread(client.list_chats, limit=limit)

    @mcp.tool()
    async def whatsapp_read_chat(chat: str, limit: int = 20) -> dict:
        """Read messages from a specific WhatsApp chat (most recent first).

        Args:
            chat: JID complet (ex `33612345678@s.whatsapp.net`) ou numéro
                international (ex `+33612345678`).
            limit: Max messages.
        """
        client = _client_for_user()
        return await asyncio.to_thread(client.read, chat=chat, limit=limit)

    @mcp.tool()
    async def whatsapp_send_message(to: str, message: str) -> dict:
        """Send a WhatsApp message FROM the user's account.

        ⚠️ Action sensible : envoie en ton nom. Confirme l'intention avant
        d'appeler ce tool dans un workflow agent.

        Args:
            to: JID complet ou numéro international (`+33...`).
            message: Texte du message.
        """
        client = _client_for_user()
        return await asyncio.to_thread(client.send, to=to, message=message)
