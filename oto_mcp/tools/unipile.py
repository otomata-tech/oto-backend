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

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, connector_verify, db

logger = logging.getLogger(__name__)

# Miroir autogéré du feed (home LinkedIn) dans le datastore spine (ADR 0016).
_FEED_NS = "linkedin-feed"          # namespace datastore per-user
_FEED_SYNC_CAP_PAGES = 5            # garde-fou anti-martelage LinkedIn par sync
_FEED_PAGE_COUNT = 40              # items par page Voyager pendant le sync
_FEED_SORT_ORDER = "MEMBER_SETTING"  # honore le tri choisi sur la home LinkedIn


def _feed_ttl_seconds() -> int:
    try:
        return int(os.environ.get("OTO_UNIPILE_FEED_TTL_SECONDS", "600"))
    except ValueError:
        return 600


def _feed_is_stale(sub: str, provider: str = "LINKEDIN") -> bool:
    """True si le cache du feed mérite un refresh (jamais sync, ou plus vieux que
    le TTL). Tolérant au format d'horodatage (string row-factory)."""
    ts = db.get_unipile_feed_synced_at(sub, access.current_org(sub), provider)
    if not ts:
        return True
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() >= _feed_ttl_seconds()


def _sync_feed(client, store, sub: str, provider: str = "LINKEDIN") -> int:
    """Pagine le feed live et upsert chaque post dans le datastore (dédup par
    `urn`). S'arrête dès qu'une page entière n'apporte AUCUN urn nouveau (condition
    robuste à l'ordre de tri) ou au cap de pages. Renvoie le nombre de posts neufs.
    Marque le sync à la fin. Best-effort : un item sans urn est ignoré."""
    new_count = 0
    cursor = None
    for _ in range(_FEED_SYNC_CAP_PAGES):
        page = client.get_feed(count=_FEED_PAGE_COUNT, cursor=cursor,
                               sort_order=_FEED_SORT_ORDER)
        items = page.get("items") or []
        if not items:
            break
        page_new = 0
        for item in items:
            urn = item.get("urn")
            if not urn:
                continue
            _row, inserted = store.upsert_row(_FEED_NS, urn, item)
            if inserted:
                page_new += 1
        new_count += page_new
        cursor = page.get("cursor")
        if page_new == 0 or not cursor:
            break  # rattrapé (page déjà connue) ou fin de flux
    db.touch_unipile_feed_synced(sub, access.current_org(sub), provider)
    return new_count


# Canaux Unipile : clé front → provider DB. Source unique de la liste de canaux
# (consommée par status_for ; calquée côté front dans ConnectorHostedWidget).
UNIPILE_CHANNELS = {
    "linkedin": "LINKEDIN", "whatsapp": "WHATSAPP", "telegram": "TELEGRAM",
    "instagram": "INSTAGRAM", "messenger": "MESSENGER", "twitter": "TWITTER",
}


def _channels_from(accts_by_provider: dict) -> dict:
    """Construit le dict des 6 canaux à partir des comptes indexés par provider DB."""
    def _ch(provider: str) -> dict:
        a = accts_by_provider.get(provider)
        return {
            "connected": a is not None,
            "account_id": a["account_id"] if a else None,
            "account_name": a.get("account_name") if a else None,
            "connected_at": str(a["connected_at"]) if a else None,
        }
    return {front: _ch(prov) for front, prov in UNIPILE_CHANNELS.items()}


def status_for(sub: str, *, org=access._UNSET, group=access._UNSET) -> dict:
    """État Unipile per-user : canaux connectés + option débloquée + mode de clé.
    SOURCE UNIQUE consommée par `/api/me/unipile` (face user). BYO (clé propre
    user/groupe/org) ⇒ option ouverte (l'user gère sa propre instance). Sinon l'option
    de messagerie hébergée doit avoir été accordée à l'org par un admin (comp).
    `org`/`group` explicites = état d'un TIERS contre son propre contexte, sans le
    contexte view-as/session du requérant (anti-fuite, cf. access._UNSET).
    Scope membre (ADR 0033 B4) : les canaux montrés = ceux rattachés à l'org de
    contexte (les bindings des autres orgs n'existent pas ici)."""
    o = access.current_org(sub) if org is access._UNSET else org
    accts = {a["provider"]: a for a in db.list_unipile_accounts(sub)
             if a.get("org_id") == o}
    mode = access.credential_mode_for(sub, "unipile", org=org, group=group)
    byo = mode in access.BYO_MODES
    subscribed = byo or access.has_option(sub, "unipile", org=org)
    return {
        "subscribed": subscribed,   # option débloquée (BYO ou comp admin) — gate « connecter »
        "mode": mode,  # user|group|org|platform|over_quota|forbidden (origine de la clé)
        "byo": byo,
        "channels": _channels_from(accts),
    }


def admin_status_by_org(sub: str, orgs: list) -> list:
    """État messagerie **par org** pour la fiche admin (un user peut être dans N orgs ;
    l'option est PAR ORG). `orgs` = `org_store.list_orgs_for_user(sub)`.
    Pour chaque org : option/mode calculés CONTRE CETTE org + canaux rattachés à elle
    (`unipile_accounts.org_id`). Les comptes rattachés à une org hors de sa liste tombent
    dans un bloc « (hors de ses orgs) »."""
    accts = db.list_unipile_accounts(sub)
    out = []
    for o in orgs:
        oid = o["org_id"]
        mode = access.credential_mode_for(sub, "unipile", org=oid)
        byo = mode in access.BYO_MODES
        by = {a["provider"]: a for a in accts if a.get("org_id") == oid}
        out.append({
            "org_id": oid, "org_name": o.get("name"), "is_active": bool(o.get("is_active")),
            "subscribed": byo or access.has_option(sub, "unipile", org=oid),
            "mode": mode, "byo": byo,
            "channels": _channels_from(by),
            "option_source": {
                "user_comp": db.has_option_comp("user", sub, "unipile"),
                "org_comp": db.has_option_comp("org", str(oid), "unipile"),
            },
        })
    member = {o["org_id"] for o in orgs}
    orphans = {a["provider"]: a for a in accts if a.get("org_id") not in member}
    if orphans:
        out.append({
            "org_id": None, "org_name": "(hors de ses orgs)", "is_active": False,
            "subscribed": None, "mode": None, "byo": None,
            "channels": _channels_from(orphans), "option_source": None,
        })
    return out


def unipile_client(provider: str = "LINKEDIN"):
    """Client Unipile du user pour un canal (LINKEDIN, WHATSAPP, …).

    Clé partagée (org) + account_id per-user PAR CANAL : chacun agit comme
    LUI-MÊME sous l'abonnement Unipile commun. PAS de fallback : sans account_id
    connecté pour ce canal, le client oto-core retomberait sur le 1er compte de
    l'abonnement → **usurpation cross-user** (audit sécu 2026-06-18). On exige le
    credential per-user, sinon McpError actionnable. Réutilisé par tools/whatsapp.py.

    SEULE exception (#55) : un compte ACCORDÉ par son propriétaire
    (`connector_account_grants`, revalidé à CHAQUE appel — révocation immédiate),
    résolu par `connector_identities.resolve_operated_account_id`. Limitation : si
    le owner est sur une AUTRE clé Unipile que le grantee (BYO perso ≠ clé
    partagée), l'API Unipile répondra 404 sur l'account_id — erreur surfacée telle
    quelle (la clé résolue est indépendante du compte).
    """
    from oto.tools.unipile import make_unipile_client
    from .. import connector_identities
    rc = access.resolve_credential("unipile", want="auto")
    sub = access.current_user_sub_or_raise()
    try:
        account_id = connector_identities.resolve_operated_account_id(sub, provider)
    except ValueError as e:  # pointeur opéré révoqué/déconnecté → erreur explicite
        raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
    # Pin projet (#57) : si le projet actif épingle un compte unipile, il prime sur le
    # défaut per-canal — MAIS seulement s'il appartient à CE user DANS CETTE org
    # (anti-usurpation + scope membre ADR 0033) OU lui est accordé par son propriétaire
    # (#55, grant vivant re-checké à cet appel), ET au canal demandé. Sinon défaut (fail-soft).
    org = access.current_org(sub)
    pinned = access.project_pinned_identity("unipile")
    if pinned and (
        any(a.get("account_id") == pinned and a.get("provider") == provider
            and a.get("org_id") == org
            for a in db.list_unipile_accounts(sub))
        or pinned in db.granted_accounts_for(sub, provider)
    ):
        account_id = pinned
    if not account_id:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"Connecte ton compte {provider.title()} sur "
                    "https://dashboard.oto.ninja/console/connections "
                    "avant d'utiliser ces outils."))
    # DSN apparié à la clé BYO gagnante (chaque clé Unipile est liée à son
    # sous-domaine), tiré de la config du credential résolu. Clé plateforme →
    # DSN env/défaut (instance Otomata).
    dsn = None if rc.is_platform else rc.config.get("dsn")
    # Version d'API : v1 par défaut (prod). Opt-in v2 par la config du credential
    # (`api_version`) — la v2 Unipile impose un compte/clé dédiés, donc la bascule
    # suit la clé — sinon repli sur l'env `OTO_UNIPILE_API_VERSION` (bascule globale).
    api_version = (rc.config.get("api_version")
                   or os.environ.get("OTO_UNIPILE_API_VERSION") or "v1")
    return make_unipile_client(api_key=rc.key, account_id=account_id, dsn=dsn,
                               api_version=api_version)


def _verify(fields: dict) -> None:
    """Sonde de connexion Unipile (#133) : `list_accounts()` sur la clé résolue.

    Teste l'auth ET le contenu d'un coup — un endpoint compte-agnostique donc pas
    besoin d'account_id (la sonde ne reçoit que les champs du credential, pas la
    config/dsn). On distingue trois cas :
    - clé absente → message actionnable (ne devrait pas arriver : `_fields_for`
      résout le credential en amont, mais on garde le garde-fou) ;
    - clé morte / refusée → `UnipileError` (401/4xx) laissée remonter telle quelle
      (son message = le retour d'erreur de la sonde) ;
    - clé valide mais AUCUN compte connecté → distinct d'un listing cassé, on lève
      un message qui oriente vers le hosted-auth du dashboard."""
    from oto.tools.unipile import make_unipile_client

    api_key = fields.get("api_key")
    if not api_key:
        raise ValueError("clé API Unipile absente.")
    client = make_unipile_client(api_key=api_key)  # dsn=None → défaut Otomata
    accounts = client.list_accounts()
    if not accounts:
        raise ValueError(
            "clé Unipile valide mais aucun compte connecté — connecte un compte "
            "via le hosted-auth du dashboard (unipile_connect_start).")


def register_messaging_tools(mcp: FastMCP, channel: str) -> None:
    """Enregistre les 3 outils de messagerie Unipile d'un canal :
    `{c}_list_chats` / `{c}_read_chat` / `{c}_send_message` (résolus sur le compte
    <channel> de l'user, no-fallback). La messagerie Unipile (`/chats`) est
    channel-agnostic → un seul code pour WhatsApp/Telegram/Instagram. Appelé par
    tools/whatsapp.py, tools/telegram.py, tools/instagram.py."""
    cl = channel.lower()
    prov = channel.upper()

    @mcp.tool(name=f"{cl}_list_chats",
              description=f"Liste les conversations {channel} (messagerie) via Unipile. "
                          "Paginé (limit + cursor) ; chaque fil 1-à-1 est enrichi du nom "
                          "de l'interlocuteur (attendee_name), with_names=False le coupe.")
    def _list_chats(limit: int = 20, cursor: Optional[str] = None,
                    with_names: bool = True) -> dict:
        return unipile_client(prov).list_chats(limit=limit, cursor=cursor,
                                               with_attendee_names=with_names)

    @mcp.tool(name=f"{cl}_read_chat",
              description=f"Lit les messages d'une conversation {channel} via Unipile "
                          f"(chat_id renvoyé par {cl}_list_chats).")
    def _read_chat(chat_id: str, limit: int = 30) -> dict:
        return unipile_client(prov).list_messages(chat_id, limit=limit)

    @mcp.tool(name=f"{cl}_send_message",
              description=f"Envoie un message {channel} via Unipile. chat_id = répondre "
                          f"dans un fil existant ; sinon recipient_id = nouveau fil.")
    def _send_message(text: str, chat_id: Optional[str] = None,
                            recipient_id: Optional[str] = None) -> dict:
        return unipile_client(prov).send_message(text, chat_id=chat_id, attendee_id=recipient_id)


def register(mcp: FastMCP) -> None:

    connector_verify.register("unipile", _verify)

    @mcp.tool()
    async def unipile_connect_start(channel: str = "linkedin") -> dict:
        """Démarre la connexion d'un compte de messagerie hébergé (LinkedIn par
        défaut) et renvoie une **`url`** d'auth Unipile à transmettre à l'utilisateur.

        L'utilisateur ouvre l'URL, se connecte à son compte (login/2FA/captcha —
        tout se passe dans cette page hébergée) ; la liaison se **finalise
        automatiquement** côté serveur (webhook), rien d'autre à appeler ensuite.
        Vérifie l'état avec `oto_verify_connector(provider='unipile')`. C'est LE
        point d'entrée d'onboarding messagerie depuis l'agent (feedback #131).

        Args:
            channel: canal à connecter — linkedin (défaut), whatsapp, telegram,
                instagram, messenger, twitter.
        """
        from .. import unipile_connect

        sub = access.current_user_sub_or_raise()
        try:
            out = await unipile_connect.hosted_auth_url(sub, channel)
        except unipile_connect.ConnectRefused as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=e.message))
        out["instructions"] = (
            f"Transmets `url` à l'utilisateur : il ouvre le lien, connecte son compte "
            f"{out.get('channel', channel)}, et la liaison se finalise seule "
            "(webhook). Vérifie ensuite avec oto_verify_connector(provider='unipile').")
        return out

    @mcp.tool()
    def unipile_search(
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
    def unipile_profile(identifier: str, sections: str = "*") -> dict:
        """Profil LinkedIn complet (carrière datée, écoles, réseau) via Unipile.

        ⚠️ LinkedIn peut throttler une section (souvent `experience`) : la réponse
        porte alors `throttled_sections=[…]` avec la section vide malgré un
        `*_total_count` > 0. C'est un rate-limit AMONT, pas une absence de donnée :
        réessaie plus tard (minutes), réduis la concurrence (≤8 en parallèle), et
        sur un batch traite ces cibles dans une passe de rattrapage différée.
        VÉRIFIE aussi que le `public_identifier`/id renvoyé == demandé avant
        d'écrire (rejette + retry sinon).

        Args:
            identifier: public identifier (slug) ou provider id LinkedIn.
            sections: Sections à inclure ("*" = tout).
        """
        return unipile_client().get_profile(identifier, sections=sections)

    @mcp.tool()
    def unipile_company(identifier: str) -> dict:
        """Fiche société LinkedIn via Unipile.

        Args:
            identifier: slug ou id de la page société.
        """
        return unipile_client().get_company(identifier)

    @mcp.tool()
    def unipile_chats(limit: int = 20, cursor: Optional[str] = None,
                      with_names: bool = True) -> dict:
        """Liste les conversations LinkedIn (messagerie) via Unipile. Paginé
        (`limit` + `cursor`).

        Chaque fil 1-à-1 est enrichi de `attendee_name`/`attendee_headline`/
        `attendee_profile_url` (résolus en batch — le `name` brut des fils 1-à-1
        est null et `attendee_provider_id` est opaque). `with_names=False` coupe
        cet enrichissement (payload brut, un appel API en moins)."""
        return unipile_client().list_chats(limit=limit, cursor=cursor,
                                           with_attendee_names=with_names)

    @mcp.tool()
    def unipile_read_chat(chat_id: str, limit: int = 30) -> dict:
        """Lit les messages d'une conversation LinkedIn via Unipile.

        Args:
            chat_id: Id du fil (renvoyé par unipile_chats).
            limit: Nombre de messages à récupérer.
        """
        return unipile_client().list_messages(chat_id, limit=limit)

    @mcp.tool()
    def unipile_send_message(
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
    def unipile_relations(cursor: Optional[str] = None,
                                limit: Optional[int] = None,
                                fields: Optional[list] = None) -> dict:
        """Liste tes relations LinkedIn de 1er degré (N1) via Unipile — pour
        cibler/exporter ton réseau direct. Paginé (`cursor`).

        `fields` = PROJECTION : ne garde que ces champs sur chaque item (ex.
        `["name","headline","public_identifier","member_id","created_at"]`) —
        réduit fortement le payload d'un export (le plein item porte des champs
        lourds inutiles : profile_picture_url, urns…). Omis = items complets.

        ⚠️ Pagination NON fiable pour un export EXHAUSTIF : le `cursor` encode un
        offset volatil (doublons dans l'espace d'offset, total surestimé) et une
        page `limit=100` rend 90-100 items, pas 100. Pour charger tout un réseau :
        dédupliquer par `member_id` (JAMAIS l'offset), garder ≤8 pages en parallèle
        (au-delà : 502 en cascade), prouver le tarissement par 2 passes décalées.
        Doctrine dédiée : `bulk-load-reseau`."""
        out = unipile_client().list_relations(cursor=cursor, limit=limit)
        if fields and isinstance(out, dict) and isinstance(out.get("items"), list):
            keep = set(fields)
            out["items"] = [{k: v for k, v in it.items() if k in keep}
                            for it in out["items"] if isinstance(it, dict)]
        return out

    @mcp.tool()
    def unipile_invitations(direction: str = "received", limit: int = 50,
                            cursor: Optional[str] = None) -> dict:
        """Liste les invitations de connexion LinkedIn. `direction`='received'
        (reçues, à accepter) ou 'sent' (envoyées, en attente). Paginé : `limit`
        (défaut 50 — sans borne le backlog entier dépasse la limite de tokens)
        + `cursor` pour la page suivante."""
        return unipile_client().list_invitations(direction, limit=limit, cursor=cursor)

    @mcp.tool()
    def unipile_send_invitation(provider_id: str,
                                      message: Optional[str] = None) -> dict:
        """Envoie une demande de connexion LinkedIn (outreach 2e/3e degré).

        Args:
            provider_id: provider id LinkedIn du destinataire (champ `provider_id`
                d'un résultat unipile_search / unipile_profile).
            message: note d'accompagnement optionnelle (≤300 caractères).
        """
        return unipile_client().send_invitation(provider_id, message=message)

    @mcp.tool()
    def unipile_member_posts(identifier: str, cursor: Optional[str] = None,
                                   limit: Optional[int] = None) -> dict:
        """Posts publiés par un membre LinkedIn — `identifier` = provider id ou slug.
        Pour repérer un post à commenter/liker (social-selling)."""
        return unipile_client().list_member_posts(identifier, cursor=cursor, limit=limit)

    @mcp.tool()
    def unipile_get_post(post_id: str) -> dict:
        """Récupère un post LinkedIn — `post_id` = social_id (`urn:li:…`) d'un résultat
        unipile_member_posts."""
        return unipile_client().get_post(post_id)

    @mcp.tool()
    def unipile_post_engagement(post_id: str, kind: str = "comments",
                                      cursor: Optional[str] = None) -> dict:
        """Liste l'engagement d'un post LinkedIn — `kind`='comments' ou 'reactions'."""
        c = unipile_client()
        return c.list_reactions(post_id, cursor=cursor) if kind == "reactions" \
            else c.list_comments(post_id, cursor=cursor)

    @mcp.tool()
    def unipile_comment(post_id: str, text: str) -> dict:
        """Commente un post LinkedIn (social-selling). `post_id` = social_id du post."""
        return unipile_client().comment_post(post_id, text)

    @mcp.tool()
    def unipile_react(post_id: str, value: str = "LIKE") -> dict:
        """Réagit à un post LinkedIn. `value`: LIKE | PRAISE | EMPATHY | INTEREST |
        APPRECIATION | ENTERTAINMENT."""
        return unipile_client().react_post(post_id, value=value)

    @mcp.tool()
    def unipile_create_post(text: str) -> dict:
        """Publie un post LinkedIn depuis le compte connecté."""
        return unipile_client().create_post(text)

    @mcp.tool()
    def unipile_feed(limit: int = 20, page: int = 0, refresh: bool = False) -> dict:
        """Miroir autogéré de ta home LinkedIn. Tu n'as RIEN à gérer (ni curseur, ni
        sync) : l'outil persiste les posts de ta page d'accueil dans ta base
        (datastore `linkedin-feed`, dédupliqués par leur identifiant), rafraîchit
        tout seul quand le cache est périmé, et te sert le miroir le plus récent en
        tête. Les encarts sponsorisés/promo sont exclus.

        Sous le capot : à `page=0`, refresh si le cache a dépassé son TTL — on
        pagine le feed live et on n'ajoute que les posts neufs (arrêt dès qu'une page
        est déjà connue). Les pages suivantes (`page>0`) lisent le miroir stocké sans
        retaper LinkedIn. Le tri suit ton réglage de home LinkedIn (« Plus récents »
        pour un miroir chronologique) ; quoi qu'il arrive le miroir est re-trié par
        date de publication.

        Le miroir complet reste requêtable via `data_rows('linkedin-feed')` (filtrage
        par date côté nous, impossible sur le feed Voyager brut).

        Args:
            limit: nombre de posts à renvoyer pour cette page (défaut 20).
            page: page du miroir (0 = la plus récente). >0 ne déclenche pas de refresh.
            refresh: force un rafraîchissement live même si le cache est encore frais.

        Retourne `{items, total, page, limit, synced}` — `items` = posts (récent en
        tête, mêmes champs qu'avant : urn, author_name, author_headline, text,
        posted_at, reactions_count, comments_count, post_url), `total` = taille du
        miroir, `synced` = True si un refresh live a eu lieu.
        """
        from ..datastore import make_store, NamespaceNotFound

        sub = access.current_user_sub_or_raise()
        client = unipile_client()
        store = make_store(sub)

        synced = False
        if page <= 0 and (refresh or _feed_is_stale(sub)):
            _sync_feed(client, store, sub)
            synced = True

        try:
            rows = store.list_rows(_FEED_NS, limit=10_000)
        except NamespaceNotFound:
            rows = []
        rows.sort(key=lambda r: r.get("posted_at") or "", reverse=True)

        offset = max(0, page) * limit
        window = rows[offset:offset + limit]
        return {"items": window, "total": len(rows), "page": page,
                "limit": limit, "synced": synced}

    # ---- réseau : invitations (accepter / annuler) ----------------------

    @mcp.tool()
    def unipile_handle_invitation(invitation_id: str, shared_secret: str,
                                        action: str = "accept") -> dict:
        """Accepte ou refuse une invitation LinkedIn REÇUE.

        `invitation_id` ET `shared_secret` proviennent d'un item de
        `unipile_invitations(direction='received')`.

        Args:
            invitation_id: id de l'invitation reçue.
            shared_secret: token LinkedIn du même item (obligatoire).
            action: 'accept' ou 'decline'.
        """
        return unipile_client().handle_invitation(invitation_id, shared_secret, action)

    @mcp.tool()
    def unipile_cancel_invitation(invitation_id: str) -> dict:
        """Annule une invitation LinkedIn ENVOYÉE (en attente). `invitation_id` =
        id d'un item `unipile_invitations(direction='sent')`."""
        return unipile_client().cancel_invitation(invitation_id)

    # ---- réseau : moi / followers / activité d'un membre ----------------

    @mcp.tool()
    def unipile_me() -> dict:
        """Profil du compte LinkedIn connecté lui-même (le « moi » sous lequel les
        autres tools unipile_* agissent)."""
        return unipile_client().get_own_profile()

    @mcp.tool()
    def unipile_followers(user_id: Optional[str] = None,
                                cursor: Optional[str] = None,
                                limit: Optional[int] = None) -> dict:
        """Followers du compte connecté (ou d'un membre via `user_id`). Paginé."""
        return unipile_client().list_followers(user_id=user_id, cursor=cursor, limit=limit)

    @mcp.tool()
    def unipile_following(user_id: Optional[str] = None,
                                cursor: Optional[str] = None,
                                limit: Optional[int] = None) -> dict:
        """Comptes suivis par le compte connecté (ou par un membre via `user_id`). Paginé."""
        return unipile_client().list_following(user_id=user_id, cursor=cursor, limit=limit)

    @mcp.tool()
    def unipile_member_comments(identifier: str, cursor: Optional[str] = None,
                                      limit: Optional[int] = None) -> dict:
        """Commentaires laissés par un membre LinkedIn (`identifier` = provider id).
        Pour repérer ce qu'un prospect engage → accroche social-selling."""
        return unipile_client().list_member_comments(identifier, cursor=cursor, limit=limit)

    @mcp.tool()
    def unipile_member_reactions(identifier: str, cursor: Optional[str] = None,
                                       limit: Optional[int] = None) -> dict:
        """Réactions d'un membre LinkedIn (`identifier` = provider id) — posts qu'il
        a likés/aimés."""
        return unipile_client().list_member_reactions(identifier, cursor=cursor, limit=limit)

    # ---- messagerie : participants / contacts / état du fil -------------

    @mcp.tool()
    def unipile_chat_attendees(chat_id: str) -> dict:
        """Participants d'un fil de messagerie LinkedIn (`chat_id` d'un unipile_chats)."""
        return unipile_client().list_chat_attendees(chat_id)

    @mcp.tool()
    def unipile_attendees(cursor: Optional[str] = None,
                                limit: Optional[int] = None) -> dict:
        """Carnet de contacts de messagerie LinkedIn (interlocuteurs). Paginé."""
        return unipile_client().list_attendees(cursor=cursor, limit=limit)

    @mcp.tool()
    def unipile_chat_update(chat_id: str, action: str,
                                  value: Optional[bool | str] = None) -> dict:
        """Modifie l'état d'un fil LinkedIn. `action` ∈ setReadStatus | setMuteStatus
        | setArchiveStatus | setPinnedStatus | setLabel | getInviteLink. `value` =
        booléen pour les statuts (ex. setReadStatus + true), string pour setLabel,
        omis pour getInviteLink."""
        return unipile_client().patch_chat(chat_id, action, value=value)

    @mcp.tool()
    def unipile_message_react(message_id: str, reaction: str,
                              chat_id: Optional[str] = None) -> dict:
        """Réagit à un message LinkedIn (DM) avec un emoji natif (ex. '👍').
        `message_id` = id d'un message de unipile_read_chat. `chat_id` (celui du
        fil) est **requis sur l'API v2**, ignoré en v1."""
        client = unipile_client()
        # Ne passe `chat_id` que s'il est fourni : garde la compat si oto-core est
        # encore à une version dont `react_message` n'a pas ce kwarg (v2-only).
        if chat_id is not None:
            return client.react_message(message_id, reaction, chat_id=chat_id)
        return client.react_message(message_id, reaction)

    # ---- LinkedIn recruiter / sales navigator ---------------------------
    # Nécessitent un abonnement Recruiter / Sales Navigator sur le compte connecté.

    @mcp.tool()
    def unipile_contracts() -> dict:
        """Contrats LinkedIn premium (Recruiter / Sales Navigator) disponibles sur le
        compte — id à passer à unipile_select_contract pour activer la bonne ardoise."""
        return unipile_client().list_contracts()

    @mcp.tool()
    def unipile_select_contract(contract_id: str) -> dict:
        """Active un contrat Recruiter / Sales Navigator (`contract_id` de
        unipile_contracts) pour les appels premium qui suivent."""
        return unipile_client().select_contract(contract_id)

    @mcp.tool()
    def unipile_inmail_balance() -> dict:
        """Solde de crédits InMail (messages premium) du compte LinkedIn connecté."""
        return unipile_client().inmail_balance()

    @mcp.tool()
    def unipile_endorse(profile_id: str, skill_endorsement_id: int) -> dict:
        """Recommande une compétence d'un membre LinkedIn.

        Args:
            profile_id: provider id du membre (commence par ACo/ADo).
            skill_endorsement_id: `endorsement_id` d'une compétence, renvoyé dans
                unipile_profile.
        """
        return unipile_client().endorse_profile(profile_id, skill_endorsement_id)

    @mcp.tool()
    def unipile_member_action(user_id: str, api: str, action: str,
                                    hiring_project_id: Optional[str] = None,
                                    stage: Optional[str] = None,
                                    list_id: Optional[str] = None) -> dict:
        """Action premium sur un membre LinkedIn (sauvegarde lead / pipeline recruteur).

        Args:
            user_id: provider id du membre.
            api: 'sales_navigator' ou 'recruiter'.
            action: sales_navigator → 'saveLead' ; recruiter → 'addCandidateToPipeline'
                | 'addApplicantToPipeline' | 'changeCandidatePipeline' | 'rejectApplicant'.
            hiring_project_id: requis pour les actions pipeline recruiter.
            stage: pipeline recruiter — 'UNCONTACTED' | 'CONTACTED' | 'REPLIED'.
            list_id: liste Sales Navigator cible (optionnel pour saveLead).
        """
        return unipile_client().member_action(
            user_id, api, action, hiring_project_id=hiring_project_id,
            stage=stage, list_id=list_id,
        )

    # ---- LinkedIn recruiter : offres d'emploi & candidats (lectures) ----

    @mcp.tool()
    def unipile_job_postings(cursor: Optional[str] = None,
                                   limit: Optional[int] = None) -> dict:
        """Offres d'emploi (job postings) du compte recruteur LinkedIn. Paginé."""
        return unipile_client().list_job_postings(cursor=cursor, limit=limit)

    @mcp.tool()
    def unipile_job_posting(job_id: str) -> dict:
        """Détail d'une offre d'emploi LinkedIn (`job_id` de unipile_job_postings)."""
        return unipile_client().get_job_posting(job_id)

    @mcp.tool()
    def unipile_job_applicants(job_id: str, cursor: Optional[str] = None,
                                     limit: Optional[int] = None) -> dict:
        """Candidats d'une offre d'emploi LinkedIn. Paginé."""
        return unipile_client().list_job_applicants(job_id, cursor=cursor, limit=limit)

    @mcp.tool()
    def unipile_job_applicant(job_id: str, applicant_id: str) -> dict:
        """Détail d'un candidat (`applicant_id` de unipile_job_applicants)."""
        return unipile_client().get_job_applicant(job_id, applicant_id)

    @mcp.tool()
    def unipile_hiring_projects(cursor: Optional[str] = None,
                                      limit: Optional[int] = None) -> dict:
        """Projets de recrutement (hiring projects) du compte Recruiter LinkedIn.
        Le `hiring_project_id` alimente unipile_member_action (pipeline). Paginé."""
        return unipile_client().list_hiring_projects(cursor=cursor, limit=limit)
