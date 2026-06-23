"""Email — envoi d'un message à contenu libre (rédigé par l'agent), per-org.

Deux chemins d'envoi, choisis selon l'**adresse expéditrice** déclarée par l'org
active (`orgs.email_settings.senders`, chaque sender porte un `transport`) :
- **`mailer`** — via le service Otomata `mailer.oto.zone` (Scaleway TEM). Le domaine
  doit être vérifié côté TEM **et** dans l'allowlist `MAILER_FROM_DOMAINS` du service.
  La clé d'envoi reste celle d'Otomata.
- **`resend`** — BYOK : appel direct de l'API Resend avec la **clé Resend de l'org**
  (coffre, résolue par `access.resolve_api_key("resend")`, cascade user > org). Le
  domaine est vérifié côté Resend par l'org.

Autorisation **dynamique** selon le `from` résolu :
- envoi depuis une adresse déclarée de l'org → **membre de l'org** suffit ;
- repli **marque** `oto@otomata.tech` (org sans adresse configurée, `from` omis) →
  réservé **super_admin** (c'est l'identité de marque de la plateforme).

À distinguer de `gmail_compose`, qui envoie depuis la boîte Gmail de l'utilisateur.

Spine : chargé explicitement dans `register_all`, hors gate d'activation, masqué
par défaut (`PROTECTED_TOOLS`/`DEFAULT_HIDDEN_TOOLS` côté visibilité).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INTERNAL_ERROR, INVALID_PARAMS

from .. import access, db, email as mailer, org_store, roles, scheduler
from ..auth_hooks import current_user_sub_from_token

logger = logging.getLogger(__name__)

_TRANSPORTS = ("mailer", "resend")


def _err(msg: str, code: int = INVALID_PARAMS) -> McpError:
    return McpError(ErrorData(code=code, message=msg))


def _sub_or_raise() -> str:
    sub = None
    try:
        sub = current_user_sub_from_token()
    except Exception:
        pass
    if not sub:
        raise _err("Auth requise — ce tool ne marche que sur le transport HTTP authentifié.")
    return sub


def _resolve_route(from_email: Optional[str]) -> tuple[str, dict]:
    """Résout (sub, route) et APPLIQUE l'autorisation. `route` =
    {from_email, from_name, transport, reply_to} ; from_email=None ⇒ marque par
    défaut. Lève McpError actionnable sinon."""
    sub = _sub_or_raise()
    org = access.current_org(sub)

    # Chemin org : une adresse déclarée de l'org active
    if org is not None:
        sender = org_store.resolve_sender(org, from_email)
        if sender is not None:
            if not roles.is_org_member(sub, org):
                raise _err("Tu n'es pas membre de l'org active — bascule avec `oto_use_org`.")
            transport = (sender.get("transport") or "mailer").lower()
            if transport not in _TRANSPORTS:
                raise _err(f"Transport invalide pour « {sender.get('email')} » : "
                           f"{transport!r} (attendu {list(_TRANSPORTS)}).")
            return sub, {
                "org_id": org,
                "from_email": sender.get("email"),
                "from_name": sender.get("name"),
                "transport": transport,
                "reply_to": sender.get("reply_to"),
            }
        if from_email is not None:
            raise _err(f"« {from_email} » n'est pas une adresse déclarée de l'org active. "
                       "Ajoute-la via `oto_set_org_email_settings`, ou omets `from_email`.")

    # Chemin marque oto@otomata.tech — super_admin uniquement
    if from_email is not None:
        raise _err("Aucune org active avec une adresse d'envoi configurée. Configure-la "
                   "(`oto_set_org_email_settings`) ou bascule sur la bonne org (`oto_use_org`).")
    if not access.is_super_admin(sub):
        raise _err("Ton org n'a pas d'adresse d'envoi configurée — demande à un org_admin "
                   "de l'ajouter via `oto_set_org_email_settings`. L'envoi sous la marque "
                   "oto@otomata.tech est réservé au super_admin de la plateforme.")
    return sub, {"org_id": None, "from_email": None, "from_name": None,
                 "transport": "mailer", "reply_to": None}


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def email_send(
        ctx: Context,
        to: str,
        subject: str,
        body: str,
        from_email: Optional[str] = None,
        cta_text: Optional[str] = None,
        cta_url: Optional[str] = None,
        reply_to: Optional[str] = None,
        send_at: Optional[str] = None,
        force_now: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Envoie un email à contenu libre depuis une adresse de TON org active,
        rendu à la charte. Peut être DIFFÉRÉ.

        L'org déclare ses adresses expéditrices (`oto_set_org_email_settings`) ;
        chacune envoie soit via le mailer Otomata (domaine vérifié côté TEM), soit
        via la clé Resend de l'org. Usage type — séquences d'onboarding pilotées
        par l'agent : lis l'état du compte cible, rédige un message ADAPTÉ, envoie,
        puis trace dans le datastore pour ne pas relancer en double. Pour envoyer
        depuis la boîte Gmail de l'utilisateur, c'est `gmail_compose`.

        Envoi différé : par défaut l'org a une fenêtre « quiet hours » (ex. 20h–8h) ;
        si tu composes dedans, l'envoi est AUTO-décalé au prochain créneau ouvert —
        tu n'as rien à calculer. Laisse `send_at` vide dans ce cas. Pour une heure
        précise, passe `send_at`. Pour forcer un envoi immédiat malgré les quiet
        hours, `force_now=True`. Gère/annule la file : `oto_list_scheduled_emails`,
        `oto_cancel_scheduled_email`.

        Args:
            to: adresse email du destinataire.
            subject: objet (voix funnel oto : minuscules, vouvoiement).
            body: corps en texte brut. Les lignes vides séparent les paragraphes ;
                les sauts de ligne simples sont conservés. Le HTML est échappé
                (n'injecte pas de balises). Écris du contenu réel, personnalisé —
                jamais d'invention sur le compte du destinataire.
            from_email: adresse expéditrice. DOIT être une adresse déclarée de l'org
                active. Omise = l'adresse par défaut de l'org (ou la marque
                oto@otomata.tech si l'org n'en a aucune — super_admin uniquement).
            cta_text: libellé d'un bouton d'action optionnel (ex. « ouvrir oto »).
            cta_url: URL du bouton (requis si `cta_text` est fourni).
            reply_to: adresse de réponse (défaut = celle du sender, sinon la boîte
                du studio).
            send_at: heure d'envoi souhaitée (ISO 8601, ex. "2026-06-24T08:00").
                Sans fuseau = fuseau de l'org. Passée = programme à cette heure.
            force_now: envoie tout de suite même dans la fenêtre quiet hours.
            dry_run: si vrai, REND le HTML sans envoyer — pour relire avant l'envoi.

        Renvoie {sent, to, subject, from, transport} en envoi immédiat ;
        {scheduled, id, scheduled_at, ...} si différé ; +`html` si dry_run.
        """
        to = (to or "").strip()
        subject = (subject or "").strip()
        if not to or "@" not in to:
            raise _err("`to` doit être une adresse email valide.")
        if not subject:
            raise _err("`subject` est requis.")
        if not (body or "").strip():
            raise _err("`body` est requis.")
        if cta_text and not cta_url:
            raise _err("`cta_url` est requis avec `cta_text`.")

        sub, route = _resolve_route((from_email or "").strip() or None)
        org_id = route["org_id"]
        from_hdr = mailer.format_from(route["from_email"], route["from_name"]) or mailer._MAIL_FROM
        transport = route["transport"]
        rt = reply_to or route["reply_to"]
        html = mailer.render_composed_email(body, cta_text=cta_text, cta_url=cta_url)

        if dry_run:
            return {"sent": False, "dry_run": True, "to": to, "subject": subject,
                    "from": from_hdr, "transport": transport, "html": html}

        # Quiet hours : appliquées seulement pour une org (le repli marque, org=None,
        # n'a pas de quiet hours — seul un send_at explicite le diffère).
        if org_id is not None:
            quiet = org_store.get_org_email_settings(org_id).get("quiet_hours")
        else:
            quiet = {"start": 0, "end": 0}   # désactivé (start==end)
        try:
            when = scheduler.compute_scheduled_at(
                datetime.now(timezone.utc), quiet, send_at, force_now)
        except ValueError:
            raise _err(f"`send_at` invalide : {send_at!r} (attendu ISO 8601, ex. "
                       "2026-06-24T08:00).")

        if when is not None:
            # Envoi différé → mise en file (HTML rendu + autz déjà figés).
            if transport == "resend" and not (org_id and org_store.has_org_secret(org_id, "resend")):
                raise _err("Transport Resend sans clé d'org : pose-la via "
                           "`oto_set_org_secret(provider=\"resend\")` avant de programmer.")
            sched_id = db.enqueue_scheduled_email(
                org_id=org_id, created_by=sub, to_email=to, subject=subject, body_html=html,
                from_email=route["from_email"], from_name=route["from_name"],
                reply_to=rt, transport=transport, scheduled_at=when)
            logger.info("email_send différé #%d → %s à %s (transport=%s)",
                        sched_id, to, when.isoformat(), transport)
            return {"sent": False, "scheduled": True, "id": sched_id,
                    "scheduled_at": when.isoformat(), "to": to, "subject": subject,
                    "from": from_hdr, "transport": transport}

        # Envoi immédiat.
        if transport == "resend":
            api_key, _key_is_platform = access.resolve_api_key("resend")  # cascade user > org ; lève si absente
            ok = mailer.send_via_resend(to, subject, html, api_key=api_key,
                                        from_email=from_hdr, reply_to=rt)
        else:
            ok = mailer.send_composed_email(
                to, subject, body, cta_text=cta_text, cta_url=cta_url, reply_to=rt,
                from_email=route["from_email"], from_name=route["from_name"])

        if not ok:
            hint = ("clé Resend invalide/absente" if transport == "resend"
                    else "mailer indisponible, ou domaine du `from` hors allowlist "
                         "`MAILER_FROM_DOMAINS` (demande l'ajout à un super_admin)")
            raise _err(f"Envoi échoué ({hint}). Rien n'a été envoyé.", code=INTERNAL_ERROR)
        logger.info("email_send → %s (from=%r, transport=%s)", to, from_hdr, transport)
        return {"sent": True, "dry_run": False, "to": to, "subject": subject,
                "from": from_hdr, "transport": transport}
