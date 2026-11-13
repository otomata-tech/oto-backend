"""Email — send a free-content message (written by the agent), per-org.

The sender address belongs to an **email connector** of the org (config keyed
per connector in `orgs.email_settings`); the **transport derives from it**
(`providers.EMAIL_CONNECTOR_TRANSPORT`):
- connector **`scaleway`** → transport `mailer`: Otomata service `mailer.oto.zone`
  (Scaleway TEM). Domain verified on the TEM side **and** in the `MAILER_FROM_DOMAINS` allowlist.
  The sending key remains Otomata's (no org key).
- connector **`resend`** → transport `resend`: BYOK, direct call to the Resend API
  with the **org's Resend key** (vault, `access.resolve_api_key("resend")`). Domain
  verified on the Resend side by the org.

**Dynamic** authorization depending on the resolved `from`:
- sending from a declared address of the org → **org member** is enough;
- **brand** fallback = the instance's sender (`OTO_MAIL_FROM`; org with no configured address, `from` omitted) →
  reserved to **super_admin** (it is the platform's brand identity).

To be distinguished from `gmail_act` (op=send), which sends from the user's Gmail mailbox
(and which drafts a DRAFT by default — sending there is explicit).

Spine: loaded explicitly in `register_all`, outside the activation gate, hidden
by default (`PROTECTED_TOOLS`/`DEFAULT_HIDDEN_TOOLS` on the visibility side).
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from fastmcp import Context, FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INTERNAL_ERROR, INVALID_PARAMS

from .. import access, config, db, email as mailer, org_store, providers, roles, scheduler, session_org
from ..auth.hooks import current_user_sub_from_token

logger = logging.getLogger(__name__)


def _cle_d_org_absente(sub: str, org_id, connecteur: str, libelle: str) -> str:
    """The refusal of a scheduled send without the org key of its transport — it says WHO sets it
    and WHERE (oto#108). It used to point to `oto_set_org_secret`, a tool removed on
    25/06/2026: the named destination no longer existed."""
    from .. import detenteurs, links
    return (f"{libelle} transport without an org key: an org administrator sets it"
            f"{links.ou_poser_la_cle(sub, org=org_id, connecteur=connecteur)} before "
            "scheduling."
            + detenteurs.phrase("Administrators of this org",
                                detenteurs.admins_de_l_org(sub, org_id)))


_CC_MAX = 10

# A cc address: `local@domain.tld`, ASCII, nothing else. A display name, a
# comma or a line break in `cc` would become extra recipients or an injected
# header at the relay; an address this pattern refuses is reported, not
# corrected.
_ADRESSE_RE = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}")

# Daily cap of RECIPIENTS (`to` + `cc`) per org on the COMMON transport — the
# instance's transport, under ITS key and ITS domain (`email.transport_plateforme`:
# its mailer relay or its own Scaleway TEM API). Alexis's decision
# of 04/10/2026: cc is allowed, but a send on the common key puts the reputation of ALL
# the instance's sends at stake (activation, reminders, summaries); an
# org that sends with ITS key (Resend, Scaleway TEM) has no platform cap.
# 200: twenty sends with ten copies, or two hundred plain sends, per day — above
# a hand-driven onboarding sequence, below a mass mailing. Adjustable
# per instance (`OTO_EMAIL_PLATFORM_DAILY_RECIPIENTS`), read on every send.
_PLAFOND_COMMUN_DEFAUT = 200


def _plafond_commun() -> int:
    """The day's cap on the common transport. An unreadable value RAISES: a
    guessed cap would be a cap nobody set."""
    raw = os.environ.get("OTO_EMAIL_PLATFORM_DAILY_RECIPIENTS")
    if raw is None:
        return _PLAFOND_COMMUN_DEFAUT
    try:
        valeur = int(raw)
    except ValueError:
        raise RuntimeError(f"OTO_EMAIL_PLATFORM_DAILY_RECIPIENTS unreadable: {raw!r} "
                           "(expected an integer >= 0).")
    if valeur < 0:
        raise RuntimeError(f"OTO_EMAIL_PLATFORM_DAILY_RECIPIENTS negative: {valeur}.")
    return valeur


def _garder_plafond_commun(sub: str, destinataires: int) -> None:
    """Refuse a send on the common transport that would exceed the day's cap of
    the calling org. Counted in the call log (`tool_calls.quantity` of the
    `email_send` calls made under `key_mode='platform'`, see `_metrer_commun`), reset
    to zero at midnight UTC: no extra table. The log is written at the end of the call —
    two simultaneous sends can cross, the cap is a reputation bound,
    not a billed count."""
    plafond = _plafond_commun()
    org = access.current_org(sub)
    deja = db.destinataires_communs_du_jour(org_id=org, sub=sub)
    if deja + destinataires <= plafond:
        return
    raise McpError(ErrorData(
        code=INVALID_PARAMS,
        message=(f"Daily cap of the common transport reached: {deja}/{plafond} "
                 f"recipient(s) today for this org, this send counts "
                 f"{destinataires} (`to` + `cc`). Try again after midnight UTC, reduce the "
                 "copies, or send from an address of an email connector of the org "
                 "(its own Resend or Scaleway TEM key: no platform cap)."),
        data={"code": "platform_email_daily_cap", "retryable": True,
              "limit": plafond, "used": deja, "units": destinataires}))


def _metrer_commun(destinataires: int) -> None:
    """Record in the call log what a send on the common transport
    consumed: this is what `_garder_plafond_commun` reads back."""
    session_org.note_call_trace(quantity=destinataires, key_mode="platform")


def _err(msg: str, code: int = INVALID_PARAMS) -> McpError:
    return McpError(ErrorData(code=code, message=msg))


def _sub_or_raise() -> str:
    # An identity failure PROPAGATES (the seam logs it with its reason, #464): only
    # a call truly without a token is "unauthenticated".
    sub = current_user_sub_from_token()
    if not sub:
        raise _err("Auth required — this tool only works over the authenticated HTTP transport.")
    return sub


def _resolve_route(from_email: Optional[str]) -> tuple[str, dict]:
    """Resolve (sub, route) and APPLY the authorization. `route` = {org_id, connector,
    from_email, from_name, transport, reply_to, quiet_hours}; from_email=None +
    org without a sender ⇒ default brand. Raises an actionable McpError otherwise.

    The TRANSPORT derives from the sender's CONNECTOR (scaleway→mailer, resend→resend)."""
    sub = _sub_or_raise()
    org = access.current_org(sub)

    # Org path: a declared address of an email connector of the active org
    if org is not None:
        match = org_store.resolve_sender(org, from_email)
        if match is not None:
            sender, connector = match
            if not roles.is_org_member(sub, org):
                raise _err("You are not a member of the active org — pass `_org=<id>` on this call.")
            transport = providers.EMAIL_CONNECTOR_TRANSPORT.get(connector)
            if transport is None:
                raise _err(f"Unknown email connector for \"{sender.get('email')}\": {connector!r}.")
            return sub, {
                "org_id": org,
                "connector": connector,
                "from_email": sender.get("email"),
                "from_name": sender.get("name"),
                "transport": transport,
                "reply_to": sender.get("reply_to"),
                "quiet_hours": org_store.org_email_quiet_hours(org, connector),
                "footer": org_store.org_email_footer(org, connector),
            }
        if from_email is not None:
            raise _err(f"\"{from_email}\" is not a declared address of an email connector of "
                       "the active org. Add it via `oto_org_settings(domain='email', op='set')`, or omit `from_email`.")

    # Brand path (the instance's sender, `OTO_MAIL_FROM`) — super_admin only
    if from_email is not None:
        raise _err("No active org with a configured sending address. Configure it "
                   "(`oto_org_settings(domain='email', op='set')`) or pass the right org (`_org=<id>`).")
    if not access.is_super_admin(sub):
        raise _err("Your org has no configured sending address — ask an org_admin "
                   "to add it via `oto_org_settings(domain='email', op='set')`. Sending under "
                   f"the platform address ({mailer._mail_from()}) is reserved to the "
                   "platform super_admin.")
    return sub, {"org_id": None, "connector": None, "from_email": None, "from_name": None,
                 "transport": "mailer", "reply_to": None, "quiet_hours": None, "footer": None}


def _cle_de_l_org(connector: Optional[str]) -> bool:
    """Does the send go out with the ORG's key? Derived from the registry, never from a
    copied list: an email connector without a `platform` tier can only send with a
    brought key (byo cascade). The brand fallback (`connector=None`, the mailer's common
    key) returns False — our footer always stays there.

    If an email connector ever gained a platform tier, this would return False
    for ALL its sends: our footer would come back, since at render time (before
    queuing) we can't know which key will be used. That is the safe side; separating it would require
    rendering the footer AFTER the key is resolved."""
    c = providers.connector_for_provider(connector) if connector else None
    return c is not None and bool(c.auth_modes) and "platform" not in c.auth_modes


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def email_send(
        ctx: Context,
        to: str,
        subject: str,
        body: str,
        markdown: bool = False,
        cc: Optional[list[str]] = None,
        from_email: Optional[str] = None,
        cta_text: Optional[str] = None,
        cta_url: Optional[str] = None,
        image_url: Optional[str] = None,
        image_alt: Optional[str] = None,
        reply_to: Optional[str] = None,
        send_at: Optional[str] = None,
        force_now: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Send a free-content email from an address of YOUR active org,
        rendered in the brand template, with an optional button and ONE header image. Can be
        SCHEDULED.

        The org declares its sender addresses (`oto_org_settings domain=email`);
        each one sends either via the Otomata mailer (domain verified on the TEM side), or
        via the org's Resend key. Typical use — onboarding sequences driven
        by the agent: read the target account's state, write a TAILORED message, send,
        then record it in the datastore so as not to follow up twice. To send
        from the user's Gmail mailbox, use `gmail_act(op="send")` — a draft written with
        `gmail(op="draft")`, by its `draft_id`, or a full message.

        Scheduled send: by default the org has a "quiet hours" window (e.g. 8pm–8am);
        if you compose inside it, the send is AUTO-shifted to the next open slot —
        you have nothing to compute. Leave `send_at` empty in that case. For a
        specific time, pass `send_at`. To force an immediate send despite the quiet
        hours, `force_now=True`. Manage/cancel the queue: `oto_scheduled_emails(op='list'|'cancel')`.

        Footer: a send with your org's key (Resend, Scaleway TEM) carries the
        PLATFORM's footer, unless the org has declared ITS unsubscribe on this
        connector — `oto_org_settings(domain='email', op='set', connector=…,
        footer={"unsubscribe_url": "https://…"} or {"unsubscribe_email": "…"})`,
        org_admin: its footer then replaces ours. Nothing removes it from this
        tool. A send under the platform address always keeps ours. The `footer`
        field of the response says which one goes out (`org` | `platform`). The
        footer's language is the brand's declared language, not your body's.

        Header image: `image_url` (https) + `image_alt` REQUIRED; the public URL
        comes from `oto_upload_url(target="image")` (an upload, reusable).

        Returns {sent, to, cc, subject, from, transport, footer} on an immediate send;
        {scheduled, id, scheduled_at, ...} if scheduled; +`html` if dry_run.

        Args:
            to: recipient's email address.
            cc: visible copy addresses (list, max 10), `name@domain.tld` with no display
                name. No hidden copy. Under the platform address, `to` +
                `cc` count toward a daily cap of recipients per org.
            subject: subject line (oto funnel voice: lowercase, formal "vous" address).
            body: plain-text body by default. Blank lines separate paragraphs;
                single line breaks are kept. HTML is escaped
                (don't inject tags). Write real, personalized content —
                never invent anything about the recipient's account.
            markdown: render `body` from markdown (bold, links, lists, headings,
                tables) — the same rendering as `gmail`. Default false: plain text,
                as before. The HTML is sanitized: raw HTML tags and images in the
                body are dropped, links keep only `http(s)`/`mailto`. The header
                image stays `image_url`.
            from_email: sender address. MUST be a declared address of the active
                org. Omitted = the org's default address (or the platform's
                address if the org has none — super_admin only).
            cta_text: label of an optional action button (e.g. "open oto").
            cta_url: URL of the button (required if `cta_text` is provided).
            image_url: public `https://` URL of ONE image at the top of the mail (480 px,
                scaled down on mobile). To publish it: `oto_upload_url(target="image")`
                → PUT the file → the receipt returns `url` (permanent, reusable).
            image_alt: replacement text, REQUIRED with `image_url` — many
                clients block images, the mail must keep its meaning without it.
            reply_to: reply address (default = the sender's, otherwise the studio's
                mailbox).
            send_at: desired send time (ISO 8601, e.g. "2026-06-24T08:00").
                Without a timezone = the org's timezone. In the past = schedules at that time.
            force_now: send right away even within the quiet hours window.
            dry_run: if true, RENDERS the HTML without sending — to proofread before sending.
        """
        to = (to or "").strip()
        subject = (subject or "").strip()
        if not to or "@" not in to:
            raise _err("`to` must be a valid email address.")
        if not subject:
            raise _err("`subject` is required.")
        if not (body or "").strip():
            raise _err("`body` is required.")
        if cta_text and not cta_url:
            raise _err("`cta_url` is required with `cta_text`.")
        # Bounded BEFORE any processing: a list of a thousand identical entries must
        # not cost a thousand validations only to end up deduplicated under the bound.
        if len(cc or []) > _CC_MAX:
            raise _err(f"`cc`: at most {_CC_MAX} addresses.")
        copies: list[str] = []
        for a in cc or []:
            a = (a or "").strip()
            if len(a) > 254 or not _ADRESSE_RE.fullmatch(a):
                raise _err(f"`cc`: invalid address {a!r} (expected `name@domain.tld`, "
                           "one address per item, no display name).")
            if a.lower() != to.lower() and a.lower() not in {c.lower() for c in copies}:
                copies.append(a)
        # The template carries the image refusals (missing alt, `http://`): we
        # trigger them BEFORE resolving the route, so that a parameter refusal
        # comes before an authorization one — like the checks just above.
        # The function is PURE: calling it again at render time costs nothing, and that is what
        # lets us keep this order while rendering, further down, with the right brand.
        try:
            mailer._image_html(image_url, image_alt)
        except ValueError as e:
            raise _err(str(e))

        sub, route = _resolve_route((from_email or "").strip() or None)
        # The brand of THE SENDER: a partner's customer whose agent writes to a
        # prospect used to sign "oto, par otomata · oto.cx" in the footer — the footer of a
        # product they never saw, under their own sending domain name.
        #
        # ⚠️ Derived from the `sub` the route has just authenticated, NEVER from one more
        # auth call: that one would raise before the parameter refusals above and
        # invert the order of this tool's errors.
        marque_expediteur = config.front_for(sub)[1]
        # The ORG's footer replaces ours on a send made with ITS key, and only there
        # (Alexis's decision of 12/09/2026, compliance): bringing one's own key
        # changed the transport and not the template, so a cold prospect read
        # "vous avez un compte oto" and was offered to unsubscribe from
        # us. On the common key, never — the condition is here, not in the route.
        pied_org = route.get("footer") if _cle_de_l_org(route["connector"]) else None
        pied = "org" if pied_org else "platform"
        html = mailer.render_composed_email(body, cta_text=cta_text, cta_url=cta_url,
                                            image_url=image_url, image_alt=image_alt,
                                            brand=marque_expediteur, org_footer=pied_org,
                                            markdown=markdown)
        org_id = route["org_id"]
        from_hdr = mailer.format_from(route["from_email"], route["from_name"]) or mailer._mail_from()
        transport = route["transport"]
        rt = reply_to or route["reply_to"]

        if dry_run:
            return {"sent": False, "dry_run": True, "to": to, "cc": copies, "subject": subject,
                    "from": from_hdr, "transport": transport, "footer": pied, "html": html}

        # Quiet hours of the sender's CONNECTOR (resolved in the route). Brand
        # fallback (org=None / no connector) → disabled (only send_at defers).
        quiet = route.get("quiet_hours") or {"start": 0, "end": 0}
        try:
            when = scheduler.compute_scheduled_at(
                datetime.now(timezone.utc), quiet, send_at, force_now)
        except ValueError:
            raise _err(f"`send_at` invalid: {send_at!r} (expected ISO 8601, e.g. "
                       "2026-06-24T08:00).")

        destinataires = 1 + len(copies)
        if transport == "mailer":
            _garder_plafond_commun(sub, destinataires)

        if when is not None:
            # Scheduled send → queued (rendered HTML + authz already frozen).
            for nom, libelle in (("resend", "Resend"), ("scaleway", "Scaleway TEM")):
                if transport == nom and not (org_id and org_store.has_org_secret(org_id, nom)):
                    raise _err(_cle_d_org_absente(sub, org_id, nom, libelle))
            sched_id = db.enqueue_scheduled_email(
                org_id=org_id, created_by=sub, to_email=to, subject=subject, body_html=html,
                from_email=route["from_email"], from_name=route["from_name"],
                reply_to=rt, transport=transport, scheduled_at=when, cc=copies)
            if transport == "mailer":
                _metrer_commun(destinataires)   # counted on the day it is scheduled
            logger.info("email_send scheduled #%d → %s at %s (transport=%s)",
                        sched_id, to, when.isoformat(), transport)
            return {"sent": False, "scheduled": True, "id": sched_id,
                    "scheduled_at": when.isoformat(), "to": to, "cc": copies, "subject": subject,
                    "from": from_hdr, "transport": transport, "footer": pied}

        # Immediate send.
        if transport == "resend":
            api_key, _key_is_platform = access.resolve_api_key("resend")  # cascade user > org; raises if absent
            ok = mailer.send_via_resend(to, subject, html, api_key=api_key,
                                        from_email=from_hdr, reply_to=rt, cc=copies)
        elif transport == "scaleway":
            f = access.resolve_credential_fields("scaleway")  # cascade → the org's key
            if not f.get("secret_key") or not f.get("project_id"):
                raise _err("Scaleway TEM connector not configured for your org: set "
                           "`secret_key` + `project_id` (key of YOUR Scaleway TEM account).")
            ok = mailer.send_via_scaleway_tem(
                to, subject, html, secret_key=f["secret_key"], project_id=f["project_id"],
                region=f.get("region") or "fr-par",
                from_email=route["from_email"], from_name=route["from_name"], reply_to=rt,
                cc=copies)
        else:
            ok = mailer.send_composed_email(
                to, subject, body, cta_text=cta_text, cta_url=cta_url, reply_to=rt,
                from_email=route["from_email"], from_name=route["from_name"],
                image_url=image_url, image_alt=image_alt, brand=marque_expediteur, cc=copies,
                markdown=markdown)

        if not ok:
            hint = ("Resend key invalid/missing" if transport == "resend"
                    else "Scaleway TEM key/project missing, or `from` domain not verified "
                         "in your Scaleway account" if transport == "scaleway"
                    else "mailer unavailable, or `from` domain outside the allowlist "
                         "`MAILER_FROM_DOMAINS` (ask a super_admin to add it)")
            raise _err(f"Send failed ({hint}). Nothing was sent.", code=INTERNAL_ERROR)
        if transport == "mailer":
            _metrer_commun(destinataires)
        logger.info("email_send → %s (cc=%d, from=%r, transport=%s)", to, len(copies),
                    from_hdr, transport)
        return {"sent": True, "dry_run": False, "to": to, "cc": copies, "subject": subject,
                "from": from_hdr, "transport": transport, "footer": pied}
