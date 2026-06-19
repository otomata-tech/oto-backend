"""WhatsApp — messagerie hébergée via Unipile (compte WhatsApp connecté par l'user).

Ex-Baileys (self-hosted, subprocess Node.js) → remplacé par Unipile : le compte
WhatsApp vit chez Unipile (linked-device), connecté par l'user via le hosted-auth
(dashboard, `?channel=whatsapp`). On résout son `account_id` WHATSAPP per-user
(no-fallback, cf. `tools/unipile.unipile_client`). L'engine Baileys reste archivé
dans oto-core (`oto.tools.whatsapp`) + la CLI `oto whatsapp`.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .unipile import unipile_client


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def whatsapp_list_chats(limit: int = 20, cursor: Optional[str] = None) -> dict:
        """Liste les conversations WhatsApp (messagerie) via Unipile."""
        return unipile_client("WHATSAPP").list_chats(limit=limit, cursor=cursor)

    @mcp.tool()
    async def whatsapp_read_chat(chat_id: str, limit: int = 30) -> dict:
        """Lit les messages d'une conversation WhatsApp via Unipile.

        Args:
            chat_id: Id du fil (renvoyé par whatsapp_list_chats).
            limit: Nombre de messages à récupérer.
        """
        return unipile_client("WHATSAPP").list_messages(chat_id, limit=limit)

    @mcp.tool()
    async def whatsapp_send_message(
        text: str,
        chat_id: Optional[str] = None,
        recipient_id: Optional[str] = None,
    ) -> dict:
        """Envoie un message WhatsApp via Unipile.

        `chat_id` → répond dans un fil existant ; sinon `recipient_id` (provider id
        du destinataire, ex. numéro au format Unipile) → ouvre un nouveau fil.

        Args:
            text: Contenu du message.
            chat_id: Id du fil pour répondre.
            recipient_id: provider id du destinataire (nouveau fil).
        """
        return unipile_client("WHATSAPP").send_message(
            text, chat_id=chat_id, attendee_id=recipient_id)
