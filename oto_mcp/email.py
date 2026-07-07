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


def _send(to: str, subject: str, html: str, reply_to: str | None = None,
          from_email: str | None = None) -> bool:
    """Envoi via mailer.oto.zone (Scaleway TEM). `from_email` = adresse expéditrice
    (défaut marque `_MAIL_FROM`) — le service refuse (403) un domaine hors allowlist
    `MAILER_FROM_DOMAINS`. Best-effort (False si pas de bearer ou échec)."""
    bearer = os.environ.get("OTO_MAILER_SEND_BEARER")
    if not bearer:
        return False
    try:
        import httpx
        payload = {"from": from_email or _MAIL_FROM, "to": to, "subject": subject, "html": html}
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


def send_via_resend(to: str, subject: str, html: str, *, api_key: str,
                    from_email: str, reply_to: str | None = None) -> bool:
    """Envoi direct via l'API Resend, avec la clé BYOK de l'org. `from_email` =
    adresse sur un domaine vérifié côté Resend par l'org. Best-effort (False si
    échec), même contrat que `_send`. PAS d'usage du client oto-core (interdiction
    de résolution de secret côté serveur)."""
    if not api_key or not from_email:
        return False
    try:
        import httpx
        payload = {"from": from_email, "to": [to], "subject": subject, "html": html}
        if reply_to:
            payload["reply_to"] = reply_to
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=10.0,
        )
        if r.status_code in (200, 201):
            return True
        log.warning("resend → %s %s", r.status_code, r.text[:200])
        return False
    except Exception as e:  # réseau, import, etc. → best-effort
        log.warning("resend email to %s not sent (%s)", to, e)
        return False


def send_via_scaleway_tem(to: str, subject: str, html: str, *, secret_key: str,
                          project_id: str, from_email: str, from_name: str | None = None,
                          region: str = "fr-par", reply_to: str | None = None) -> bool:
    """Envoi direct via l'API Scaleway TEM, avec la clé BYO de l'org (secret_key +
    project_id). `from_email` = adresse sur un domaine VÉRIFIÉ dans le compte Scaleway
    de l'org — l'API TEM refuse les domaines non vérifiés (propriété du domaine garantie
    par Scaleway, zéro logique domaine côté oto). Best-effort (False si échec), même
    contrat que `send_via_resend`. PAS de résolution de secret côté serveur."""
    if not secret_key or not project_id or not from_email:
        return False
    region = region or "fr-par"
    try:
        import httpx
        frm: dict = {"email": from_email}
        if from_name:
            frm["name"] = from_name
        payload: dict = {
            "from": frm,
            "to": [{"email": to}],
            "subject": subject,
            "html": html,
            "project_id": project_id,
        }
        if reply_to:
            payload["additional_headers"] = [{"key": "Reply-To", "value": reply_to}]
        r = httpx.post(
            f"https://api.scaleway.com/transactional-email/v1alpha1/regions/{region}/emails",
            headers={"X-Auth-Token": secret_key},
            json=payload,
            timeout=10.0,
        )
        if r.status_code in (200, 201):
            return True
        log.warning("scaleway tem → %s %s", r.status_code, r.text[:200])
        return False
    except Exception as e:  # réseau, import, domaine non vérifié → best-effort
        log.warning("scaleway tem email to %s not sent (%s)", to, e)
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


def send_resource_shared_email(to: str, *, type_label: str, name: str | None,
                               permission: str, app_url: str,
                               sharer: str | None = None) -> bool:
    """Email à un utilisateur avec qui on vient de PARTAGER une ressource (projet,
    datastore, doctrine). Best-effort (False si non envoyé) — un échec ne casse
    jamais le partage. Voix funnel : FR, vouvoiement + minuscules."""
    droit = "en lecture" if permission == "read" else "en écriture"
    titre = f"{type_label} « {name} »" if name else f"un {type_label}"
    who = f"{_esc(sharer)} a partagé" if sharer else "on a partagé"
    subject = (f"{name} — {type_label} partagé avec vous sur oto" if name
               else f"un {type_label} partagé avec vous sur oto")
    html = (
        f'<div style="{_WRAP}">'
        f'<p>{who} avec vous {_esc(titre)} ({droit}) sur oto.</p>'
        f'<p><a href="{_esc(app_url)}" style="{_BTN}">ouvrir dans oto</a></p>'
        f'<p style="{_FAINT}">{_esc(app_url)}</p>'
        f'</div>'
    )
    return _send(to, subject, html)


def send_resource_transferred_email(to: str, *, type_label: str, name: str | None,
                                    app_url: str, sharer: str | None = None) -> bool:
    """Email à un utilisateur à qui on vient de TRANSFÉRER la propriété d'une
    ressource (ADR 0030). Best-effort. Voix funnel : FR, vouvoiement + minuscules."""
    titre = f"{type_label} « {name} »" if name else f"un {type_label}"
    who = f"{_esc(sharer)} vous a transféré" if sharer else "on vous a transféré"
    subject = (f"{name} — {type_label} transféré à vous sur oto" if name
               else f"un {type_label} transféré à vous sur oto")
    html = (
        f'<div style="{_WRAP}">'
        f'<p>{who} la propriété de <strong>{_esc(titre)}</strong> sur oto — '
        f'vous en êtes désormais propriétaire.</p>'
        f'<p><a href="{_esc(app_url)}" style="{_BTN}">ouvrir dans oto</a></p>'
        f'<p style="{_FAINT}">{_esc(app_url)}</p>'
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
        'oto, par otomata · oto.cx<br>'
        'vous recevez cet email car vous avez été invité·e à l\'alpha.</td></tr>'

        '</table></div>'
    )
    return _send(to, subject, html)


def render_composed_email(
    body: str,
    *,
    cta_text: str | None = None,
    cta_url: str | None = None,
    footer: bool = True,
) -> str:
    """Rend le HTML à la charte « manuscrit chaud » d'un email dont le **contenu
    est fourni par l'agent** (prose brute + CTA optionnel).

    `body` = texte brut : les lignes vides séparent des paragraphes, les sauts de
    ligne simples deviennent des `<br>`. Échappé (jamais de HTML injecté par
    l'agent). `footer` ajoute la signature de marque + l'opt-out par réponse."""
    paras = [p.strip() for p in (body or "").split("\n\n") if p.strip()]
    body_html = "".join(
        f'<p style="font-size:16px;line-height:1.6;margin:0 0 16px">'
        f'{_esc(p).replace(chr(10), "<br>")}</p>'
        for p in paras
    )
    cta_html = ""
    if cta_text and cta_url:
        cta_html = (
            f'<p style="padding:8px 0"><a href="{_esc(cta_url)}" style="{_BTN}">'
            f'{_esc(cta_text)}</a></p>'
        )
    footer_html = ""
    if footer:
        footer_html = (
            '<hr style="border:none;border-top:1px solid #ece4d0;margin:24px 0 16px">'
            f'<p style="{_FAINT}">oto, par otomata · oto.cx<br>'
            'vous recevez ce message car vous avez un compte oto — '
            'répondez à cet email pour nous parler, ou pour ne plus en recevoir.</p>'
        )
    return f'<div style="{_WRAP}">{body_html}{cta_html}{footer_html}</div>'


def format_from(from_email: str | None, from_name: str | None = None) -> str | None:
    """En-tête `from` au format « Name <addr> » (ou l'adresse seule). None si pas
    d'adresse → l'appelant retombe sur la marque par défaut."""
    if not from_email:
        return None
    return f"{from_name} <{from_email}>" if from_name else from_email


def send_composed_email(
    to: str,
    subject: str,
    body: str,
    *,
    cta_text: str | None = None,
    cta_url: str | None = None,
    reply_to: str | None = None,
    footer: bool = True,
    from_email: str | None = None,
    from_name: str | None = None,
) -> bool:
    """Envoie un email à contenu libre (fourni par l'agent), rendu à la charte, via
    le mailer Otomata (Scaleway TEM).

    `from_email`/`from_name` = adresse expéditrice (défaut = marque `_MAIL_FROM`) ;
    le domaine doit être dans l'allowlist du service. `reply_to` défaut = la boîte
    du studio (`OTO_CONTACT_TO`). True si envoyé, False sinon (best-effort)."""
    html = render_composed_email(body, cta_text=cta_text, cta_url=cta_url, footer=footer)
    rt = reply_to or os.environ.get("OTO_CONTACT_TO", "alexis@otomata.tech")
    return _send(to, subject, html, reply_to=rt, from_email=format_from(from_email, from_name))


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
