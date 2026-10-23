"""Generation of the Unipile hosted-auth link — SHARED body for REST + MCP (feedback #131).

A single body of logic for both faces (`POST /api/unipile/connect` on the
dashboard side, the `unipile_connect_start` tool on the agent side): gates (channel, key,
context org, hosted-messaging option), pending row set (nonce = `name`
of the link = reconciliation key, THE binding path since #581), then Unipile's
`hosted_auth_link`. Raises `ConnectRefused` (machine code + message) — each
face translates it (json_error / McpError).
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import secrets

from .mcp_errors import McpError
from . import access, db, unipile_binding
from .access import CredentialUnavailable
from . import config

logger = logging.getLogger(__name__)

# Neither X (TWITTER) nor Messenger (MESSENGER): the Unipile v2 API does not serve them (2026-09-15).
CHANNELS = ("LINKEDIN", "WHATSAPP", "TELEGRAM", "INSTAGRAM")
# Premium LinkedIn products that can be activated at connection time (`config.linkedin.products`,
# oto-core ≥1.30). EXCLUSIVE: an account activates only ONE (Unipile otherwise returns 400).
LINKEDIN_PREMIUM = ("recruiter", "sales_navigator")




class ConnectRefused(Exception):
    """Gated refusal to generate the link. `status` = reference HTTP code,
    `code` = stable machine token, `message` = actionable detail."""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code


def _default_limit() -> int:
    """Default cap on Unipile accounts per org (cost runaway guard) if the org
    does not define its own. 0 = no cap."""
    try:
        return int(os.environ.get("OTO_MCP_UNIPILE_DEFAULT_LIMIT", "5"))
    except ValueError:
        return 5


def plafond_de_comptes(org_id: int) -> int:
    """The org's cap on hosted accounts that the connection applies: none, `0`.

    Orgs are not capped since 2026-10-23: the connection no longer
    counts an org's hosted accounts, whatever `orgs.unipile_account_limit` or
    `OTO_MCP_UNIPILE_DEFAULT_LIMIT` say. Kept so the readers of the cap (the
    commerce read, the admin view) report the one actually applied."""
    return 0


def connections_page(sub: "str | None", org_id: "int | None") -> "str | None":
    """The page where THIS account connects its hosted messaging — on ITS product.

    Primary-tenant account ⟹ the dashboard's `/console/connections`, byte for byte.
    THIRD-party tenant account ⟹ the `connectors` pattern its tenant declares
    (`links.link_for`), or `None` if it declares none: never OUR path under its
    domain, nor our domain at all — that is a product it does not have.

    ⚠️ Lived on 2026-09-03 then 2026-09-14 (a client of a hosted tenant): the
    "connect your account" refusal hardcoded `https://manage.oto.cx/console/connections`,
    and the end of the MCP side's wizard also sent people there. The person, who has
    no account with us, CREATED one (another sub) to get past the login
    screen — and the reconciliation that followed ran under that sub, with no pending:
    nothing was linked (signal #689)."""
    if sub and config.tenant_slug_for(sub):
        from . import links
        return links.link_for("connectors", sub=sub, org=org_id)
    return f"{config.dashboard_url()}/console/connections"


def _return_to(app: "str | None", org_id: "int | None", suffix: str,
               sub: "str | None" = None) -> str:
    """Where Unipile drops the person at the end of the hosted wizard.

    The hosted-auth leaves the site: this is the ONLY thing that decides which front
    we wake up on. As long as it was hardcoded to oto-dashboard, a user of a
    third-party tenant ended their connection on another product — and not merely
    awkwardly: the account is linked by reconciliation, under the
    JWT of the arrival front. Landing on the wrong front means reconciling under
    ANOTHER sub, hence linking nothing at all (lived on 2026-08-22).

    Known `app` ⟹ the front that asked. We NEVER trust a client-supplied value
    beyond a lookup in the closed list `RETURN_APPS`
    (`resolve_return_app` takes care of it).

    Unknown or missing `app` (MCP side: an agent has no front) ⟹ the
    connections page of the ACCOUNT'S PRODUCT (`connections_page`), derived from the sub — hence from the
    token, never from a client value. Primary tenant: historical destination,
    byte for byte. Third-party tenant without a `connectors` pattern: we fall back on ours,
    because a redirect must land somewhere (cf. `links.redirect_for`)."""
    from .auth import flow as oauth_flow
    if oauth_flow.resolve_return_app(app):
        return oauth_flow.return_url(app, suffix, org=org_id)
    page = connections_page(sub, org_id)
    if page:
        if "?" in page and suffix.startswith("?"):
            return f"{page}&{suffix[1:]}"
        return f"{page}{suffix}"
    return f"{config.dashboard_url()}/console/connections{suffix}"


async def hosted_auth_url(sub: str, channel: str = "linkedin",
                          force: bool = False,
                          premium: "str | None" = None,
                          app: "str | None" = None) -> dict:
    """Generates the hosted-auth URL where the user connects THEIR account (given channel) —
    same gates as the dashboard side. Returns `{url, channel}`.

    `force=True` bypasses the cross-org anti-duplicate guard (issue #172): by
    default, if `sub` has already connected this channel in ANOTHER org, we refuse (the
    account is PER-PERSON and now follows the user cross-org).

    `premium` (LinkedIn) = `'recruiter'` | `'sales_navigator'`: product to ACTIVATE
    at connection time. Without it, Unipile only connects `classic` → the premium
    endpoints answer 403 "out of your scope" and the wizard offers
    no checkbox. The two are exclusive (only one per account). Requesting a premium
    also adds **cookie** connection to the wizard (recommended by Unipile
    for these products — without it, only username/password is offered).

    `app` = the front that REQUESTS the connection, key of a CLOSED list
    (`oauth_flow.RETURN_APPS`) — never an origin taken as is, that would be
    an open redirect. It governs the end-of-wizard landing. Without it (MCP
    side, oto-dashboard), we keep the old destination byte for byte
    `/console/connections`: it is a path SPECIFIC to the dashboard, which the generic
    `return_url` pattern does not know — falling back there would send the dashboard to
    `/connectors`, a regression for the historical caller."""
    provider = str(channel or "linkedin").upper()
    if provider not in CHANNELS:
        raise ConnectRefused(400, "invalid_channel",
                             f"unknown channel: {channel} (expected: "
                             f"{', '.join(c.lower() for c in CHANNELS)})")
    if premium:
        if provider != "LINKEDIN":
            raise ConnectRefused(400, "premium_linkedin_only",
                                 f"`premium` only applies to LinkedIn (requested channel: {channel}).")
        if premium not in LINKEDIN_PREMIUM:
            raise ConnectRefused(
                400, "invalid_premium",
                f"unknown premium: {premium} (expected: {', '.join(LINKEDIN_PREMIUM)}). "
                "An account can only activate ONE premium product.")
    # CHANNEL ACCESS gate (split of 2026-08-28). Since each channel is a
    # connector, "who can connect WhatsApp" is set per channel — including org
    # activation. The gate lives HERE, in the shared body, and not
    # only in the generic REST capability: the `unipile_connect_start` tool and
    # the old `POST /api/unipile/connect` route go through here without it, and a
    # gate that only one of the three paths applies is not one.
    # Channel unknown to the registry (impossible after the guard above, but we do not
    # presume) ⟹ no additional gate: the fail-open is that of an unknown
    # namespace, unchanged.
    from . import providers as _providers
    canal_con = _providers.connector_for_hosted_channel(provider)
    # A channel CUT for the caller (`connector_availability`) opens no wizard — the
    # three paths (generic flow, `unipile_connect_start`, the old REST route) pass
    # here, and the `unipile_connect_start` tool is gated by the call guard on
    # `unipile`, not on the channel it is asked to connect.
    if canal_con is not None:
        from .connectors import activation_gate
        try:
            await asyncio.to_thread(activation_gate.exiger_connectable, canal_con.name, sub)
        except activation_gate.ConnecteurCoupe as e:
            raise ConnectRefused(403, e.code, e.message) from None
    # The channel carries its rights; the resolver follows its delegation to the unipile
    # key. Key, mode and DSN come from THE SAME instance, account included.
    try:
        credential = await asyncio.to_thread(
            access.resolve_credential, canal_con.name if canal_con else "unipile",
            sub=sub, check_usage=False, emit_on_failure=False)
    except CredentialUnavailable:
        raise ConnectRefused(404, "unipile_not_configured",
                             "Unipile is not configured (neither a BYO key nor a platform key).")
    except McpError as e:
        raise ConnectRefused(400, "credential_resolution_failed", e.error.message) from e
    api_key = credential.key
    byo = credential.mode in access.BYO_MODES
    org_id = access.current_org(sub)
    if org_id is None:
        raise ConnectRefused(400, "no_org_context",
                             "No context org — cannot attach the account.")
    # Anti-duplicate guard (issue #172, track C): a hosted messaging account
    # is inherently PER-PERSON. If `sub` has already connected THIS channel in
    # ANOTHER org (another Unipile tenant), reconnecting would create a 2nd `account_id` for
    # the SAME login → the two hosted sessions fight over the cookie (`li_at`
    # rotation) → silent degradation. We refuse with an actionable path:
    # the personal instance now follows the user cross-org (track A), no need
    # to reconnect; `force=True` for a REALLY distinct account. (Reconnection
    # in the SAME org = replacement, not concerned: filtered by `org_id`.)
    platform_seat = not byo
    # OPTION gate (layer 3): hosted without the declared right = refusal. The right is
    # the org's OR a row set on the person, in the org or everywhere
    # (ADR 0070 §7); the account mark (`option_comps`) does not open it.
    if not byo and not await asyncio.to_thread(access.has_right, sub, org_id, "unipile"):
        from . import detenteurs  # lazy, like the other tiers at the call site
        raise ConnectRefused(402, "unipile_option_required",
                             "Hosted messaging is active neither for this org nor "
                             "for you: trial ended or subscription required."
                             + await asyncio.to_thread(detenteurs.qui_leve_une_option,
                                                       sub, org_id))
    # Explicit ADOPTION (binding-per-org model): the sub's hosted account already lives
    # on the PLATFORM key in another of its orgs → "connect here" does not need
    # the wizard, we write the binding for THIS org. Safe: same shared key
    # ⟹ the account_id is reachable here; same sub ⟹ zero impersonation. `force=True`
    # (really different account) or `premium` (reconnection to ATTACH a
    # product) → wizard anyway.
    dead_seat_account = None  # DEAD (401) platform seat → to RECONNECT via the wizard
    if not force and not premium and platform_seat:
        mine = db.seat_binding_elsewhere(sub, provider, exclude_org=org_id)
        if mine:
            # Only re-adopt if the session is ALIVE (401 probe). Re-adopting a
            # dead account leaves the user "connected" on a 401 (lived internally:
            # disconnect→connect re-adopted the corpse instead of opening a login).
            # Dead account ⟹ we fall into the wizard in RECONNECT mode for THIS account
            # (type=reconnect, same account_id — not a duplicate). Fail-soft: probe
            # unavailable ⟹ we adopt (previous behaviour).
            alive = True
            try:
                from oto.tools.unipile import make_unipile_client
                # Off the loop: `account_alive` is a synchronous HTTP call, and
                # this function is `async def`. Called bare, it froze the whole
                # process while Unipile answered — up to 120 s of read time
                # (oto-backend#867). The `hosted_auth_link` fifteen lines below
                # was already protected: same file, same client, only one of the two
                # lines handled. Fixing what we look at does not close the class.
                alive = await asyncio.to_thread(
                    lambda: make_unipile_client(api_key=api_key).account_alive(
                        mine["account_id"]))
            # noqa: SILENT — documented fail-soft: probe unavailable ⇒ account considered alive
            except Exception:  # noqa: BLE001 — best-effort probe, never blocking
                alive = True
            if alive:
                # DIRECT write, and that is deliberate: `bind_account` guards an
                # identifier coming from a THIRD party (the provider's inventory). Here
                # the identifier comes from a row that the database
                # ALREADY attributes to this `sub` (`seat_binding_elsewhere` filters on it) —
                # checking it against someone else's ownership would prove nothing more, and
                # would refuse a legitimate adoption if a crossed binding were lying around in the
                # database. The AST ratchet of `tests/test_unipile_bind_guard.py` holds the
                # CLOSED list of writers: a third one must justify itself here.
                db.set_unipile_account(sub, mine["account_id"],
                                       account_name=mine.get("account_name"),
                                       org_id=org_id, provider=provider, platform_seat=True)
                logger.info("unipile adopt: sub=%s account=%s org=%s (from org %s)",
                            sub, mine["account_id"], org_id, mine.get("org_id"))
                return {"adopted": True, "channel": provider.lower(),
                        "account_name": mine.get("account_name")}
            dead_seat_account = mine["account_id"]
            logger.info("unipile adopt SKIP dead account: sub=%s account=%s → wizard reconnection",
                        sub, mine["account_id"])
    # BYO anti-duplicate (issue #172): an account connected under ANOTHER org's key
    # (BYO) is NOT adoptable here (an account_id only exists on the tenant of the
    # key that created it) → reconnecting the same login would create a 2nd account (rotation
    # of the li_at cookie, silent degradation). Actionable refusal.
    if not force:
        byo_elsewhere = [a for a in db.list_unipile_accounts(sub)
                         if a.get("provider") == provider and a.get("org_id") != org_id
                         and not a.get("platform_seat")]
        if byo_elsewhere:
            other = byo_elsewhere[0]
            who = other.get("account_name") or other["account_id"]
            raise ConnectRefused(
                409, "unipile_already_connected_elsewhere",
                f"You already have a {provider.lower()} account connected ('{who}') in "
                "another of your orgs, under that org's Unipile key (BYO) — "
                "it is not reachable here. To connect a different account, "
                "retry with force=true.")
    from oto.tools.unipile import make_unipile_client
    # DSN carried by the winning BYO credential (`config.dsn`); the platform stays
    # on the oto-core default (api.unipile.com).
    dsn = (await asyncio.to_thread(lambda: credential.config)).get("dsn") if byo else None
    client = make_unipile_client(api_key=api_key, dsn=dsn)
    # Activating a premium on an ALREADY connected account = `type=reconnect` on THIS account
    # (attaches the product without a DUPLICATE), not a `create` (which produced the
    # competing accounts we lived through). We only reconnect the sub's platform seat (same
    # shared key). ⚠️ INDEPENDENT of `force` (#237): the agent passes `force=true` TO
    # get past the anti-duplicate guard when the account is ALREADY connected — that is
    # precisely the case where we must RECONNECT (attach Recruiter/Sales Nav to the
    # existing seat), not create a 2nd account. `force` only governs the BYO anti-duplicate
    # above; it must no longer force a `create` that loses the premium product.
    # Reconnect (type=reconnect, NOT create) the existing account: (1) a dead seat
    # detected above, or (2) activating a premium on an already connected account.
    reconnect_account = dead_seat_account
    if not reconnect_account and premium and platform_seat:
        existing = db.seat_binding_elsewhere(sub, provider, exclude_org=None) \
            or ({"account_id": db.get_unipile_account_id(sub, org_id, provider)}
                if db.get_unipile_account_id(sub, org_id, provider) else None)
        if existing and existing.get("account_id"):
            reconnect_account = existing["account_id"]
    # The nonce is the link's `name` (opaque to the provider) and the key of the pending row
    # that `reconcile_pending` consumes. No `notify_url`: no longer called back in v2, and the
    # route that received it is removed (#581) — sending it would point to a 404.
    nonce = secrets.token_urlsafe(24)
    db.create_unipile_pending(nonce, sub, org_id, provider, platform_seat=platform_seat)
    ch = provider.lower()
    try:
        url = await asyncio.to_thread(
            functools.partial(
                client.hosted_auth_link,
                name=nonce,
                providers=[provider],
                success_redirect_url=_return_to(app, org_id, f"?unipile=connected&channel={ch}", sub),
                failure_redirect_url=_return_to(app, org_id, f"?unipile=failed&channel={ch}", sub),
                # requested premium product → `config.linkedin` (+ cookies in the wizard,
                # recommended by Unipile for these products)
                premium=premium,
                allow_cookies=bool(premium),
                # attach the product to the existing account (anti-duplicate)
                reconnect_account=reconnect_account,
            )
        )
    except Exception as e:
        raise ConnectRefused(502, "unipile_link_failed", f"unipile_link_failed: {e}")
    if not url:
        raise ConnectRefused(502, "unipile_link_empty", "unipile_link_empty")
    return {"url": url, "channel": ch}


# --- Poll-and-bind reconciliation: THE binding path ----------------------
# Hosted-auth v2 calls no per-link callback (the v2 webhook is configured at the
# level of the Unipile APPLICATION) and the account does not carry our nonce → nothing to
# correlate on return. So we LIST the Unipile accounts, NOT already linked, of the right
# provider, created AFTER the pending row (the floor avoids rebinding a pre-existing seat
# of a third party) — and we only link what is identified WITHOUT ambiguity (oto#247):
# the `account_id` returned on return, a dead row of the sub (proof of ownership), or a
# UNIQUE candidate that nobody else is waiting for. Otherwise, named refusal. Idempotent. The
# v1 linking webhook, a dormant twin path, was removed on 2026-08-29 (#581).

def _parse_dt(v):
    """Parse an Unipile date or a PG datetime into an aware `datetime` (UTC by default).
    None if unreadable — and since #580, an unreadable date REFUSES a binding: this
    read is therefore on the path of every connection, it must read what the
    provider serves on ALL supported Python versions (>= 3.10).

    Forms read: `2026-07-16 11:00:49.019235+00` (v1), ISO 8601 with trailing `Z`
    (`2026-07-16T11:00:49.019Z`, which `fromisoformat` rejects before 3.11), a fractional
    second of 1 to 9 digits (3.10 only reads 3 or 6), a Unix timestamp in
    seconds or milliseconds."""
    from datetime import datetime, timezone
    import re as _re
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)):
        secondes = v / 1000 if v > 1e11 else v
        try:
            return datetime.fromtimestamp(secondes, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    if "T" not in s and " " in s:
        s = s.replace(" ", "T", 1)
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    # normalize an offset "+00" / "+0000" to "+00:00" (fromisoformat 3.10 is strict)
    m = _re.search(r'([+-]\d{2})(\d{2})?$', s)
    if m and ":" not in s[m.start():]:
        s = s[:m.start()] + m.group(1) + ":" + (m.group(2) or "00")
    # a fractional second brought down to 6 digits (3.10 only reads 3 or 6)
    s = _re.sub(r'\.(\d+)', lambda f: "." + (f.group(1) + "000000")[:6], s, count=1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _rien(reason: str, detail: str) -> dict:
    """A reconciliation that bound nothing, and SAYS why."""
    return {"bound": False, "accounts": [], "reason": reason, "detail": detail}


def _attendu_ailleurs(sub: str, pend: dict, provider: str, created) -> bool:
    """Is a request from ANOTHER `sub` (same channel, same key population) waiting
    in a window that covers this account created at `created`? Unreadable date ⟹ yes:
    we cannot rule it out."""
    planchers = db.unipile_pending_floors_elsewhere(
        sub, provider, bool(pend.get("platform_seat")))
    if created is None:
        return bool(planchers)
    return any(created >= _parse_dt(f) - unipile_binding.MARGE_HORLOGE
               for f in planchers)


def reconcile_pending(sub: str, account_id: "str | None" = None) -> dict:
    """Links the account(s) freshly connected by `sub` without depending on the
    webhook. No-op if no pending row / no key / no new account.
    Returns `{bound: bool, accounts: [{account_id, name, org_id}]}`.

    `account_id` = the identifier Unipile appends to `redirect_uri` on success, read back
    by the front that receives the return (or passed by the agent,
    `linkedin_unipile_account(op="status", account_id=…)`). It RESTRICTS the
    candidates to that single account — it widens nothing: all the guards (provider,
    third party, already taken, floor, probe) apply as without it.

    ⚠️ **Without it, we no longer choose** (oto#247). The key is SHARED: two
    people connecting in the same window make both accounts
    candidates for each, and "the most recent alive" linked an account to the
    wrong person. Without a hint, an account is only linked if it is the ONLY
    live candidate AND (a dead row of `sub` proves it is theirs, OR no
    other `sub` has a pending request, same channel, whose window covers it).
    Otherwise: `ambiguous_candidates` refusal, nothing is written, the pending row stays —
    a call carrying the `account_id` will link it."""
    pendings = db.list_unipile_pending_for_sub(sub)
    if not pendings:
        return _rien("no_pending",
                     "No pending linking request for this account: the hosted-auth "
                     "link was not requested from this sub, or the linking has "
                     "already happened. Re-run `op=connect` to get one.")
    try:
        rc = access.resolve_credential("unipile", want="auto", sub=sub,
                                       emit_on_failure=False)
    except McpError:
        return _rien("no_credential",
                     "No resolvable Unipile credential for this account: the linking "
                     "cannot be verified with the provider. The flow may have "
                     "completed on its side without us being able to see it.")
    from oto.tools.unipile import make_unipile_client
    dsn = None if rc.is_platform else rc.config.get("dsn")
    client = make_unipile_client(api_key=rc.key, dsn=dsn)
    try:
        accounts = client.list_accounts()
    except Exception as e:  # noqa: BLE001 — best-effort, never fatal for the status
        logger.warning("reconcile unipile: list_accounts failed", exc_info=True)
        return _rien("provider_unreachable",
                     f"The provider did not answer the account listing "
                     f"({type(e).__name__}): the linking could not be attempted. "
                     "Try again — this is not a refusal.")
    taken = db.bound_unipile_account_ids()  # alive + dead (never a third party's seat)
    # The security guard (#559), set at WRITE time in `bind_account`: `foreign`
    # is the subset of `taken` that belongs to SOMEONE ELSE. `taken` stays,
    # but for what it really is here — a SELECTION heuristic (do not
    # re-pick an already attributed identifier while sweeping a list), not a boundary.
    # Distinguishing the two is what makes the boundary transposable to the next path.
    foreign = db.foreign_unipile_account_ids(sub)
    bound: list = []
    motifs: list = []
    done: set = set()   # providers linked during THIS pass
    for pend in pendings:
        provider = (pend.get("provider") or "LINKEDIN").upper()
        if provider in done:
            continue
        floor = _parse_dt(pend.get("created_at"))
        # DETERMINISTIC rebind: Unipile REUSES the existing account on reconnection
        # (same account_id) — a soft-disconnected row of the SAME sub is proof of
        # ownership → we rebind directly, without heuristics (the floor would miss an account
        # older than the pending row, case lived 2026-07-17).
        mine_dead = db.dead_unipile_account_ids_for(sub, provider)
        cand = []
        for a in accounts:
            aid = a.get("id")
            if not aid:
                continue
            if account_id and aid != account_id:
                continue
            if (a.get("provider") or a.get("type") or "").upper() != provider:
                continue
            # Mine (dead row) → candidate with no date condition; otherwise, created AFTER
            # the pending row. The shared guard settles both, and refuses a THIRD party's
            # account as well as an orphan seat with no readable date (#580).
            prov = unipile_binding.Provenance(a_moi=aid in mine_dead,
                                              cree_le=_parse_dt(a.get("created_at")),
                                              plancher=floor)
            if not unipile_binding.account_claimable(sub, aid, prov, foreign=foreign):
                continue
            if not prov.a_moi and aid in taken:
                continue
            cand.append((prov.cree_le, a, prov))
        if not cand:
            # The case of signal #689: the flow ended on the provider side
            # (final redirect seen by the user) and yet no account
            # is eligible. Three possible causes, indistinguishable until now because
            # this `continue` was silent.
            motifs.append({
                "nonce": pend.get("nonce"), "provider": provider,
                "reason": "no_candidate",
                # Not the NUMBER of accounts: on the platform key, that is the inventory
                # of all tenants, and this reason is served to the end user.
                "detail": (f"No eligible account for {provider} at the "
                           "provider: either the flow created "
                           "no account (abandoned before the end), or the account "
                           "ALREADY existed before the request (it is then older "
                           "than the pending row), or it belongs to someone else. "
                           "An account already linked elsewhere is freed by `op=disconnect` "
                           "on its holder's side."),
            })
            continue
        # SESSION probe on EACH candidate: only bind an ALIVE account. An aborted
        # wizard produces an account with `status:'running'` but dead (401 users/me)
        # — linking it made the agent hit a dead session while the old
        # healthy account stayed ignored (incident 2026-07-17). All probed, and no longer
        # only up to the first alive one: it is the NUMBER of alive ones that says whether
        # there is a choice to make (oto#247).
        vivants = [(created, a, p) for created, a, p in cand
                   if client.account_alive(a["id"])]
        chosen, prov = (vivants[0][1], vivants[0][2]) if len(vivants) == 1 else (None, None)
        # Without proof (neither hint nor row of the sub), a single candidate only belongs to `sub`
        # if nobody else is waiting for an account in the same window.
        sans_preuve = chosen is not None and not account_id and not prov.a_moi
        if len(vivants) > 1 or (sans_preuve and _attendu_ailleurs(
                sub, pend, provider, prov.cree_le)):
            # oto#247: several alive candidates, or a single one that a request from
            # ANOTHER sub could claim — and no proof to decide (the hint
            # restricts to one account, so `vivants` never has two with it).
            # "The most recent" linked one person's account to another: we no longer
            # guess. The pending row stays, a call carrying the `account_id` will link.
            logger.warning("reconcile unipile: ambiguous refusal sub=%s provider=%s "
                           "candidates=%s", sub, provider,
                           [a["id"] for _, a, _ in vivants])
            motifs.append({
                "nonce": pend.get("nonce"), "provider": provider,
                "reason": "ambiguous_candidates",
                # Neither the number of accounts nor the existence of a third party: the
                # mere possibility, which is enough to say what to do.
                "detail": ("More than one account may match this "
                           f"{provider} connection on the shared key (several connections in "
                           "the same window): without proof, nothing was linked, so as not to "
                           "attach someone else's account. Complete the "
                           "link's flow through to the return page: its address "
                           "carries `account_id=…`, to pass back (`POST "
                           "/api/me/unipile/reconcile`, or "
                           "`linkedin_unipile_account(op=\"status\", account_id=…)`). "
                           "Otherwise, restart the connection with a new link "
                           "(`op=connect`) in a few minutes."),
            })
            continue
        if chosen is None:
            logger.info("reconcile unipile: all candidates dead (401 session) sub=%s", sub)
            motifs.append({
                "nonce": pend.get("nonce"), "provider": provider,
                "reason": "candidates_dead",
                "detail": (f"{len(cand)} candidate account(s), all with a session "
                           "dead on the provider side (401): the flow produced an "
                           "account that the provider no longer authenticates. Redo the "
                           "flow to the end WITHOUT closing the tab before the "
                           "final redirect."),
            })
            continue
        issue = unipile_binding.bind_account(sub, chosen["id"], prov,
                             account_name=chosen.get("name"),
                             org_id=pend["org_id"], provider=provider,
                             platform_seat=bool(pend.get("platform_seat")),
                             foreign=foreign)
        if not issue.bound:
            # Unreachable in practice (the candidate already passed the guard above) —
            # but the write is guarded AT ITS point, not at the call site: that is
            # the discipline the twin path lacked.
            logger.warning("reconcile unipile: binding refused (%s) sub=%s account_id=%s",
                           issue.reason, sub, chosen["id"])
            motifs.append({"nonce": pend.get("nonce"), "provider": provider,
                           "reason": issue.reason or "bind_refused",
                           "detail": "The account was found but the write of the "
                                     "binding was refused at its guard point."})
            continue
        db.resolve_unipile_pending(pend["nonce"])
        # The OTHER requests for the same channel (double click on "Connect", link
        # requested again) are consumed along with it: left alive, they would stay for an
        # hour able to link the next account a third party connects on the key.
        for other in pendings:
            if (other is not pend
                    and (other.get("provider") or "LINKEDIN").upper() == provider):
                db.resolve_unipile_pending(other["nonce"])
        done.add(provider)
        taken.add(chosen["id"])
        bound.append({"account_id": chosen["id"], "name": chosen.get("name"),
                      "org_id": pend["org_id"]})
        logger.info("reconcile unipile: bound sub=%s account_id=%s org=%s",
                    sub, chosen["id"], pend["org_id"])
    out: dict = {"bound": bool(bound), "accounts": bound}
    if motifs and not bound:
        # Nothing was linked: return the established reason, like `unipile_binding` (#689).
        out["reason"] = motifs[0]["reason"]
        out["detail"] = motifs[0]["detail"]
        out["pendings"] = motifs
    return out


# --- The "connect" gesture, under the common checkpoint (#300) ----------

async def _start_flow(ctx, values: dict):
    """Starts the connection of a hosted channel — declared like any other flow.

    ⚠️ **This flow has two outcomes, and only one is a consent.** The nominal case
    returns a hosted link to open. But when the SAME person has already connected this
    channel elsewhere, the account is **adopted** — attached here without a wizard — and there is
    no page to open.

    The common contract promises "start a connection ⟹ a URL to open". Returning
    an empty URL in the adopted case would be a lie that a client would open; and
    making the URL optional would reopen for ALL a contract closed precisely
    because each flow invented its own shape there. Hence: **adoption is not a flow
    start**, it is a resolution — two gestures that a single route had merged
    because they share a button.

    Hence a TYPED and actionable refusal (`tool_not_mounted` pattern: a refusal that says
    what to do) rather than a mutilated `FlowStart`. Since the adoption has already happened, it
    does not ask to act: it states.

    The old REST route keeps serving its two outcomes as is until the
    front switches over — this batch does not touch it.
    """
    from .connectors import flow as connector_flow
    from .capabilities._types import AuthzDenied
    try:
        out = await hosted_auth_url(
            ctx.sub, str(values.get("channel") or "linkedin"),
            force=bool(values.get("force")),
            premium=(str(values["premium"]).strip().lower()
                     if values.get("premium") else None),
            # `app` travels with the gesture (the front already puts it in `params`):
            # without it, the end of the wizard goes back to oto-dashboard, whichever
            # front asked for the connection.
            app=(str(values["app"]) if values.get("app") else None))
    except ConnectRefused as e:
        raise AuthzDenied(e.status, e.code, e.message)
    if out.get("adopted"):
        raise AuthzDenied(
            409, "already_linked",
            f"This {out.get('channel') or 'hosted'} account was already connected under your "
            f"identity: it has just been attached here ({out.get('account_name') or 'account'}). "
            "No consent to give — re-read your identities to see it.")
    return connector_flow.FlowStart(auth_url=out["url"],
                                    details={"channel": out.get("channel")})
