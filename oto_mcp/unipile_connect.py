"""Génération du lien hosted-auth Unipile — corps PARTAGÉ REST + MCP (feedback #131).

Un seul corps de logique pour les deux faces (`POST /api/unipile/connect` côté
dashboard, tool `unipile_connect_start` côté agent) : gates (canal, clé, org de
contexte, option messagerie hébergée, plafond de sièges), nonce de corrélation
(webhook `notify_url`), puis `hosted_auth_link` Unipile. Lève `ConnectRefused`
(code machine + message) — chaque face la traduit (json_error / McpError).
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets

from mcp.shared.exceptions import McpError

from . import access, db

logger = logging.getLogger(__name__)

CHANNELS = ("LINKEDIN", "WHATSAPP", "TELEGRAM", "INSTAGRAM", "MESSENGER", "TWITTER")


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


async def hosted_auth_url(sub: str, channel: str = "linkedin",
                          force: bool = False) -> dict:
    """Génère l'URL hosted-auth où l'user connecte SON compte (canal donné) —
    mêmes gates que la face dashboard. Renvoie `{url, channel}`.

    `force=True` outrepasse le garde-fou anti-doublon cross-org (issue #172) : par
    défaut, si `sub` a déjà connecté ce canal dans une AUTRE org, on refuse (le
    compte est PAR-PERSONNE et suit désormais l'utilisateur cross-org)."""
    provider = str(channel or "linkedin").upper()
    if provider not in CHANNELS:
        raise ConnectRefused(400, "invalid_channel",
                             f"canal inconnu : {channel} (attendu : "
                             f"{', '.join(c.lower() for c in CHANNELS)})")
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
    if not force:
        elsewhere = [a for a in db.list_unipile_accounts(sub)
                     if a.get("provider") == provider and a.get("org_id") != org_id]
        if elsewhere:
            other = elsewhere[0]
            who = other.get("account_name") or other["account_id"]
            raise ConnectRefused(
                409, "unipile_already_connected_elsewhere",
                f"Tu as déjà un compte {provider.lower()} connecté (« {who} ») dans "
                "une autre de tes orgs. Il te suit désormais dans toutes tes orgs — "
                "inutile de reconnecter, utilise-le directement. Pour connecter un "
                "compte RÉELLEMENT différent, relance avec force=true.")
    platform_seat = not byo
    # Gate OPTION (couche 3) : hébergé sans option accordée = refus.
    if not byo and not access.has_option(sub, "unipile"):
        raise ConnectRefused(402, "unipile_option_required",
                             "La messagerie hébergée n'est pas activée pour ton org "
                             "(option à accorder par un admin).")
    # Plafond de sièges hébergés (reconnexion d'un compte existant = remplacement, OK).
    if platform_seat and db.get_unipile_account(sub, org_id, provider) is None:
        limit = db.get_org_unipile_limit(org_id)
        if limit is None:
            limit = _default_limit()
        if limit and db.count_unipile_accounts_for_org(org_id) >= limit:
            logger.info("unipile cap hit org=%s limit=%s", org_id, limit)
            raise ConnectRefused(429, "unipile_account_limit_reached",
                                 "Plafond de comptes hébergés atteint pour l'org.")
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
    public = os.environ.get("OTO_MCP_PUBLIC_URL", "https://mcp.oto.ninja").rstrip("/")
    dash = os.environ.get("OTO_DASHBOARD_URL", "https://dashboard.oto.ninja").rstrip("/")
    nonce = secrets.token_urlsafe(24)
    db.create_unipile_pending(nonce, sub, org_id, provider, platform_seat=platform_seat)
    ch = provider.lower()
    try:
        url = await asyncio.to_thread(
            client.hosted_auth_link,
            name=nonce,
            providers=[provider],
            notify_url=f"{public}/api/unipile/webhook",
            success_redirect_url=f"{dash}/console/connections?unipile=connected&channel={ch}",
            failure_redirect_url=f"{dash}/console/connections?unipile=failed&channel={ch}",
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
    taken = db.bound_unipile_account_ids()
    bound = []
    for pend in pendings:
        provider = (pend.get("provider") or "LINKEDIN").upper()
        floor = _parse_dt(pend.get("created_at"))
        cand = []
        for a in accounts:
            aid = a.get("id")
            if not aid or aid in taken:
                continue
            if (a.get("provider") or a.get("type") or "").upper() != provider:
                continue
            created = _parse_dt(a.get("created_at"))
            # créé après le pending (marge 5 min d'horloge) ; date illisible → on garde
            if floor is None or created is None or created >= floor - timedelta(minutes=5):
                cand.append((created, a))
        if not cand:
            continue
        from datetime import datetime, timezone
        cand.sort(key=lambda t: t[0] or datetime.min.replace(tzinfo=timezone.utc))
        chosen = cand[-1][1]
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
