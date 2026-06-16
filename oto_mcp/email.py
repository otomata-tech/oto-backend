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


def _send(to: str, subject: str, html: str) -> bool:
    bearer = os.environ.get("OTO_MAILER_SEND_BEARER")
    if not bearer:
        return False
    try:
        import httpx
        r = httpx.post(
            _MAILER_URL,
            headers={"Authorization": f"Bearer {bearer}"},
            json={"from": _MAIL_FROM, "to": to, "subject": subject, "html": html},
            timeout=10.0,
        )
        if r.status_code == 200:
            return True
        log.warning("mailer %s → %s %s", _MAILER_URL, r.status_code, r.text[:200])
        return False
    except Exception as e:  # réseau, import, etc. → best-effort
        log.warning("email to %s not sent (%s)", to, e)
        return False


_BTN = ('display:inline-block;background:#2c2112;color:#fefcf5;text-decoration:none;'
        'padding:10px 20px;border-radius:999px;font-weight:600')
_WRAP = 'font-family:system-ui,sans-serif;max-width:480px;margin:0 auto;color:#2c2112'
_FAINT = 'color:#7a6c50;font-size:13px'


def send_invite_email(to: str, org_name: str, invite_url: str,
                      inviter: str | None = None) -> bool:
    """Email d'invitation à rejoindre une org. True si envoyé, False sinon."""
    who = f"{_esc(inviter)} " if inviter else ""
    subject = f"You're invited to {org_name} on Oto"
    html = (
        f'<div style="{_WRAP}">'
        f'<p>{who}invited you to join <strong>{_esc(org_name)}</strong> on Oto.</p>'
        f'<p><a href="{_esc(invite_url)}" style="{_BTN}">Accept invitation</a></p>'
        f'<p style="{_FAINT}">Or paste this link: {_esc(invite_url)}</p>'
        f'</div>'
    )
    return _send(to, subject, html)


def send_alpha_invite_email(to: str, invite_url: str,
                            inviter: str | None = None) -> bool:
    """Email d'invitation à l'alpha de Oto (referral). True si envoyé, False sinon."""
    who = f"{_esc(inviter)} vous invite" if inviter else "Vous êtes invité·e"
    subject = "Vous avez été invité·e à l'alpha de Oto"
    html = (
        f'<div style="{_WRAP}">'
        f'<p>{who} à l\'<strong>alpha de Oto</strong>.</p>'
        f'<p>Oto automatise la prospection B2B et le travail sur vos outils '
        f'(CRM, email, données entreprise) directement depuis Claude.</p>'
        f'<p><a href="{_esc(invite_url)}" style="{_BTN}">Activer mon accès</a></p>'
        f'<p style="{_FAINT}">Ou collez ce lien : {_esc(invite_url)}</p>'
        f'</div>'
    )
    return _send(to, subject, html)
