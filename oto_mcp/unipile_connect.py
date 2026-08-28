"""Génération du lien hosted-auth Unipile — corps PARTAGÉ REST + MCP (feedback #131).

Un seul corps de logique pour les deux faces (`POST /api/unipile/connect` côté
dashboard, tool `unipile_connect_start` côté agent) : gates (canal, clé, org de
contexte, option messagerie hébergée, plafond de sièges), nonce de corrélation
(webhook `notify_url`), puis `hosted_auth_link` Unipile. Lève `ConnectRefused`
(code machine + message) — chaque face la traduit (json_error / McpError).
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import secrets

from mcp.shared.exceptions import McpError

from . import access, db
from .connectors import flow as connector_flow
from . import config

logger = logging.getLogger(__name__)

CHANNELS = ("LINKEDIN", "WHATSAPP", "TELEGRAM", "INSTAGRAM", "MESSENGER", "TWITTER")
# Produits LinkedIn premium activables à la connexion (`config.linkedin.products`,
# oto-core ≥1.30). EXCLUSIFS : un compte n'en active qu'UN (Unipile renvoie 400 sinon).
LINKEDIN_PREMIUM = ("recruiter", "sales_navigator")


class ConnectRefused(Exception):
    """Refus gaté de la génération du lien. `status` = code HTTP de référence,
    `code` = jeton machine stable, `message` = détail actionnable."""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code


def _default_limit() -> int:
    """Plafond par défaut de comptes Unipile par org (anti-dérapage coût) si l'org
    n'en définit pas un propre. 0 = pas de plafond."""
    try:
        return int(os.environ.get("OTO_MCP_UNIPILE_DEFAULT_LIMIT", "5"))
    except ValueError:
        return 5


def _return_to(app: "str | None", org_id: "int | None", suffix: str) -> str:
    """Où Unipile dépose la personne à la fin du wizard hébergé.

    Le hosted-auth sort du site : c'est la SEULE chose qui décide sur quel front
    on se réveille. Tant que c'était codé sur oto-dashboard, un utilisateur de
    Tulina finissait sa connexion chez un autre produit — et pas seulement de
    façon disgracieuse : la liaison du compte se fait par réconciliation, sous le
    JWT du front d'arrivée. Atterrir sur le mauvais front, c'est réconcilier sous
    un AUTRE sub, donc ne rien lier du tout (vécu le 2026-08-22).

    `app` inconnu ou absent ⟹ destination historique, à l'octet près. On ne fait
    JAMAIS confiance à une valeur de client au-delà d'un lookup dans la liste
    fermée `RETURN_APPS` (`resolve_return_app` s'en charge)."""
    from .auth import flow as oauth_flow
    if oauth_flow.resolve_return_app(app):
        return oauth_flow.return_url(app, suffix, org=org_id)
    return f"{config.dashboard_url()}/console/connections{suffix}"


async def hosted_auth_url(sub: str, channel: str = "linkedin",
                          force: bool = False,
                          premium: "str | None" = None,
                          app: "str | None" = None) -> dict:
    """Génère l'URL hosted-auth où l'user connecte SON compte (canal donné) —
    mêmes gates que la face dashboard. Renvoie `{url, channel}`.

    `force=True` outrepasse le garde-fou anti-doublon cross-org (issue #172) : par
    défaut, si `sub` a déjà connecté ce canal dans une AUTRE org, on refuse (le
    compte est PAR-PERSONNE et suit désormais l'utilisateur cross-org).

    `premium` (LinkedIn) = `'recruiter'` | `'sales_navigator'` : produit à ACTIVER
    au moment de la connexion. Sans lui, Unipile ne connecte que `classic` → les
    endpoints premium répondent 403 « out of your scope » et le wizard n'offre
    aucune case. Les deux sont exclusifs (un seul par compte). Demander un premium
    ajoute aussi la connexion par **cookies** au wizard (recommandé par Unipile
    pour ces produits — sans ça, seul identifiant/mot de passe est proposé).

    `app` = le front qui DEMANDE la connexion, clé d'une liste FERMÉE
    (`oauth_flow.RETURN_APPS`) — jamais une origine prise telle quelle, ce serait
    un open redirect. Il gouverne l'atterrissage de fin de wizard. Sans lui (face
    MCP, oto-dashboard), on garde à l'octet près l'ancienne destination
    `/console/connections` : c'est un chemin PROPRE au dashboard, que le patron
    générique `return_url` ne connaît pas — y retomber renverrait le dashboard sur
    `/connectors`, une régression pour l'appelant historique."""
    provider = str(channel or "linkedin").upper()
    if provider not in CHANNELS:
        raise ConnectRefused(400, "invalid_channel",
                             f"canal inconnu : {channel} (attendu : "
                             f"{', '.join(c.lower() for c in CHANNELS)})")
    if premium:
        if provider != "LINKEDIN":
            raise ConnectRefused(400, "premium_linkedin_only",
                                 f"`premium` ne vaut que pour LinkedIn (canal demandé : {channel}).")
        if premium not in LINKEDIN_PREMIUM:
            raise ConnectRefused(
                400, "invalid_premium",
                f"premium inconnu : {premium} (attendu : {', '.join(LINKEDIN_PREMIUM)}). "
                "Un compte ne peut activer qu'UN produit premium.")
    # Gate d'ACCÈS du CANAL (split du 2026-08-28). Depuis que chaque canal est un
    # connecteur, « qui peut connecter WhatsApp » se règle par canal — activation
    # d'org et ACL comprises. Le gate vit ICI, dans le corps partagé, et pas
    # seulement dans la capacité REST générique : le tool `unipile_connect_start` et
    # l'ancienne route `POST /api/unipile/connect` passent par là sans elle, et un
    # gate qu'un seul des trois chemins applique n'en est pas un.
    # Canal inconnu au registre (impossible après la garde ci-dessus, mais on ne
    # présume pas) ⟹ pas de gate supplémentaire : le fail-open est celui d'un
    # namespace inconnu, inchangé.
    from . import providers as _providers
    canal_con = _providers.connector_for_hosted_channel(provider)
    if canal_con is not None:
        try:
            access.require_connector_access(canal_con.name, sub)
        except McpError as e:
            raise ConnectRefused(403, "connector_restricted", e.error.message)
    api_key = access.unipile_api_key_for(sub)
    if not api_key:
        raise ConnectRefused(404, "unipile_not_configured",
                             "Unipile n'est pas configuré (ni clé BYO ni clé plateforme).")
    # BYO = clé propre (user/groupe/ORG) — via le seam de résolution (mode).
    byo = access.credential_mode_for(sub, "unipile") in access.BYO_MODES
    org_id = access.current_org(sub)
    if org_id is None:
        raise ConnectRefused(400, "no_org_context",
                             "Aucune org de contexte — impossible de rattacher le compte.")
    # Garde-fou anti-doublon (issue #172, piste C) : un compte de messagerie hébergé
    # est intrinsèquement PAR-PERSONNE. Si `sub` a déjà connecté CE canal dans une
    # AUTRE org (autre tenant Unipile), reconnecter créerait un 2e `account_id` pour
    # le MÊME login → les deux sessions hébergées se disputent le cookie (rotation
    # `li_at`) → dégradation silencieuse. On refuse avec un chemin actionnable :
    # l'instance personnelle suit désormais l'utilisateur cross-org (piste A), inutile
    # de reconnecter ; `force=True` pour un compte RÉELLEMENT distinct. (Reconnexion
    # dans la MÊME org = remplacement, non concernée : filtrée par `org_id`.)
    platform_seat = not byo
    # Gate OPTION (couche 3) : hébergé sans option accordée = refus.
    if not byo and not access.has_option(sub, "unipile"):
        raise ConnectRefused(402, "unipile_option_required",
                             "La messagerie hébergée n'est pas activée pour ton org "
                             "(option à accorder par un admin).")
    # Plafond de sièges hébergés (reconnexion d'un compte existant = remplacement, OK ;
    # une ADOPTION ci-dessous crée un binding dans cette org → soumise au même plafond).
    if platform_seat and db.get_unipile_account(sub, org_id, provider) is None:
        limit = db.get_org_unipile_limit(org_id)
        if limit is None:
            limit = _default_limit()
        if limit and db.count_unipile_accounts_for_org(org_id) >= limit:
            logger.info("unipile cap hit org=%s limit=%s", org_id, limit)
            raise ConnectRefused(429, "unipile_account_limit_reached",
                                 "Plafond de comptes hébergés atteint pour l'org.")
    # ADOPTION explicite (modèle binding-par-org) : le compte hébergé du sub vit déjà
    # sur la clé PLATEFORME dans une autre de ses orgs → « connecter ici » n'a pas
    # besoin du wizard, on écrit le binding pour CETTE org. Sûr : même clé partagée
    # ⟹ l'account_id est joignable ici ; même sub ⟹ zéro usurpation. `force=True`
    # (compte réellement différent) ou `premium` (reconnexion pour ATTACHER un
    # produit) → wizard quand même.
    dead_seat_account = None  # siège plateforme MORT (401) → à RECONNECTER via le wizard
    if not force and not premium and platform_seat:
        mine = db.seat_binding_elsewhere(sub, provider, exclude_org=org_id)
        if mine:
            # Ne ré-adopter QUE si la session est VIVANTE (sonde 401). Ré-adopter un
            # compte mort laisse l'user « connecté » sur un 401 (vécu Alexandra :
            # disconnect→connect ré-adoptait le cadavre au lieu d'ouvrir un login).
            # Compte mort ⟹ on tombe dans le wizard EN RECONNEXION de CE compte
            # (type=reconnect, même account_id — pas un doublon). Fail-soft : sonde
            # indisponible ⟹ on adopte (comportement d'avant).
            alive = True
            try:
                from oto.tools.unipile import make_unipile_client
                alive = make_unipile_client(api_key=api_key).account_alive(mine["account_id"])
            # noqa: SILENT — fail-soft documenté : sonde indisponible ⇒ compte tenu pour vivant
            except Exception:  # noqa: BLE001 — sonde best-effort, jamais bloquante
                alive = True
            if alive:
                db.set_unipile_account(sub, mine["account_id"],
                                       account_name=mine.get("account_name"),
                                       org_id=org_id, provider=provider, platform_seat=True)
                logger.info("unipile adopt: sub=%s account=%s org=%s (depuis org %s)",
                            sub, mine["account_id"], org_id, mine.get("org_id"))
                return {"adopted": True, "channel": provider.lower(),
                        "account_name": mine.get("account_name")}
            dead_seat_account = mine["account_id"]
            logger.info("unipile adopt SKIP compte mort: sub=%s account=%s → reconnexion wizard",
                        sub, mine["account_id"])
    # Anti-doublon BYO (issue #172) : un compte connecté sous la clé d'une AUTRE org
    # (BYO) n'est PAS adoptable ici (un account_id n'existe que sur le tenant de la
    # clé qui l'a créé) → reconnecter le même login créerait un 2e compte (rotation
    # du cookie li_at, dégradation silencieuse). Refus actionnable.
    if not force:
        byo_elsewhere = [a for a in db.list_unipile_accounts(sub)
                         if a.get("provider") == provider and a.get("org_id") != org_id
                         and not a.get("platform_seat")]
        if byo_elsewhere:
            other = byo_elsewhere[0]
            who = other.get("account_name") or other["account_id"]
            raise ConnectRefused(
                409, "unipile_already_connected_elsewhere",
                f"Tu as déjà un compte {provider.lower()} connecté (« {who} ») dans "
                "une autre de tes orgs, sous la clé Unipile de cette org-là (BYO) — "
                "il n'est pas joignable ici. Pour connecter un compte différent, "
                "relance avec force=true.")
    from oto.tools.unipile import make_unipile_client
    # DSN porté par le credential BYO gagnant (`config.dsn`) ; la plateforme reste
    # sur le défaut oto-core (api.unipile.com).
    dsn = None
    if byo:
        try:
            cfg = access.resolve_credential(
                "unipile", want="byo", sub=sub, emit_on_failure=False).config
            dsn = cfg.get("dsn")
        except McpError:
            pass
    client = make_unipile_client(api_key=api_key, dsn=dsn)
    # Activer un premium sur un compte DÉJÀ connecté = `type=reconnect` sur CE compte
    # (rattache le produit sans DOUBLON), pas un `create` (qui a produit les comptes
    # concurrents vécus). On ne reconnecte que le siège plateforme du sub (même clé
    # partagée). ⚠️ INDÉPENDANT de `force` (#237) : l'agent passe `force=true` POUR
    # dépasser le garde anti-doublon quand le compte est DÉJÀ connecté — c'est
    # justement le cas où il faut RECONNECTER (rattacher Recruiter/Sales Nav au siège
    # existant), pas créer un 2e compte. `force` ne gouverne que l'anti-doublon BYO
    # ci-dessus ; il ne doit PLUS forcer un `create` qui perd le produit premium.
    # Reconnecter (type=reconnect, PAS create) le compte existant : (1) un siège mort
    # détecté ci-dessus, ou (2) l'activation d'un premium sur un compte déjà connecté.
    reconnect_account = dead_seat_account
    if not reconnect_account and premium and platform_seat:
        existing = db.seat_binding_elsewhere(sub, provider, exclude_org=None) \
            or ({"account_id": db.get_unipile_account_id(sub, org_id, provider)}
                if db.get_unipile_account_id(sub, org_id, provider) else None)
        if existing and existing.get("account_id"):
            reconnect_account = existing["account_id"]
    public = os.environ.get("OTO_MCP_PUBLIC_URL", "https://mcp.oto.ninja").rstrip("/")
    nonce = secrets.token_urlsafe(24)
    db.create_unipile_pending(nonce, sub, org_id, provider, platform_seat=platform_seat)
    ch = provider.lower()
    try:
        url = await asyncio.to_thread(
            functools.partial(
                client.hosted_auth_link,
                name=nonce,
                providers=[provider],
                notify_url=f"{public}/api/unipile/webhook",
                success_redirect_url=_return_to(app, org_id, f"?unipile=connected&channel={ch}"),
                failure_redirect_url=_return_to(app, org_id, f"?unipile=failed&channel={ch}"),
                # produit premium demandé → `config.linkedin` (+ cookies au wizard,
                # recommandé par Unipile pour ces produits)
                premium=premium,
                allow_cookies=bool(premium),
                # rattacher le produit sur le compte existant (anti-doublon)
                reconnect_account=reconnect_account,
            )
        )
    except Exception as e:
        raise ConnectRefused(502, "unipile_link_failed", f"unipile_link_failed: {e}")
    if not url:
        raise ConnectRefused(502, "unipile_link_empty", "unipile_link_empty")
    return {"url": url, "channel": ch}


# --- Réconciliation poll-and-bind (webhook v2 non livré) ---------------------
# Le hosted-auth v2 ne rappelle pas notre `notify_url` (le webhook est configuré au
# niveau de l'APPLICATION Unipile, pas par lien) et le compte ne porte pas notre
# nonce → on ne peut pas corréler au retour du webhook. À la place : au retour de
# connexion, on LISTE les comptes Unipile et on lie au `sub` le compte le plus
# récent, NON déjà lié, du bon provider, créé APRÈS son pending (le floor évite de
# rebinder un siège pré-existant d'un tiers). Idempotent, best-effort.

def _parse_dt(v):
    """Parse une date Unipile ('2026-07-16 11:00:49.019235+00') ou un datetime PG
    en `datetime` aware (UTC par défaut). None si illisible."""
    from datetime import datetime, timezone
    import re as _re
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v).strip()
    if "T" not in s and " " in s:
        s = s.replace(" ", "T", 1)
    # normaliser un offset "+00" / "+0000" en "+00:00" (fromisoformat 3.10 strict)
    m = _re.search(r'([+-]\d{2})(\d{2})?$', s)
    if m and ":" not in s[m.start():]:
        s = s[:m.start()] + m.group(1) + ":" + (m.group(2) or "00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def reconcile_pending(sub: str) -> dict:
    """Lie le(s) compte(s) fraîchement connecté(s) par `sub` sans dépendre du
    webhook. No-op si pas de pending / pas de clé / pas de nouveau compte.
    Renvoie `{bound: bool, accounts: [{account_id, name, org_id}]}`."""
    from datetime import timedelta
    pendings = db.list_unipile_pending_for_sub(sub)
    if not pendings:
        return {"bound": False, "accounts": []}
    try:
        rc = access.resolve_credential("unipile", want="auto", sub=sub,
                                       emit_on_failure=False)
    except McpError:
        return {"bound": False, "accounts": []}
    from oto.tools.unipile import make_unipile_client
    dsn = None if rc.is_platform else rc.config.get("dsn")
    client = make_unipile_client(api_key=rc.key, dsn=dsn)
    try:
        accounts = client.list_accounts()
    except Exception:  # noqa: BLE001 — best-effort, jamais fatal pour le statut
        logger.warning("reconcile unipile: list_accounts échoué", exc_info=True)
        return {"bound": False, "accounts": []}
    taken = db.bound_unipile_account_ids()  # vivants + morts (jamais le siège d'un tiers)
    bound = []
    for pend in pendings:
        provider = (pend.get("provider") or "LINKEDIN").upper()
        floor = _parse_dt(pend.get("created_at"))
        # Rebind DÉTERMINISTE : Unipile RÉUTILISE le compte existant à la reconnexion
        # (même account_id) — une ligne soft-déconnectée du MÊME sub est la preuve de
        # propriété → on rebinde direct, sans heuristique (le floor raterait un compte
        # antérieur au pending, cas vécu 2026-07-17).
        mine_dead = db.dead_unipile_account_ids_for(sub, provider)
        cand = []
        for a in accounts:
            aid = a.get("id")
            if not aid:
                continue
            if (a.get("provider") or a.get("type") or "").upper() != provider:
                continue
            if aid in mine_dead:
                cand.append((_parse_dt(a.get("created_at")), a))
                continue  # à moi (ligne morte) → candidat sans condition de date
            if aid in taken:
                continue
            created = _parse_dt(a.get("created_at"))
            # créé après le pending (marge 5 min d'horloge) ; date illisible → on garde
            if floor is None or created is None or created >= floor - timedelta(minutes=5):
                cand.append((created, a))
        if not cand:
            continue
        from datetime import datetime, timezone
        cand.sort(key=lambda t: t[0] or datetime.min.replace(tzinfo=timezone.utc))
        # Sonde de SESSION (du plus récent au plus ancien) : ne binder qu'un compte
        # VIVANT. Un wizard avorté produit un compte `status:'running'` mais mort
        # (401 users/me) — le lier faisait taper l'agent sur une session morte pendant
        # que l'ancien compte sain restait ignoré (incident 2026-07-17).
        chosen = next((a for _, a in reversed(cand)
                       if client.account_alive(a["id"])), None)
        if chosen is None:
            logger.info("reconcile unipile: candidats tous morts (session 401) sub=%s", sub)
            continue
        db.set_unipile_account(sub, chosen["id"], account_name=chosen.get("name"),
                               org_id=pend["org_id"], provider=provider,
                               platform_seat=bool(pend.get("platform_seat")))
        db.resolve_unipile_pending(pend["nonce"])
        taken.add(chosen["id"])
        bound.append({"account_id": chosen["id"], "name": chosen.get("name"),
                      "org_id": pend["org_id"]})
        logger.info("reconcile unipile: bound sub=%s account_id=%s org=%s",
                    sub, chosen["id"], pend["org_id"])
    return {"bound": bool(bound), "accounts": bound}


# --- Le geste « connecter », sous le point de passage commun (#300) ----------

async def _start_flow(ctx, values: dict):
    """Démarre la connexion d'un canal hébergé — déclaré comme tout autre flux.

    ⚠️ **Ce flux a deux issues, et une seule est un consentement.** Le cas nominal
    rend un lien hébergé à ouvrir. Mais quand la MÊME personne a déjà connecté ce
    canal ailleurs, le compte est **adopté** — rattaché ici sans wizard — et il n'y a
    aucune page à ouvrir.

    Le contrat commun promet « démarrer une connexion ⟹ une URL à ouvrir ». Rendre
    une URL vide dans le cas adopté serait un mensonge qu'un client ouvrirait ; et
    rendre l'URL facultative rouvrirait pour TOUS un contrat fermé précisément parce
    que chaque flux y inventait sa forme. Donc : **l'adoption n'est pas un démarrage
    de flux**, c'est une résolution — deux gestes qu'une même route avait fusionnés
    parce qu'ils partagent un bouton.

    D'où un refus TYPÉ et actionnable (patron `tool_not_mounted` : un refus qui dit
    quoi faire) plutôt qu'un `FlowStart` mutilé. L'adoption ayant déjà eu lieu, il ne
    demande pas d'agir : il constate.

    L'ancienne route REST continue de servir ses deux issues telle quelle jusqu'à la
    bascule du front — ce lot ne la touche pas.
    """
    from .connectors import flow as connector_flow
    from .capabilities._types import AuthzDenied
    try:
        out = await hosted_auth_url(
            ctx.sub, str(values.get("channel") or "linkedin"),
            force=bool(values.get("force")),
            premium=(str(values["premium"]).strip().lower()
                     if values.get("premium") else None),
            # `app` voyage avec le geste (le front le pose déjà dans `params`) :
            # sans lui, la fin du wizard repart chez oto-dashboard, quel que soit
            # le front qui a demandé la connexion.
            app=(str(values["app"]) if values.get("app") else None))
    except ConnectRefused as e:
        raise AuthzDenied(e.status, e.code, e.message)
    if out.get("adopted"):
        raise AuthzDenied(
            409, "already_linked",
            f"Ce compte {out.get('channel') or 'hébergé'} était déjà connecté sous ton "
            f"identité : il vient d'être rattaché ici ({out.get('account_name') or 'compte'}). "
            "Aucun consentement à donner — relis tes identités pour le voir.")
    return connector_flow.FlowStart(auth_url=out["url"],
                                    details={"channel": out.get("channel")})

