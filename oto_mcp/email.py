"""Envoi d'email transactionnel (invitations d'org) via Resend.

Fine façade sur `oto.tools.resend` (oto-core). **Best-effort** : si Resend n'est
pas configuré (pas de `RESEND_API_KEY` dans l'env du process) ou si l'envoi
échoue, on ne lève pas — on renvoie False et l'appelant expose l'`invite_url`
pour un partage manuel. Aucun secret hors env de process (OTO_CONFIG_DISABLE_SOPS).
"""
from __future__ import annotations

import logging

log = logging.getLogger("oto_mcp.email")


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_invite_email(to: str, org_name: str, invite_url: str,
                      inviter: str | None = None) -> bool:
    """Email d'invitation à rejoindre une org. True si envoyé, False sinon."""
    who = f"{_esc(inviter)} " if inviter else ""
    subject = f"You're invited to {org_name} on Oto"
    html = (
        f'<div style="font-family:system-ui,sans-serif;max-width:480px;margin:0 auto;color:#2c2112">'
        f'<p>{who}invited you to join <strong>{_esc(org_name)}</strong> on Oto.</p>'
        f'<p><a href="{_esc(invite_url)}" '
        f'style="display:inline-block;background:#2c2112;color:#fefcf5;text-decoration:none;'
        f'padding:10px 20px;border-radius:999px;font-weight:600">Accept invitation</a></p>'
        f'<p style="color:#7a6c50;font-size:13px">Or paste this link: {_esc(invite_url)}</p>'
        f'</div>'
    )
    try:
        from oto.tools.resend import send_email
        send_email(to=to, subject=subject, html=html)
        return True
    except Exception as e:  # pas de clé, package absent, erreur API → best-effort
        log.warning("invite email to %s not sent (%s)", to, e)
        return False
