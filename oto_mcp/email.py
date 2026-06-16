"""Envoi d'email transactionnel (invitations d'org) via **otomata-mailer**.

Standard Otomata : on n'utilise plus Resend per-app — l'endpoint générique
`POST mailer.oto.zone/api/send` (Scaleway TEM, brand Otomata, domaines from
vérifiés DKIM/SPF) sert les emails métier de toutes les apps. Bearer
`OTO_MAILER_SEND_BEARER`. **Best-effort** : sans bearer configuré ou en cas
d'échec, on ne lève pas — on renvoie False et l'appelant expose l'`invite_url`
pour un partage manuel.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("oto_mcp.email")

_MAILER_URL = os.environ.get("OTO_MAILER_URL", "https://mailer.oto.zone/api/send")
_MAIL_FROM = os.environ.get("OTO_MAIL_FROM", "Oto <oto@otomata.tech>")


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _send(to: str, subject: str, html: str, reply_to: str | None = None) -> bool:
    bearer = os.environ.get("OTO_MAILER_SEND_BEARER")
    if not bearer:
        return False
    try:
        import httpx
        payload = {"from": _MAIL_FROM, "to": to, "subject": subject, "html": html}
        if reply_to:
            payload["reply_to"] = reply_to
        r = httpx.post(
            _MAILER_URL,
            headers={"Authorization": f"Bearer {bearer}"},
            json=payload,
            timeout=10.0,
        )
        if r.status_code == 200:
            return True
        log.warning("mailer %s → %s %s", _MAILER_URL, r.status_code, r.text[:200])
        return False
    except Exception as e:  # réseau, import, etc. → best-effort
        log.warning("email to %s not sent (%s)", to, e)
        return False


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
    return _send(to, subject, html)


def send_contact_email(name: str, email: str, message: str) -> bool:
    """Message du formulaire de contact d'otomata.tech → boîte du studio.

    `reply_to` = l'email du visiteur pour répondre en un clic. Destinataire
    configurable via `OTO_CONTACT_TO` (défaut alexis@otomata.tech)."""
    to = os.environ.get("OTO_CONTACT_TO", "alexis@otomata.tech")
    subject = f"otomata.tech — message de {name}"
    body = _esc(message).replace("\n", "<br>")
    html = (
        f'<div style="font-family:system-ui,sans-serif;max-width:480px;margin:0 auto;color:#2c2112">'
        f'<p style="color:#7a6c50;font-size:13px">nouveau message via otomata.tech</p>'
        f'<p><strong>{_esc(name)}</strong> &lt;{_esc(email)}&gt;</p>'
        f'<hr style="border:none;border-top:1px solid #ece4d0;margin:16px 0">'
        f'<p>{body}</p>'
        f'</div>'
    )
    return _send(to, subject, html, reply_to=email)
