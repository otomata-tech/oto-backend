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


async def hosted_auth_url(sub: str, channel: str = "linkedin") -> dict:
    """Génère l'URL hosted-auth où l'user connecte SON compte (canal donné) —
    mêmes gates que la face dashboard. Renvoie `{url, channel}`."""
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
    from oto.tools.unipile import UnipileClient
    dsn = None
    if byo:  # DSN apparié à la clé BYO (chaque clé Unipile = son sous-domaine)
        try:
            dsn = access.resolve_credential(
                "unipile", want="byo", sub=sub, emit_on_failure=False).config.get("dsn")
        except McpError:
            dsn = None
    client = UnipileClient(api_key=api_key, dsn=dsn)
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
