"""Connected-identity selector (ADR 0024) — unified "list / choose an identity"
surface, with a backend PER CONNECTOR.

Three storage models coexist behind the same surface (we don't force a single one —
the unification is at the surface level, not the storage level):
- **Google**: N vault credentials (`account=email`), default = `meta.is_default`.
- **Unipile**: 1 key → N remote identities (opaque handles returned by the API),
  per-channel choice in `unipile_accounts`. **BYO-only**: under a platform key (resale)
  we keep the hosted-auth that creates a dedicated account (no cross-client exposure).
- **Backend declared by the connector** (`register()`, `browser_session` pattern):
  the enumeration logic lives in ITS OWN module `tools/<name>.py` (e.g. `pennylaneged`:
  the firm's companies = the target GEDs), the default in the credential's `meta`.

Common `Identity` contract = `{id, label, status, is_default, channel}` (`channel` None
outside multi-channel — accepted leak: unipile is per-channel, Google per-service).
Additive fields for a SHARED account (#55): `granted=True` + `owner={sub,email,name}`,
plus `via_group={id,name}` if the access comes from a group (2026-09 extension, `None`
otherwise) — the `label` prefers it over `owner` when present: "Growth team" identifies
a shared account better than the name of whoever historically connected it.

**Granted accounts (otomata-private#55)**: an account whose owner granted the operation
to the user (`connector_account_grants`) appears in the list and can be selected —
the selection sets the `unipile_operated_accounts` pointer (it NEVER touches the
grantee's `unipile_accounts` connection row). Validation of the select for a granted
account = the grant itself (deny-by-default), not `cli.list_accounts` (under resale the
grantee has no BYO key). Resolution at call time: `resolve_operated_account_id`
(revalidated against live grants, hard backstop).

⚠️ A registered backend may be **async** (e.g. Browserbase execution):
`list_identities`/`select_identity` then return an awaitable — the capabilities
(`capabilities/connectors/identities.py`) await the result when applicable.
"""
from __future__ import annotations

import asyncio


def _tableau_de_bord(sub) -> str:
    """The dashboard of THIS account — that of its product, not ours.

    Late import, like everything else in this module."""
    from .. import config
    return config.dashboard_url_for(sub)

# oto-backend#867 — DEFENSIBLE timeout for ONE off-loop Unipile HTTP call,
# bounded on the backend side (the oto-core client exposes no per-call `timeout`:
# its default is `(10, 120)` — 120s of READ, measured as responsible for a
# production freeze of 87.8s on 04/09). Measured on the "chronic" days of this
# endpoint: 2-4s normally, 19.9-23.3s on slow days that still answered,
# 46.2-87.9s on the days that froze the loop. 25s covers nearly all of the
# real responses observed and firmly cuts the two worst.
_UNIPILE_TIMEOUT_S = 25


async def _call_unipile(fn, *args):
    """One Unipile call (SYNC method of the oto-core client), off-loop and bounded.

    `asyncio.to_thread` takes it out of the event loop (otherwise the WHOLE
    process — MCP, REST, standby probes — waits on Unipile, #867).
    `asyncio.wait_for` bounds it to `_UNIPILE_TIMEOUT_S`: beyond that, it raises
    `TimeoutError` — the thread keeps running in the background until it truly ends
    (an in-flight `requests` call can't be interrupted), but the CALLER gets
    a named error instead of a freeze."""
    return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=_UNIPILE_TIMEOUT_S)


def supports(connector: str) -> bool:
    return connector in _LISTERS


def register(connector: str, lister, selector) -> None:
    """Declares a connector's identity backend (called on import of its
    `tools/*` module, like `browser_session.register`). `lister(sub)` →
    list[Identity]; `selector(sub, identity_id)` → Identity (ValueError if the id
    is not reachable by the credential — anti-binding). Sync or async."""
    _LISTERS[connector] = lister
    _SELECTORS[connector] = selector


SCOPES = ("member", "org", "group")


def list_identities(sub: str, connector: str, scope: str = "member"):
    """Identities reachable by the `sub`'s resolved credential for `connector`.
    [] if unsupported (or nothing to choose, e.g. unipile platform key). May
    return an awaitable (async backend registered via `register`).
    `scope` (Phase 2): `org`/`group` list the named accounts of the shared tier
    — generic keyed backend only; the specific backends (google,
    unipile) are per-member, another scope gets []."""
    fn = _LISTERS.get(connector)
    if not fn:
        return []
    if scope != "member":
        return fn(sub, scope) if connector in _KEYED else []
    return fn(sub)


def select_identity(sub: str, connector: str, identity_id: str, scope: str = "member"):
    """Chooses the identity `identity_id`. Raises `ValueError` if unsupported or if
    the id doesn't exist for this credential (anti arbitrary binding). May return
    an awaitable (async backend registered via `register`). `scope`: see
    `list_identities`; the access control of the shared tier (org /
    team admin) lives in the capability, not here."""
    fn = _SELECTORS.get(connector)
    if not fn:
        raise ValueError(f"The connector `{connector}` does not support account selection.")
    if scope != "member":
        if connector not in _KEYED:
            raise ValueError(f"The connector `{connector}` only has accounts at the member tier.")
        return fn(sub, identity_id, scope)
    return fn(sub, identity_id)


# --- Google: N vault credentials (account=email) ----------------------------

def _google_list(sub: str, service: "str | None" = None) -> list[dict]:
    """The member's Google accounts — ALL of them for the account, those that
    AUTHORIZED `service` for a service's card (split of 2026-09-26): `oto_identity(
    connector='drive')` must not offer an account that `drive_file` will refuse."""
    from ..auth import google as google_oauth
    ok = (lambda a: a.get("google_email") and (
        service is None or service in google_oauth.services_granted(a.get("scopes"))))
    mine = [{"id": a["google_email"], "label": a["google_email"], "status": "ok",
             "is_default": a["is_default"], "channel": None}
            for a in google_oauth.list_accounts(sub) if ok(a)]
    # Accounts SHARED by the team or org (2026-09-27): reachable via
    # `_account=`, marked `shared` with their tier in `shared_scope` (the label stays
    # the address: the screen says "shared" itself) — never the member's default.
    vus = {i["id"] for i in mine}
    partages = [{"id": a["google_email"], "label": a["google_email"],
                 "status": "ok", "is_default": False, "channel": None, "shared": True,
                 "shared_scope": "team" if a.get("scope") == "group" else "org"}
                for a in google_oauth.list_shared_accounts(sub)
                if ok(a) and a["google_email"] not in vus]
    return mine + partages


def _google_select(sub: str, identity_id: str) -> dict:
    from .. import access, db
    org = access.current_org(sub)
    if org is None or not db.set_default_google_account(sub, org, identity_id):
        raise ValueError(f"Unknown Google account: {identity_id}")
    return {"id": identity_id, "is_default": True, "channel": None}


# --- Unipile: 1 key → N remote identities (BYO-only) ------------------------
# + accounts GRANTED by their owner (#55, any mode — including resale).

def _own_unipile_account_id(sub: str, provider: str) -> str | None:
    """`sub`'s OWN connected Unipile account on this channel — the LIVE binding of
    the context org (the binding is an ACT per org, explicit model: a platform seat
    connected elsewhere is offered for ADOPTION at connect, never as a silent
    fallback here — the former auto #221 was removed, it made disconnect
    inconsistent). Only remaining cross-org case: **BYO** (#172) — the member key
    follows me into another org → account taken from the SAME org as the key
    (`personal_instance_org`), key and account paired. None if there is none."""
    from .. import access, providers, db
    org = access.current_org(sub)
    acc = db.get_unipile_account_id(sub, org, provider)
    if acc:
        return acc
    if providers.is_personal_cross_org("unipile"):
        pio = access.personal_instance_org(sub, "unipile", exclude_org=org)
        if pio is not None:
            return db.get_unipile_account_id(sub, pio, provider)
    return None


def _own_account_ids(sub: str, provider: str) -> set[str]:
    """All of the sub's own LIVE `account_id`s on this channel (all orgs) — guard
    set for the `_account=` pin (their own, in addition to granted accounts)."""
    from .. import db
    p = provider.upper()
    return {a["account_id"] for a in db.list_unipile_accounts(sub)
            if (a.get("provider") or "").upper() == p}


def refuser_si_preteur_en_pause(sub: str, provider: str, account_id: str) -> None:
    """Raises `PreteurEnPause` if `account_id` is lent to `sub` by a PAUSED account.

    To be called wherever a lent account is not (or no longer) operable, BEFORE the
    generic refusal: without it, the lender's pause would read as "authorization
    revoked or account disconnected", and the beneficiary would go ask again for a
    loan that was never withdrawn (#898, decision of 23/09/2026: option A)."""
    from .. import db
    preteur = db.suspended_lenders_for(sub, provider).get(account_id)
    if preteur:
        raise _preteur_en_pause(preteur, provider, account_id)


def _preteur_en_pause(pret: dict, provider: str, account_id: str):
    """The named refusal of a withheld loan, from a loan row (`owner_sub`,
    `owner_email`) — a single wording for the two paths that raise it."""
    from .. import account_suspension
    return account_suspension.PreteurEnPause(
        pret.get("owner_email") or pret["owner_sub"],
        f"Le compte {provider.title()} `{account_id}`")


def resolve_operated_account_id(sub: str, provider: str) -> str | None:
    """Unipile account operated by `sub` on this channel (THE #55/0051 resolution point).

    **Call pin `_account=` (ADR 0051)**: operated identity pinned FOR THIS
    CALL — takes precedence over the home pointer, EPHEMERAL (no state written).
    Guarded: GRANTED account (live #55) OR the sub's OWN account; a non-operable pin
    RAISES (never a silent fallback to another identity).

    Otherwise, a "home identity" pointer set → REVALIDATED against LIVE grants on
    every call (owner revocation or disconnection = immediate effect, hard
    backstop). Invalid pointer → EXPLICIT `ValueError`, never a silent fallback
    to the own account: the agent would believe it acts as the owner and would act
    as itself (a message sent under the wrong identity is irreversible).
    No pin and no pointer → own connected account (context org OR cross-org
    personal instance, #172)."""
    from .. import db, session_org
    pin = session_org.current_call_account()
    if pin:
        if pin in db.granted_accounts_for(sub, provider) or pin in _own_account_ids(sub, provider):
            return pin
        refuser_si_preteur_en_pause(sub, provider, pin)
        raise ValueError(
            f"The pinned {provider.title()} account (`_account=`) is neither "
            "yours nor an account granted to you — or it is no longer operable. List the "
            "operable identities with oto_identity(op='list').")
    op = db.get_operated_account(sub, provider)
    if op:
        if op["account_id"] in db.granted_accounts_for(sub, provider):
            return op["account_id"]
        # The pointer is NOT cleared: it's what makes the loan come back on wake-up.
        refuser_si_preteur_en_pause(sub, provider, op["account_id"])
        raise ValueError(
            f"The {provider.title()} account that was granted to you is no longer operable "
            "(authorization revoked or account disconnected by its owner). "
            "Reselect your identity (oto_identity(op='set') or "
            f"{_tableau_de_bord(sub)}/console/connectors).")
    return _own_unipile_account_id(sub, provider)


def _unipile_chosen(sub: str, provider: str) -> str | None:
    """Account actually operated, for the `is_default` display (valid pointer,
    otherwise own account) — fail-soft version of `resolve_operated_account_id`
    (an identity list must not raise on an orphaned pointer)."""
    from .. import db
    op = db.get_operated_account(sub, provider)
    if op and op["account_id"] in db.granted_accounts_for(sub, provider):
        return op["account_id"]
    return _own_unipile_account_id(sub, provider)


def _unipile_client(sub: str):
    """(client, byo) — resolves key+DSN of the BYO credential; None if non-BYO/absent."""
    from .. import access
    if access.credential_mode_for(sub, "unipile") not in access.BYO_MODES:
        return None  # resale (platform key) → hosted-auth, no selector
    rc = access.resolve_credential("unipile", want="byo", sub=sub)
    from oto.tools.unipile import make_unipile_client
    # dsn paired with the key (default api.unipile.com on the oto-core side) — a key that lives
    # on a distinct tenant carries its dsn in the credential's config.
    return make_unipile_client(api_key=rc.key, dsn=rc.config.get("dsn"))


async def _unipile_live_status_map(sub: str) -> dict:
    """LIVE status of hosted accounts, read on the Unipile PLATFORM key:
    `{account_id: status}`.

    The resale / hosted-auth mode persists accounts in the DB and does NOT query
    Unipile → a really dead account (checkpoint, expired credentials, revoked
    by the user) wrongly displayed "ok" (#201). The real status is only readable
    by listing the subscription's accounts (`list_accounts().sources[].status`).

    ⚠️ But this account `sources[].status` can ITSELF stay "OK" while
    the SESSION is dead (checkpoint / rotated li_at cookie) → a real call gets
    a 401 but the card said "connected" (#236). So we confirm liveness
    with an `account_alive` probe (GET users/me → 401 = dead) and downgrade to
    'disconnected'. Identity PICKER path ONLY (outside the hot /api/me loop —
    accepted budget, one users/me call per hosted account, on clicking the selector).
    Fail-soft: `{}` if unavailable (the caller falls back to "ok", the
    previous behavior); best-effort probe PER account (an incident keeps the account status)."""
    from .. import access
    try:
        rc = access.resolve_credential("unipile", want="auto", sub=sub)
        from oto.tools.unipile import make_unipile_client
        cli = make_unipile_client(api_key=rc.key, dsn=rc.config.get("dsn"))
        out: dict = {}
        for a in await _call_unipile(cli.list_accounts):
            aid = a.get("id")
            if not aid:
                continue
            status = (a.get("sources") or [{}])[0].get("status")
            try:  # real liveness probe (#236): users/me 401 = dead session
                if not await _call_unipile(cli.account_alive, aid):
                    status = "disconnected"
            # noqa: SILENT — best-effort: keeps the account status on probe incident
            except Exception:
                pass  # best-effort: keeps the account status on probe incident
            out[aid] = status
        return out
    # noqa: SILENT — live status probe unavailable ⇒ stored status kept
    except Exception:
        return {}


async def _unipile_list(sub: str, canal: str | None = None) -> list[dict]:
    """Hosted identities reachable by `sub`. `canal` (LINKEDIN/WHATSAPP/…) =
    return only those of THIS channel — what a channel connector's card sees
    since the 2026-08-28 split. None = all channels (call path that doesn't
    know a channel)."""
    from .. import db
    granted = [g for g in db.list_account_grants_to(sub) if g.get("active")]
    out = []
    cli = _unipile_client(sub)
    # Live status of hosted accounts (platform key), resolved at most once and
    # only if a non-BYO account requires it (#201). Fail-soft → "ok".
    _live: dict = {}

    async def _live_status(account_id: str) -> str:
        if "map" not in _live:
            _live["map"] = await _unipile_live_status_map(sub)
        return _live["map"].get(account_id) or "ok"

    def _statut_mesure(account_id: str) -> bool:
        """Did the probe ANSWER for this account?

        oto#42, rule 1: a value we couldn't establish is never rendered
        by its default — and "ok" is the worst of defaults, it asserts that it
        works. The probe is fail-soft (empty map if it fails wholesale, account
        missing if it failed for it alone), and the caller then fell back on
        "ok" without any trace warning it: a really dead account
        showed up as connected, which is the #201/#236 defect through a third
        path — that of the PROBE OUTAGE, not that of the stale status.
        We don't change the served value (the front reads it), we say whether it was
        MEASURED. False ⟹ `status` is the stored status, not an observation."""
        return account_id in _live.get("map", {})
    if cli is not None:  # BYO: the key's accounts (existing list)
        # oto-backend#867 — NO LONGER swallow the failure: this is the list itself
        # HERE (not a side status probe), so a slow or down Unipile must
        # return a named error (`_list`, capabilities/connectors/identities.py,
        # converts it to `unipile_list_failed`), never a silent empty list.
        accounts = await _call_unipile(cli.list_accounts)
        for a in accounts:
            ch = (a.get("type") or "").upper() or None
            sources = a.get("sources") or []
            out.append({
                "id": a.get("id"),
                "label": a.get("name"),
                "status": (sources[0].get("status") if sources else None) or "ok",
                "is_default": bool(ch) and a.get("id") == _unipile_chosen(sub, ch),
                "channel": ch,
            })
    else:
        # Resale (platform key / hosted-auth): the OWN accounts connected
        # IN THE CONTEXT ORG. Always listed — even without a grant and with no
        # "choice" to make, a connected account MUST appear (feedback #132:
        # `identities: []` while a hosted LinkedIn was connected = false
        # negative, the agent wrongly concluded "no account" and sent
        # the user back to the dashboard). Org filter = member scope ADR 0033 B4,
        # aligned with `status_for` and the call resolution (`get_unipile_account_id`):
        # an account from ANOTHER org is not operable here → listing it would be a
        # false positive (inert "Use this account" button, experienced 2026-07-08).
        accounts = db.list_unipile_accounts(sub)
        if accounts:  # org resolved only if there is something to filter
            from .. import access
            org = access.current_org(sub)
            accounts = [a for a in accounts if a.get("org_id") == org]
        for a in accounts:
            out.append({
                "id": a["account_id"],
                "label": a.get("account_name") or a["account_id"],
                "status": await _live_status(a["account_id"]),
                # Said ONLY on a discrepancy: an always-present field becomes noise
                # that people stop reading. Absent ⟹ the status was indeed measured.
                **({} if _statut_mesure(a["account_id"]) else {
                    "status_measured": False,
                    "status_hint": (
                        "the liveness probe did not answer for this account: "
                        "`status` is the last KNOWN state, not an observation. A dead "
                        "account may show up there as \"ok\". Retry to measure."),
                }),
                "is_default": a["account_id"] == _unipile_chosen(sub, a["provider"]),
                "channel": a["provider"],
            })
    # GRANTED accounts (#55), any mode. A shared BYO key already lists the owner's
    # account → we ANNOTATE the existing entry rather than duplicate it.
    seen = {i["id"]: i for i in out}
    for g in granted:
        owner = {"sub": g["owner_sub"], "email": g.get("owner_email"),
                 "name": g.get("owner_name"),
                 "org": g.get("owner_org_id"), "org_name": g.get("owner_org_name")}
        # Received via a group (#55 extension, 2026-09): says WHAT carries the access,
        # not just who owns the account — a member who doesn't know the
        # owner still recognizes "the Growth team". None
        # on a named grant (`via_group_id` absent or empty, e.g. original #55).
        via_group_id = g.get("via_group_id")
        via_group = ({"id": via_group_id, "name": g.get("via_group_name")}
                     if via_group_id else None)
        existing = seen.get(g["account_id"])
        if existing is not None:
            existing["granted"] = True
            existing["owner"] = owner
            existing["via_group"] = via_group
            continue
        if via_group:
            libelle = f"team account ({via_group['name'] or via_group_id})"
        else:
            libelle = f"account of {g.get('owner_name') or g.get('owner_email') or g['owner_sub']}"
        out.append({
            "id": g["account_id"],
            "label": f"{g.get('account_name') or g['account_id']} — {libelle}",
            "status": await _live_status(g["account_id"]),
            "is_default": g["account_id"] == _unipile_chosen(sub, g["provider"]),
            "channel": g["provider"],
            "granted": True,
            "owner": owner,
            "via_group": via_group,
        })
    if canal:
        # Filter AFTER annotating granted accounts: a granted account of the right
        # channel must stay listed (it's the only identity some grantees
        # have). An account whose channel is UNKNOWN (`channel=None` — seen in BYO when
        # Unipile doesn't return `type`) attaches to no channel card: silencing it
        # here is better than making it appear under all six.
        _c = canal.upper()
        out = [i for i in out if (i.get("channel") or "").upper() == _c]
    return out


async def _unipile_select(sub: str, identity_id: str, canal: str | None = None) -> dict:
    """Chooses the operated identity. `canal` (a channel connector's card) = guard:
    you don't switch your Telegram identity from the WhatsApp card. The REAL channel
    is the ACCOUNT's, never the one we assume — hence a guard on the result
    rather than a filter on the input: the three selection paths (granted account,
    return to self, BYO switch) each discover it in their own way, and a single place
    must decide."""
    from .. import db

    def _exige_canal(trouve: str | None) -> None:
        """Refuses BEFORE writing if the account is not of the card's channel.

        After the write it would be too late: setting the pointer then raising
        would leave the operated identity changed by a call that returned an error."""
        if canal and (trouve or "").upper() != canal.upper():
            raise ValueError(
                f"This account is a {(trouve or 'unknown').title()} account: it can't be "
                f"chosen from the {canal.title()} card. Use the card of "
                "its channel.")
    # 1) GRANTED account (#55): sets the "operated identity" POINTER — NEVER touches
    #    the grantee's `unipile_accounts` connection row. Validation
    #    = the live grant (deny-by-default), not the key.
    recus = db.list_account_grants_to(sub)
    g = next((r for r in recus
              if r.get("active") and r["account_id"] == identity_id), None)
    if g:
        _exige_canal(g["provider"])
        db.set_operated_account(sub, g["provider"], identity_id, g["owner_sub"])
        return {"id": identity_id, "channel": g["provider"], "is_default": True,
                "granted": True}
    # 1bis) Account lent by a PAUSED account (#898): the grant exists, it is withheld
    #    — say so, rather than falling further down to "unknown account".
    retenu = next((r for r in recus
                   if r.get("owner_suspended") and r["account_id"] == identity_id), None)
    if retenu:
        raise _preteur_en_pause(retenu, retenu["provider"], identity_id)
    # 2) Return to SELF (any mode, including resale): clears the channel's pointer.
    own = next((a for a in db.list_unipile_accounts(sub)
                if a["account_id"] == identity_id), None)
    if own:
        _exige_canal(own["provider"])
        db.clear_operated_account(sub, own["provider"])
        return {"id": identity_id, "channel": own["provider"], "is_default": True}
    # 3) Existing BYO path: choose an account of ITS key (switches the connection).
    cli = _unipile_client(sub)
    if cli is None:
        raise ValueError("Account selection unavailable (platform key — use "
                         "the hosted connection).")
    # oto-backend#867 — same rule as `_unipile_list`: an Unipile outage/slowness here
    # must be stated, not confused with an unknown id (`unknown_identity`, 404) — the
    # capable layer (`_set_default`) distinguishes this `RuntimeError` from a `ValueError`.
    try:
        accounts = await _call_unipile(cli.list_accounts)
    except Exception as e:
        raise RuntimeError(f"Unipile did not answer when choosing this account: {e}") from e
    match = next((a for a in accounts if a.get("id") == identity_id), None)
    if match is None:  # anti-binding: the id MUST exist on the key (or be granted)
        raise ValueError(f"Unknown Unipile account on this key: {identity_id}")
    ch = (match.get("type") or "LINKEDIN").upper()
    _exige_canal(ch)
    # Member scope (ADR 0033 B4): the binding applies in the context org. BYO →
    # not a platform seat (platform_seat=False), consistent with unipile_connect.
    # Connection switch = return-to-self on this channel → clears the operated pointer (#55).
    from .. import access
    org = access.current_org(sub)
    if org is None:
        raise ValueError("No context org — unable to attach the account.")
    db.set_unipile_account(sub, identity_id, match.get("name"), org_id=org,
                           provider=ch, platform_seat=False)
    db.clear_operated_account(sub, ch)
    return {"id": identity_id, "channel": ch, "is_default": True}


# --- GENERIC keyed backend: N vault credentials (account=free label) ---
# For any multi-account connector (`Connector.auth_multi_account`) WITHOUT a specific
# backend (google has one): the accounts = the vault rows at the MEMBER scope of the
# context org, the default = `meta.is_default`. E.g. "2 Zoho" (FR/US self-clients).

def keyed_entity(sub: str, scope: str) -> "tuple[str, str] | None":
    """The vault entity targeted by a `scope` (member | org | group) for `sub`, or
    None without a context org/team. Phase 2 (2026-08-25): named accounts
    also exist at the shared tiers."""
    from .. import access, credentials_store
    org = access.current_org(sub)
    if org is None:
        return None
    if scope == "org":
        return ("org", str(org))
    if scope == "group":
        gid = access.current_group(sub)
        return None if gid is None else ("group", str(gid))
    return (credentials_store.MEMBER, credentials_store.member_id(org, sub))


def _keyed_list(sub: str, connector: str, scope: str = "member") -> list[dict]:
    from .. import credentials_store
    ent = keyed_entity(sub, scope)
    if ent is None:
        return []
    from .. import group_store
    rows = (group_store.list_group_accounts(int(ent[1]), connector) if ent[0] == "group"
            else credentials_store.list_accounts(ent[0], ent[1], connector))
    out = []
    for row in rows:
        acct = row["account"]
        meta = row.get("meta") or {}
        entry = {
            "id": acct,
            "label": meta.get("label") or acct or "(default)",
            "status": "ok",
            "is_default": bool(meta.get("is_default")),
            "channel": None,
        }
        if meta.get("lent_by"):
            # A member's instance lent to the team: same shape as a granted account.
            entry.update(granted=True, owner={"sub": meta["lent_by"]})
        out.append(entry)
    return out


def _refuse_lent(sub: str, connector: str, identity_id: str, scope: str, action: str) -> None:
    """An account LENT to the team stays its lender's: the team neither renames it nor
    makes it its default (the default lives on the team's own vault rows)."""
    if scope != "group":
        return
    from .. import group_store
    ent = keyed_entity(sub, scope)
    if ent is None:
        return
    for i in group_store.lent_instances(int(ent[1]), connector):
        if i["account"] == identity_id:
            raise ValueError(
                f"`{identity_id}` is a member's instance lent to the team: the team "
                f"cannot {action} it. Only its lender manages it.")


def _keyed_select(sub: str, connector: str, identity_id: str, scope: str = "member") -> dict:
    from .. import credentials_store
    ent = keyed_entity(sub, scope)
    if ent is None:
        raise ValueError("No context org/team — unable to choose an account.")
    _refuse_lent(sub, connector, identity_id, scope, "set as default")
    accounts = [r["account"] for r in credentials_store.list_accounts(ent[0], ent[1], connector)]
    if identity_id not in accounts:
        raise ValueError(f"Unknown account `{identity_id}` for {connector}.")
    # UNIQUE default: sets is_default on the chosen row, removes it from the others.
    for acct in accounts:
        credentials_store.update_meta(ent[0], ent[1], connector, acct,
                                      {"is_default": acct == identity_id})
    return {"id": identity_id, "label": identity_id, "is_default": True, "channel": None}


def rename_identity(sub: str, connector: str, identity_id: str, new_name: str,
                    scope: str = "member") -> dict:
    """Renames a named account of the generic keyed backend — the name IS the identifier
    that the agent passes as `_account=`, so it's the vault row that changes
    (`credentials_store.rename_account`: re-encryption, the instance follows). Raises
    `ValueError` (connector without vault accounts, unknown account, empty or already
    taken name); the tier's access control lives in the capability."""
    from .. import credentials_store
    if connector not in _KEYED:
        raise ValueError(f"The connector `{connector}` has no renamable accounts.")
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("The new name is empty.")
    _refuse_lent(sub, connector, identity_id, scope, "rename")
    ent = keyed_entity(sub, scope)
    if ent is None:
        raise ValueError("No context org/team — unable to rename an account.")
    rows = {r["account"]: r for r in credentials_store.list_accounts(ent[0], ent[1], connector)}
    if identity_id not in rows:
        raise ValueError(f"Unknown account `{identity_id}` for {connector}.")
    if new_name == identity_id:
        return {"id": identity_id, "is_default": bool((rows[identity_id].get("meta") or {})
                                                        .get("is_default"))}
    if new_name in rows:
        # `rename_account` would overwrite the destination row (upsert): a lost key.
        raise ValueError(f"An account `{new_name}` already exists for {connector}.")
    credentials_store.rename_account(ent[0], ent[1], connector, identity_id, new_name)
    return {"id": new_name, "is_default": bool((rows[identity_id].get("meta") or {})
                                                .get("is_default"))}


_LISTERS = {"google": _google_list, "unipile": _unipile_list}
_SELECTORS = {"google": _google_select, "unipile": _unipile_select}
# Connectors served by the GENERIC keyed backend (the only one that knows the shared
# tiers) — filled by `_register_keyed_multi_account`.
_KEYED: set[str] = set()


def _register_keyed_multi_account() -> None:
    """Registers the generic keyed backend for every multi-account connector
    (`Connector.auth_multi_account` — since 2026-08-25, every API key is one by
    default; the curated list was removed on 29/08) that doesn't already
    have a specific backend (google, unipile). Closures binding the connector's
    name (arg default = capture by value)."""
    from .. import providers
    for con in providers._REGISTRY_LIST:
        name = con.name
        if not con.auth_multi_account or name in _LISTERS:
            continue
        _KEYED.add(name)
        _LISTERS[name] = lambda sub, scope="member", c=name: _keyed_list(sub, c, scope)
        _SELECTORS[name] = lambda sub, iid, scope="member", c=name: _keyed_select(sub, c, iid, scope)


def _register_hosted_channels() -> None:
    """One identity backend per hosted CHANNEL (split of 2026-08-28).

    `oto_identity(connector='whatsapp')` must return the WhatsApp accounts, not the
    six channels: since each channel has its own card, an unfiltered list would show
    a LinkedIn there that no button on this card can operate. Same body
    (`_unipile_list`/`_unipile_select`) with an added channel — resolution, grants
    and live status remain ONE single path.

    (These connectors don't go through the generic keyed backend: `hosted` ⟹
    `auth_multi_account` false — their accounts are not vault rows.)"""
    from .. import providers
    for con in providers._REGISTRY_LIST:
        if not con.hosted_channel:
            continue
        # `_ch` captured by value: without the argument default, the six backends
        # would close over the same loop variable (hence over the last channel).
        _LISTERS[con.name] = (
            lambda sub, scope="member", _ch=con.hosted_channel: _unipile_list(sub, _ch))
        _SELECTORS[con.name] = (
            lambda sub, iid, scope="member", _ch=con.hosted_channel:
            _unipile_select(sub, iid, _ch))


def _register_google_services() -> None:
    """One identity backend per Google SERVICE (split of 2026-09-26): same body
    as the account, filtered on the authorized scope. Registered BEFORE the generic
    keyed backend — which would otherwise take these multi-account connectors for vault
    keys under their name (no row lives there: always-empty list, without a word).
    Population derived from the registry (`credential_of == "google"`), never written."""
    from .. import providers
    for con in providers._REGISTRY_LIST:
        if con.credential_of != "google":
            continue
        # `_s` captured by value: without the argument default, the six backends
        # would close over the same loop variable (hence over the last service).
        _LISTERS[con.name] = (
            lambda sub, scope="member", _s=con.name: _google_list(sub, service=_s))
        _SELECTORS[con.name] = _google_select


def _microsoft_list(sub: str, service: str) -> list[dict]:
    """The member's Microsoft accounts that AUTHORIZED `service` — same shape as the
    generic keyed backend, filtered like a Google service: `oto_identity(
    connector='sharepoint')` must not offer an account that `sharepoint_file` will refuse."""
    from .. import db
    from ..auth import microsoft as ms_auth
    comptes = ms_auth.accounts_for(sub, service)
    # An account LENT by a peer (`oto_instance op=lend`) is operable through `_account=`
    # and marked `granted` + `owner`, like a granted Unipile account: not the borrower's
    # to revoke nor to make their default.
    preteurs = sorted({c["lent_by"] for c in comptes if c.get("lent_by")})
    emails = db.emails_by_subs(preteurs) if preteurs else {}
    out = []
    for c in comptes:
        meta = c.get("meta") or {}
        ident = {"id": c["account"], "label": meta.get("label") or c["account"],
                 "status": "ok", "is_default": bool(meta.get("is_default")),
                 "channel": None}
        if c.get("lent_by"):
            ident.update(granted=True, is_default=False,
                         owner={"sub": c["lent_by"], "email": emails.get(c["lent_by"]),
                                "org": c.get("lender_org")})
        out.append(ident)
    return out


def _register_microsoft_services() -> None:
    """One identity backend per Microsoft SERVICE: the accounts of the `microsoft`
    carrier that authorized it; the default is the carrier's (one default per person,
    shared by the services, as for Google). Registered BEFORE the generic keyed backend —
    which would otherwise take these multi-account connectors for vault keys under their
    name (no row lives there). The carrier itself goes through the generic backend
    (list, default, rename). Population derived from the registry."""
    from .. import providers
    for con in providers._REGISTRY_LIST:
        if con.credential_of != "microsoft":
            continue
        _LISTERS[con.name] = (
            lambda sub, scope="member", _s=con.name: _microsoft_list(sub, _s))
        _SELECTORS[con.name] = (
            lambda sub, iid, scope="member": _keyed_select(sub, "microsoft", iid))


_register_google_services()
_register_microsoft_services()
_register_keyed_multi_account()
_register_hosted_channels()
