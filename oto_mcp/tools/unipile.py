"""Unipile — LinkedIn hébergé (recherche / scrape / messagerie).

Clé résolue par appel via `access.resolve_api_key("unipile")` (keyed, cascade
user > org). Le dsn (`api<NN>.unipile.com:port`) et l'account_id LinkedIn sont
résolus côté client (env `UNIPILE_DSN`, défaut api25 ; auto-résolution du 1er
compte LINKEDIN connecté).

Pourquoi à côté du connecteur browser `linkedin` : la session vit chez Unipile
(vrai Chrome + proxy résidentiel), ce qui contourne l'empreinte TLS et
l'isolation de session du browser local (issue #5) — au prix d'un SaaS payant.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, db


def register(mcp: FastMCP) -> None:
    # Import au register pour fail-fast si oto-core n'est pas installé.
    from oto.tools.unipile import UnipileClient

    def _client() -> UnipileClient:
        # Clé partagée (org) + account_id LinkedIn per-user : chacun agit comme
        # LUI-MÊME sous l'abonnement Unipile commun. PAS de fallback : sans
        # account_id connecté, le client oto-core retomberait sur le 1er compte de
        # l'abonnement → **usurpation cross-user** (audit sécu 2026-06-18). On exige
        # donc le credential per-user, sinon McpError actionnable.
        key, _is_platform = access.resolve_api_key("unipile", "UNIPILE_API_KEY")
        sub = access.current_user_sub_or_raise()
        account_id = db.get_unipile_account_id(sub)
        if not account_id:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message="Connecte ton compte LinkedIn sur https://oto.ninja/account "
                        "avant d'utiliser les outils Unipile."))
        return UnipileClient(api_key=key, account_id=account_id)

    @mcp.tool()
    async def unipile_search(
        keywords: Optional[str] = None,
        category: str = "people",
        company: Optional[list[str]] = None,
        location: Optional[list[str]] = None,
        cursor: Optional[str] = None,
    ) -> dict:
        """Recherche LinkedIn (classic) via Unipile.

        `company`/`location` acceptent des NOMS (résolus automatiquement en
        facettes LinkedIn) ou des ids de facette numériques. ⚠️ La page company
        LinkedIn n'est PAS un id de facette employeur valide pour la recherche
        people — passer le nom et laisser le client résoudre.

        Args:
            keywords: Mots-clés (nom, intitulé de poste…).
            category: "people" ou "companies".
            company: Employeur(s) — noms ou ids de facette.
            location: Localisation(s) — noms ou ids de facette.
            cursor: Curseur de pagination renvoyé par un appel précédent.
        """
        return _client().search(
            keywords=keywords, category=category,
            company=company, location=location, cursor=cursor,
        )

    @mcp.tool()
    async def unipile_profile(identifier: str, sections: str = "*") -> dict:
        """Profil LinkedIn complet (carrière datée, écoles, réseau) via Unipile.

        Args:
            identifier: public identifier (slug) ou provider id LinkedIn.
            sections: Sections à inclure ("*" = tout).
        """
        return _client().get_profile(identifier, sections=sections)

    @mcp.tool()
    async def unipile_company(identifier: str) -> dict:
        """Fiche société LinkedIn via Unipile.

        Args:
            identifier: slug ou id de la page société.
        """
        return _client().get_company(identifier)

    @mcp.tool()
    async def unipile_chats(limit: int = 20, cursor: Optional[str] = None) -> dict:
        """Liste les conversations LinkedIn (messagerie) via Unipile."""
        return _client().list_chats(limit=limit, cursor=cursor)

    @mcp.tool()
    async def unipile_send_message(
        text: str,
        chat_id: Optional[str] = None,
        recipient_id: Optional[str] = None,
    ) -> dict:
        """Envoie un message LinkedIn via Unipile.

        `chat_id` → répond dans un fil existant ; sinon `recipient_id` (provider
        id du destinataire) → ouvre un nouveau fil.

        Args:
            text: Contenu du message.
            chat_id: Id du fil pour répondre.
            recipient_id: provider id du destinataire (nouveau fil).
        """
        return _client().send_message(text, chat_id=chat_id, attendee_id=recipient_id)
