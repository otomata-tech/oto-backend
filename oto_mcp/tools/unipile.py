"""Unipile — LinkedIn & WhatsApp hébergés (recherche / scrape / messagerie).

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


def unipile_client(provider: str = "LINKEDIN"):
    """Client Unipile du user pour un canal (LINKEDIN, WHATSAPP, …).

    Clé partagée (org) + account_id per-user PAR CANAL : chacun agit comme
    LUI-MÊME sous l'abonnement Unipile commun. PAS de fallback : sans account_id
    connecté pour ce canal, le client oto-core retomberait sur le 1er compte de
    l'abonnement → **usurpation cross-user** (audit sécu 2026-06-18). On exige le
    credential per-user, sinon McpError actionnable. Réutilisé par tools/whatsapp.py.
    """
    from oto.tools.unipile import UnipileClient
    key, _is_platform = access.resolve_api_key("unipile")
    sub = access.current_user_sub_or_raise()
    account_id = db.get_unipile_account_id(sub, provider)
    if not account_id:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"Connecte ton compte {provider.title()} sur "
                    "https://dashboard.oto.ninja/console/connections "
                    "avant d'utiliser ces outils."))
    return UnipileClient(api_key=key, account_id=account_id)


def register_messaging_tools(mcp: FastMCP, channel: str) -> None:
    """Enregistre les 3 outils de messagerie Unipile d'un canal :
    `{c}_list_chats` / `{c}_read_chat` / `{c}_send_message` (résolus sur le compte
    <channel> de l'user, no-fallback). La messagerie Unipile (`/chats`) est
    channel-agnostic → un seul code pour WhatsApp/Telegram/Instagram. Appelé par
    tools/whatsapp.py, tools/telegram.py, tools/instagram.py."""
    cl = channel.lower()
    prov = channel.upper()

    @mcp.tool(name=f"{cl}_list_chats",
              description=f"Liste les conversations {channel} (messagerie) via Unipile.")
    async def _list_chats(limit: int = 20, cursor: Optional[str] = None) -> dict:
        return unipile_client(prov).list_chats(limit=limit, cursor=cursor)

    @mcp.tool(name=f"{cl}_read_chat",
              description=f"Lit les messages d'une conversation {channel} via Unipile "
                          f"(chat_id renvoyé par {cl}_list_chats).")
    async def _read_chat(chat_id: str, limit: int = 30) -> dict:
        return unipile_client(prov).list_messages(chat_id, limit=limit)

    @mcp.tool(name=f"{cl}_send_message",
              description=f"Envoie un message {channel} via Unipile. chat_id = répondre "
                          f"dans un fil existant ; sinon recipient_id = nouveau fil.")
    async def _send_message(text: str, chat_id: Optional[str] = None,
                            recipient_id: Optional[str] = None) -> dict:
        return unipile_client(prov).send_message(text, chat_id=chat_id, attendee_id=recipient_id)


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def unipile_search(
        keywords: Optional[str] = None,
        category: str = "people",
        company: Optional[list[str]] = None,
        location: Optional[list[str]] = None,
        industry: Optional[dict] = None,
        network_distance: Optional[list[int]] = None,
        advanced_keywords: Optional[dict] = None,
        url: Optional[str] = None,
        api: str = "classic",
        cursor: Optional[str] = None,
    ) -> dict:
        """Recherche LinkedIn via Unipile.

        `company`/`location`/`industry` acceptent des NOMS (résolus automatiquement
        en facettes LinkedIn) ou des ids de facette numériques. ⚠️ La page company
        LinkedIn n'est PAS un id de facette employeur valide pour la recherche
        people — passer le nom et laisser le client résoudre.

        Args:
            keywords: Mots-clés (nom, intitulé de poste…).
            category: "people" ou "companies".
            company: Employeur(s) — noms ou ids de facette.
            location: Localisation(s) — noms ou ids de facette.
            industry: filtre secteur — dict `{include?: [...], exclude?: [...]}` (noms ou ids).
            network_distance: degré de relation — `[1]`=1er degré (tes relations N1),
                `[2]`=2e, `[3]`=3e+. Combinable (`[1, 2]`) → cible « mes N1 sur [ville] ».
            advanced_keywords: ciblage people — dict `{first_name?, last_name?, title?,
                company?, school?}`.
            url: URL de recherche LinkedIn/Sales Nav collée du navigateur (si fournie,
                les autres filtres structurés sont ignorés).
            api: "classic" | "sales_navigator" | "recruiter" (filtres avancés selon
                l'abonnement LinkedIn du compte connecté).
            cursor: Curseur de pagination renvoyé par un appel précédent.
        """
        return unipile_client().search(
            keywords=keywords, category=category, company=company, location=location,
            industry=industry, network_distance=network_distance,
            advanced_keywords=advanced_keywords, url=url, api=api, cursor=cursor,
        )

    @mcp.tool()
    async def unipile_profile(identifier: str, sections: str = "*") -> dict:
        """Profil LinkedIn complet (carrière datée, écoles, réseau) via Unipile.

        Args:
            identifier: public identifier (slug) ou provider id LinkedIn.
            sections: Sections à inclure ("*" = tout).
        """
        return unipile_client().get_profile(identifier, sections=sections)

    @mcp.tool()
    async def unipile_company(identifier: str) -> dict:
        """Fiche société LinkedIn via Unipile.

        Args:
            identifier: slug ou id de la page société.
        """
        return unipile_client().get_company(identifier)

    @mcp.tool()
    async def unipile_chats(limit: int = 20, cursor: Optional[str] = None) -> dict:
        """Liste les conversations LinkedIn (messagerie) via Unipile."""
        return unipile_client().list_chats(limit=limit, cursor=cursor)

    @mcp.tool()
    async def unipile_read_chat(chat_id: str, limit: int = 30) -> dict:
        """Lit les messages d'une conversation LinkedIn via Unipile.

        Args:
            chat_id: Id du fil (renvoyé par unipile_chats).
            limit: Nombre de messages à récupérer.
        """
        return unipile_client().list_messages(chat_id, limit=limit)

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
        return unipile_client().send_message(text, chat_id=chat_id, attendee_id=recipient_id)

    @mcp.tool()
    async def unipile_relations(cursor: Optional[str] = None,
                                limit: Optional[int] = None) -> dict:
        """Liste tes relations LinkedIn de 1er degré (N1) via Unipile — pour
        cibler/exporter ton réseau direct. Paginé (`cursor`)."""
        return unipile_client().list_relations(cursor=cursor, limit=limit)

    @mcp.tool()
    async def unipile_invitations(direction: str = "received") -> dict:
        """Liste les invitations de connexion LinkedIn. `direction`='received'
        (reçues, à accepter) ou 'sent' (envoyées, en attente)."""
        return unipile_client().list_invitations(direction)

    @mcp.tool()
    async def unipile_send_invitation(provider_id: str,
                                      message: Optional[str] = None) -> dict:
        """Envoie une demande de connexion LinkedIn (outreach 2e/3e degré).

        Args:
            provider_id: provider id LinkedIn du destinataire (champ `provider_id`
                d'un résultat unipile_search / unipile_profile).
            message: note d'accompagnement optionnelle (≤300 caractères).
        """
        return unipile_client().send_invitation(provider_id, message=message)

    @mcp.tool()
    async def unipile_member_posts(identifier: str, cursor: Optional[str] = None,
                                   limit: Optional[int] = None) -> dict:
        """Posts publiés par un membre LinkedIn — `identifier` = provider id ou slug.
        Pour repérer un post à commenter/liker (social-selling)."""
        return unipile_client().list_member_posts(identifier, cursor=cursor, limit=limit)

    @mcp.tool()
    async def unipile_get_post(post_id: str) -> dict:
        """Récupère un post LinkedIn — `post_id` = social_id (`urn:li:…`) d'un résultat
        unipile_member_posts."""
        return unipile_client().get_post(post_id)

    @mcp.tool()
    async def unipile_post_engagement(post_id: str, kind: str = "comments",
                                      cursor: Optional[str] = None) -> dict:
        """Liste l'engagement d'un post LinkedIn — `kind`='comments' ou 'reactions'."""
        c = unipile_client()
        return c.list_reactions(post_id, cursor=cursor) if kind == "reactions" \
            else c.list_comments(post_id, cursor=cursor)

    @mcp.tool()
    async def unipile_comment(post_id: str, text: str) -> dict:
        """Commente un post LinkedIn (social-selling). `post_id` = social_id du post."""
        return unipile_client().comment_post(post_id, text)

    @mcp.tool()
    async def unipile_react(post_id: str, value: str = "LIKE") -> dict:
        """Réagit à un post LinkedIn. `value`: LIKE | PRAISE | EMPATHY | INTEREST |
        APPRECIATION | ENTERTAINMENT."""
        return unipile_client().react_post(post_id, value=value)

    @mcp.tool()
    async def unipile_create_post(text: str) -> dict:
        """Publie un post LinkedIn depuis le compte connecté."""
        return unipile_client().create_post(text)

    @mcp.tool()
    async def unipile_feed(count: int = 20, cursor: Optional[str] = None) -> dict:
        """Feed d'accueil LinkedIn (flux chronologique de ta page d'accueil) via Unipile.

        ⚠️ Passthrough Voyager via la Magic Route raw data d'Unipile (LinkedIn
        n'expose AUCUN endpoint feed) : route NON contractuelle, peut nécessiter une
        maintenance quand LinkedIn change son API interne. Parsing défensif — un item
        non mappé revient en `{_unmapped, _raw}` plutôt que de tout faire échouer.

        Retourne `{items, cursor, count}` où chaque item normalisé porte :
        `urn, author_name, author_headline, text, posted_at, posted_relative,
        reactions_count, comments_count, post_url`.

        Args:
            count: nombre d'items voulus sur la page (défaut 20).
            cursor: curseur renvoyé par un appel précédent (champ `cursor`) pour la
                page suivante. None = première page.
        """
        return unipile_client().get_feed(count=count, cursor=cursor)
