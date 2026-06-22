"""Email — envoi d'un message à contenu libre (rédigé par l'agent) via le mailer
Otomata.

Brique d'**onboarding piloté par l'agent** (≠ scheduler/SaaS) : pour des séquences
d'accueil à petit volume, l'agent lit l'état du compte cible, rédige un message
adapté au cas, l'envoie ici, et trace l'envoi dans le datastore (`data_*`). La
doctrine d'org encode le playbook (qui relancer, sur quel signal, quoi dire).

Le message part de l'identité de **marque** `oto@otomata.tech` (mailer
`mailer.oto.zone`, DKIM/SPF vérifiés) — rendu à la charte « manuscrit chaud ». À
distinguer de `gmail_compose`, qui envoie depuis la boîte Gmail de l'utilisateur.

Spine : chargé explicitement dans `register_all`, hors gate d'activation.
**Gaté super_admin** dans le handler (envoyer sous la marque = propriétaire de la
plateforme) et masqué par défaut (pas de bruit dans la toolbox des autres comptes).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INTERNAL_ERROR, INVALID_PARAMS

from .. import access, email as mailer
from ..auth_hooks import current_user_sub_from_token

logger = logging.getLogger(__name__)


def _require_super_admin() -> str:
    """Sub du super_admin courant, ou McpError. Envoyer sous l'identité de marque
    est réservé au propriétaire de la plateforme."""
    sub = None
    try:
        sub = current_user_sub_from_token()
    except Exception:
        pass
    if not sub:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="Auth requise — ce tool ne marche que sur le transport HTTP authentifié.",
        ))
    if not access.is_super_admin(sub):
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="Réservé au super_admin : `email_send` part de l'identité de marque "
                    "oto@otomata.tech. Pour envoyer depuis ta boîte, utilise gmail_compose.",
        ))
    return sub


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def email_send(
        ctx: Context,
        to: str,
        subject: str,
        body: str,
        cta_text: Optional[str] = None,
        cta_url: Optional[str] = None,
        reply_to: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict:
        """Envoie un email à contenu libre depuis la marque Otomata (oto@otomata.tech),
        rendu à la charte. RÉSERVÉ au super_admin de la plateforme.

        Usage type — séquences d'onboarding pilotées par l'agent : lis l'état du
        compte cible, rédige un message ADAPTÉ au cas (pas un template générique),
        envoie-le, puis trace l'envoi dans le datastore pour ne pas relancer en
        double. Pour envoyer depuis la boîte Gmail de l'utilisateur, c'est
        `gmail_compose` — pas ce tool.

        Args:
            to: adresse email du destinataire.
            subject: objet (voix funnel oto : minuscules, vouvoiement).
            body: corps du message en texte brut. Les lignes vides séparent les
                paragraphes ; les sauts de ligne simples sont conservés. Le HTML
                est échappé (n'injecte pas de balises). Écris le contenu réel,
                personnalisé — jamais d'invention sur le compte du destinataire.
            cta_text: libellé d'un bouton d'action optionnel (ex. « ouvrir oto »).
            cta_url: URL du bouton (requis si `cta_text` est fourni).
            reply_to: adresse de réponse (défaut = la boîte du studio).
            dry_run: si vrai, REND le HTML sans envoyer — utilise-le pour faire
                relire le message avant l'envoi réel.

        Renvoie {sent, to, subject, dry_run} (+ `html` rendu si `dry_run`).
        """
        _require_super_admin()
        to = (to or "").strip()
        subject = (subject or "").strip()
        if not to or "@" not in to:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="`to` doit être une adresse email valide."))
        if not subject:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="`subject` est requis."))
        if not (body or "").strip():
            raise McpError(ErrorData(code=INVALID_PARAMS, message="`body` est requis."))
        if cta_text and not cta_url:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="`cta_url` est requis avec `cta_text`."))

        if dry_run:
            html = mailer.render_composed_email(body, cta_text=cta_text, cta_url=cta_url)
            return {"sent": False, "dry_run": True, "to": to, "subject": subject, "html": html}

        ok = mailer.send_composed_email(
            to, subject, body, cta_text=cta_text, cta_url=cta_url, reply_to=reply_to)
        if not ok:
            raise McpError(ErrorData(
                code=INTERNAL_ERROR,
                message="Envoi échoué (mailer indisponible ou bearer non configuré). "
                        "Rien n'a été envoyé.",
            ))
        logger.info("email_send → %s (subject=%r)", to, subject)
        return {"sent": True, "dry_run": False, "to": to, "subject": subject}
