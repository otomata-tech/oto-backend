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


_BTN = ('display:inline-block;background:#2c2112;color:#fefcf5;text-decoration:none;'
        'padding:10px 20px;border-radius:999px;font-weight:600')
_WRAP = 'font-family:system-ui,sans-serif;max-width:480px;margin:0 auto;color:#2c2112'
_FAINT = 'color:#7a6c50;font-size:13px'

# RDV d'installation avec Alexis (proposé dans les mails d'invitation / accès ouvert).
_CAL_URL = os.environ.get(
    "OTO_INSTALL_CAL_URL",
    "https://cal.com/alexis-laporte/30min?overlayCalendar=true",
)


def send_invite_email(to: str, org_name: str, invite_url: str,
                      inviter: str | None = None) -> bool:
    """Email d'invitation à rejoindre une org. True si envoyé, False sinon.

    Voix funnel : FR, vouvoiement + minuscules (alignée sur le dashboard)."""
    lead = f"{_esc(inviter)} vous invite" if inviter else "vous êtes invité·e"
    subject = f"invitation à rejoindre {org_name} sur oto"
    html = (
        f'<div style="{_WRAP}">'
        f'<p>{lead} à rejoindre <strong>{_esc(org_name)}</strong> sur oto.</p>'
        f'<p><a href="{_esc(invite_url)}" style="{_BTN}">rejoindre l\'équipe</a></p>'
        f'<p style="{_FAINT}">ou collez ce lien : {_esc(invite_url)}</p>'
        f'</div>'
    )
    return _send(to, subject, html)


def send_access_granted_email(to: str, app_url: str) -> bool:
    """Email à un compte waitlisté dont l'accès alpha vient d'être ouvert."""
    subject = "votre accès à l'alpha de oto est ouvert"
    html = (
        f'<div style="{_WRAP}">'
        f'<p>bonne nouvelle — votre accès à l\'<strong>alpha de oto</strong> est ouvert.</p>'
        f'<p><a href="{_esc(app_url)}" style="{_BTN}">ouvrir oto</a></p>'
        f'<p style="{_FAINT}">{_esc(app_url)}</p>'
        f'<p style="{_FAINT}">un coup de main pour démarrer ? '
        f'<a href="{_esc(_CAL_URL)}" style="color:#d63d0a;text-decoration:none">'
        f'réservez 30 min avec alexis →</a></p>'
        f'</div>'
    )
    return _send(to, subject, html)


def send_alpha_invite_email(to: str, invite_url: str,
                            inviter: str | None = None) -> bool:
    """Email d'invitation à l'alpha de Oto (referral). True si envoyé, False sinon.

    Copy alignée sur le positionnement oto.ninja : voix funnel = vouvoiement +
    minuscules (décision 2026-06-22, voix de marque « manuscrit chaud »)."""
    subject = "vous êtes invité·e à l'alpha de oto"
    sub_line = (
        f'<div style="font-size:15px;color:#7a6c50;padding-bottom:24px">'
        f'{_esc(inviter)} vous ouvre l\'accès.</div>'
        if inviter else '<div style="height:24px"></div>'
    )
    url = _esc(invite_url)
    html = (
        '<div style="background:#fefcf5;padding:40px 16px;'
        'font-family:-apple-system,system-ui,\'Segoe UI\',sans-serif;color:#2c2112">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:560px;margin:0 auto">'

        # En-tête : médaillon « o » + eyebrow
        '<tr><td style="padding-bottom:32px">'
        '<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
        '<td style="width:46px;height:46px;background:#2c2112;border-radius:50%;'
        'text-align:center;vertical-align:middle;color:#fefcf5;font-size:25px;'
        'font-weight:700;line-height:46px">o</td>'
        '<td style="padding-left:14px;font-size:12px;letter-spacing:.16em;'
        'text-transform:uppercase;color:#b08200;font-weight:600">alpha · sur invitation</td>'
        '</tr></table></td></tr>'

        # Titre = le hero oto.ninja
        '<tr><td style="font-size:32px;line-height:1.18;font-weight:700;'
        'padding-bottom:14px">claude connaît le monde.<br>'
        'oto lui ouvre <span style="color:#d63d0a">vos outils</span>.</td></tr>'
        f'<tr><td>{sub_line}</td></tr>'

        # Corps = hero_sub oto.ninja, VERBATIM (web/src/i18n.ts)
        '<tr><td style="font-size:16px;line-height:1.6;color:#2c2112;padding-bottom:18px">'
        'crm, emails, linkedin, données entreprise france — vos agents '
        '(claude.ai, claude code) s\'en servent avec vos clés, gardées dans un '
        'coffre côté serveur. jamais collées dans un prompt.</td></tr>'

        # 2e ligne = acc_lead oto.ninja, VERBATIM
        '<tr><td style="font-size:16px;line-height:1.6;color:#7a6c50">'
        'le même compte, le même coffre, le même catalogue — depuis claude code, '
        'claude.ai ou votre terminal.</td></tr>'

        # CTA
        '<tr><td style="padding:32px 0 8px">'
        '<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
        '<td style="background:#2c2112;border-radius:999px">'
        f'<a href="{url}" style="display:inline-block;padding:15px 34px;color:#fefcf5;'
        'text-decoration:none;font-weight:600;font-size:16px">activer mon accès →</a>'
        '</td></tr></table></td></tr>'
        f'<tr><td style="font-size:13px;color:#7a6c50;padding-bottom:24px">'
        f'ou colle ce lien : {url}</td></tr>'

        # RDV d'installation avec Alexis
        '<tr><td style="font-size:15px;line-height:1.6;color:#2c2112;'
        'padding:20px 0 32px">'
        'envie d\'un coup de main pour brancher vos outils ? '
        f'<a href="{_esc(_CAL_URL)}" style="color:#d63d0a;text-decoration:none;'
        'font-weight:600">réservez 30 min avec alexis →</a></td></tr>'

        # Footer
        '<tr><td style="border-top:1px solid #ece4d0;padding-top:20px;'
        'font-size:12px;line-height:1.5;color:#9a8a6a">'
        'oto, par otomata · oto.ninja<br>'
        'vous recevez cet email car vous avez été invité·e à l\'alpha.</td></tr>'

        '</table></div>'
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
        f'<div style="{_WRAP}">'
        f'<p style="{_FAINT}">nouveau message via otomata.tech</p>'
        f'<p><strong>{_esc(name)}</strong> &lt;{_esc(email)}&gt;</p>'
        f'<hr style="border:none;border-top:1px solid #ece4d0;margin:16px 0">'
        f'<p>{body}</p>'
        f'</div>'
    )
    return _send(to, subject, html, reply_to=email)
