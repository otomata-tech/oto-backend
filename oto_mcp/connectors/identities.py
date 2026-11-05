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
An identity that is not the caller's own — her team's, her org's, a peer's — carries
`level` (`read`|`write`|`sensitive`|`manage`: what the rules of that account give her,
#1499) and `owner` (`{type, id}`, plus `label`/`name` when known). It is operable NAMED
(`_account=`, `_instance=`), never her default; unnamed only at step 5, when nothing of
hers, her team's or her org's serves. An account of ANOTHER org (D1)
is listed only when it is hers (`elsewhere_org`, #172) or a rule naming her opens it
(`lent_by_org` + its owner's label) — nothing else of that org (`_hors_org`). There is no pointer and no grant:
another person's hosted account is resolved by `resolve_operated_account_id` through
`connectors/comptes_heberges` at the level of its sub-instance's rules.

⚠️ A registered backend may be **async** (e.g. Browserbase execution):
`list_identities`/`select_identity` then return an awaitable — the capabilities
(`capabilities/connectors/identities.py`) await the result when applicable.
"""
from __future__ import annotations

import asyncio

from . import comptes_heberges


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


def _oauth_status(meta: dict) -> str:
    """An OAuth account's line status: `reauth_required` when its vault row carries
    the health mark (refresh refused, `connector_health.mark_rejected`) — the mark that
    `/api/me` already reads for « État pour toi ». A fixed `ok` showed a green dot next
    to a « Connexion KO » verdict, with no way to reconnect from the line."""
    return "reauth_required" if meta.get("health_ko") else "ok"


# --- Google: N vault credentials (account=email) ----------------------------

# The holder type of an account of the shared tier, as its instance names it.
_TYPE_DU_PALIER = {"group": "group", "org": "org"}


def _niveaux(sub: str, porteur: str, service: "str | None", cles: list) -> dict:
    """`{(owner_type, owner_id, account): level}` — the level the rules of each of these
    accounts (not the caller's own) give her, in one read (#1499). An account without
    its instance is a defect: RAISES, never a level presumed."""
    if not cles:
        return {}
    from .. import access
    comptes = access.comptes_des_lignes(porteur, cles)
    appelant = access.Appelant(sub, access.current_org(sub),
                                       lambda: access.current_group(sub))
    out = {}
    for cle in cles:
        compte = comptes.get(cle)
        if compte is None:
            raise RuntimeError(f"{porteur} account {':'.join(cle)} has no live instance: "
                               "its level cannot be judged")
        out[cle] = access.niveau(appelant, compte, service)
    return out


def _hors_org(sub: str, porteur: str, service: "str | None", vus) -> list[dict]:
    """The accounts of `porteur` in ANOTHER org than the call's that `sub` may NAME (D1,
    #1499), never served unnamed: her own key set in another org (#172, marked
    `elsewhere_org`) and another person's account a rule naming her opens
    (`lent_by_org`, plus its owner's label — nothing else of the lending org).
    `vus` = ids already listed (an id is listed once)."""
    from .. import access
    org = access.current_org(sub)
    if org is None:
        return []
    appelant = access.Appelant(sub, org, lambda: access.current_group(sub))
    out = []
    for compte in access.comptes_hors_org(porteur, org, sub):
        if compte.account in vus:
            continue
        detenu = access.niveau(appelant, compte, service)
        if access.RANG[detenu] == 0:
            continue
        ident = {"id": compte.account, "label": compte.label or compte.account,
                 "status": "ok", "is_default": False, "channel": None, "level": detenu}
        if compte.proprietaire == sub:
            ident["elsewhere_org"] = compte.org_nom
        else:
            ident.update(lent_by_org=compte.org_nom,
                         owner={"type": "member", "id": compte.owner_id,
                                "label": access.libelle_de_personne(compte.proprietaire)})
        out.append(ident)
    return out


def _google_list(sub: str, service: "str | None" = None) -> list[dict]:
    """The member's Google accounts — ALL of them for the account, those that
    AUTHORIZED `service` for a service's card (split of 2026-09-26): `oto_identity(
    connector='drive')` must not offer an account that `drive_file` will refuse."""
    from ..auth import google as google_oauth
    ok = (lambda a: a.get("google_email") and (
        service is None or service in google_oauth.services_granted(a.get("scopes"))))
    mine = [{"id": a["google_email"], "label": a["google_email"], "status": _oauth_status(a),
             "is_default": a["is_default"], "channel": None}
            for a in google_oauth.list_accounts(sub) if ok(a)]
    # The accounts of the team or org (2026-09-27): reachable via `_account=`, marked
    # with the `level` their rules give the caller and their `owner` (#1499) — never
    # the member's default.
    vus = {i["id"] for i in mine}
    autres = [a for a in google_oauth.list_shared_accounts(sub)
              if ok(a) and a["google_email"] not in vus]
    cles = [(_TYPE_DU_PALIER[a["scope"]], str(a["target_id"]), a["google_email"])
            for a in autres]
    niveaux = _niveaux(sub, "google", service, cles)
    partages = [{"id": a["google_email"], "label": a["google_email"], "status": "ok",
                 "is_default": False, "channel": None, "level": niveaux[cle],
                 "owner": {"type": cle[0], "id": cle[1]}}
                for a, cle in zip(autres, cles)]
    listes = mine + partages
    return listes + _hors_org(sub, "google", service, {i["id"] for i in listes})


def _google_select(sub: str, identity_id: str) -> dict:
    from .. import access, db
    org = access.current_org(sub)
    if org is None or not db.set_default_google_account(sub, org, identity_id):
        raise ValueError(f"Unknown Google account: {identity_id}")
    return {"id": identity_id, "is_default": True, "channel": None}


# --- Unipile: 1 key → N remote identities (BYO-only) ------------------------
# + the accounts other members of the org opened by their rules (#1499), served NAMED.

def _own_unipile_account_id(sub: str, provider: str) -> str | None:
    """`sub`'s OWN connected Unipile account on this channel — the LIVE binding of
    the context org (the binding is an ACT per org: an account connected elsewhere is
    offered for ADOPTION at connect, never as a fallback here). None if there is none."""
    from .. import access, db
    return db.get_unipile_account_id(sub, access.current_org(sub), provider)


def _own_account_ids(sub: str, provider: str) -> set[str]:
    """The sub's own LIVE `account_id`s on this channel linked to the context org
    (member scope, ADR 0033 B4) — guard set for the `_account=` pin. An account linked
    to ANOTHER org of the same sub is not in it (#1342, option C of 27/10/2026): such a
    pin gets the named refusal `account_in_other_org` (see `_ligne_dans_une_autre_org`)."""
    from .. import access, db
    p = provider.upper()
    org = access.current_org(sub)
    return {a["account_id"] for a in db.list_unipile_accounts(sub)
            if (a.get("provider") or "").upper() == p and a.get("org_id") == org}


# #1342 (option C of 27/10/2026): the code of the refusal served when the caller's OWN
# account of this channel is linked in ANOTHER of their orgs, not in the context org.
# Never « connect your account »: the account exists, a new pairing would create a
# second session at the provider. The gesture is the adoption (`unipile_connect_start`).
CODE_AUTRE_ORG = "account_in_other_org"


class CompteDansUneAutreOrg(ValueError):
    """The caller's own account of this channel lives in another of their orgs.

    `ValueError` like the other « this identity is not operable here » refusals of the
    resolution (their callers convert them, `tools/unipile._refus_identite`); the
    subclass carries the code that NAMES it."""

    code = CODE_AUTRE_ORG


def comptes_dans_une_autre_org(sub: str, provider: "str | None" = None) -> list[dict]:
    """The caller's hosted accounts linked in ANOTHER of their orgs and ADOPTABLE here
    (`status_for(...)['elsewhere']`, single source: platform seat on the shared key,
    option open in the context org). `provider` (LINKEDIN, WHATSAPP…) = this channel
    only; None = every channel. Each entry: `{channel, account_id, account_name,
    org_id, next_step}` — `next_step` is the exact gesture (adoption, no new pairing)."""
    from ..tools import unipile as unipile_tool
    canaux = unipile_tool.UNIPILE_CHANNELS
    out = []
    for front, compte in unipile_tool.status_for(sub)["elsewhere"].items():
        canal = canaux[front]
        if provider and canal != provider.upper():
            continue
        entree = {"channel": canal, **compte}
        out.append({**entree, "next_step": phrase_compte_ailleurs(entree, adoptable=True)})
    return out


def phrase_compte_ailleurs(compte: dict, *, adoptable: bool) -> str:
    """The sentence that names an own account linked in another org, and the gesture.

    `adoptable` = the adoption by `unipile_connect_start` works from here (platform seat,
    option open here). Otherwise the account stays usable from ITS org (`_org=`)."""
    canal = (compte.get("channel") or "").upper()
    front = canal.lower()
    nom = compte.get("account_name") or compte["account_id"]
    debut = (f"Your {canal.title()} account '{nom}' (`{compte['account_id']}`) is linked "
             f"to another of your orgs (org {compte.get('org_id')}), not to this one: an "
             "account is only served in the org it is linked to.")
    ailleurs = f" — or run the call in its org with `_org={compte.get('org_id')}`."
    if adoptable:
        return (debut + f" To use it here, call `unipile_connect_start(channel='{front}')`: "
                "it adopts this SAME account in the current org, with no new pairing (no "
                "login, no QR code). Do not reconnect it" + ailleurs)
    return (debut + " It cannot be adopted here as is (another account of this channel "
            "is already linked here, it lives under that org's own Unipile key, or hosted "
            "messaging is not open in this org): use it from its org" + ailleurs)


def _ligne_dans_une_autre_org(sub: str, provider: str, account_id: str) -> "dict | None":
    """The caller's own live row for `account_id` on this channel in ANOTHER org (the
    first one), or None."""
    from .. import access, db
    p = provider.upper()
    org = access.current_org(sub)
    return next((a for a in db.list_unipile_accounts(sub)
                 if a.get("account_id") == account_id and a.get("org_id") != org
                 and (a.get("provider") or "").upper() == p), None)


def refuser_si_compte_dans_une_autre_org(sub: str, provider: str, account_id: str) -> None:
    """Raises `CompteDansUneAutreOrg` if the pinned `account_id` is the caller's own
    account linked to another org (#1342): the named refusal, with the gesture."""
    ligne = _ligne_dans_une_autre_org(sub, provider, account_id)
    if ligne is None:
        return
    adoptables = {c["account_id"] for c in comptes_dans_une_autre_org(sub, provider)}
    raise CompteDansUneAutreOrg(phrase_compte_ailleurs(
        {"channel": provider, "account_id": account_id,
         "account_name": ligne.get("account_name"), "org_id": ligne.get("org_id")},
        adoptable=account_id in adoptables))


def _refus_autrui(provider: str, comment: str) -> ValueError:
    return ValueError(
        f"The {provider.title()} account named by {comment} is neither yours nor an account "
        "another member of this org opened to you — or it is no longer operable. List the "
        "operable identities with oto_identity(op='list').")


def resolve_operated_account_id(sub: str, provider: str) -> str | None:
    """Unipile account operated by `sub` on this channel (THE resolution point).

    **Named by the call** — `_account=<account_id>` or `_instance=` (ephemeral, no state
    written): the sub's OWN account operable HERE (linked to the context org, #1342), or
    ANOTHER member's account of the org at the level its rules give (`comptes_heberges.autrui_nomme`).
    A name that serves nothing RAISES — never a silent fallback to another identity
    (a message sent under the wrong identity is irreversible); named
    `account_in_other_org` when it is the sub's own account linked to another org.

    Unnamed: the sub's own account of the context org; without one, another person's
    account a rule lends her (`comptes_heberges.prete_sans_nom`, step 5 of #1499: the
    echo says who lends it); else None."""
    from .. import session_org
    pin = session_org.current_call_account()
    if pin:
        if pin in _own_account_ids(sub, provider):
            return pin
        autrui = comptes_heberges.autrui_nomme(sub, provider, account_id=pin)
        if autrui:
            return autrui
        refuser_si_compte_dans_une_autre_org(sub, provider, pin)
        raise _refus_autrui(provider, "`_account=`")
    ref = comptes_heberges.ref_du_canal(sub, provider)
    if ref is not None:
        from .. import access
        if ref.level == "member" and ref.sub == sub and ref.org_id == access.current_org(sub):
            return _own_unipile_account_id(sub, provider)
        autrui = comptes_heberges.autrui_nomme(sub, provider, ref=ref)
        if autrui:
            return autrui
        own = access.comptes_des_lignes(
            "unipile", [("member", f"{access.current_org(sub)}:{sub}",
                         comptes_heberges.compte_du_canal(provider))])
        if ref.level == "inst" and any(c.instance_id == ref.instance_id
                                       for c in own.values()):
            return _own_unipile_account_id(sub, provider)
        raise _refus_autrui(provider, "`_instance=`")
    return (_own_unipile_account_id(sub, provider)
            or comptes_heberges.prete_sans_nom(sub, provider))


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
    out = []
    cli = _unipile_client(sub)
    # Live status of hosted accounts (platform key), resolved at most once and
    # only if a non-BYO account requires it (#201). Fail-soft → "unknown".
    _live: dict = {}
    _propres: dict = {}

    def _propre(ch: str | None) -> str | None:
        """The sub's own account of this channel in the context org — what a call
        without a name operates (`is_default`)."""
        if not ch:
            return None
        if ch not in _propres:
            _propres[ch] = _own_unipile_account_id(sub, ch)
        return _propres[ch]

    async def _live_status(account_id: str) -> str:
        if "map" not in _live:
            _live["map"] = await _unipile_live_status_map(sub)
        return _live["map"].get(account_id) or "ok"

    def _statut_mesure(account_id: str) -> bool:
        """Did the probe ANSWER for this account?

        oto#42, rule 1: a value we couldn't establish is never rendered
        by its default — and "ok" is the worst of defaults, it asserts that it
        works. Since 10/10/2026 (signal 1360): an unmeasured status is served as
        `unknown`, never as the « ok » default — an agent that reads `status` alone
        took a dead account for a live one while every call answered 500."""
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
                "is_default": bool(ch) and a.get("id") == _propre(ch),
                "channel": ch,
            })
    else:
        # Resale (platform key / hosted-auth): the OWN accounts connected
        # IN THE CONTEXT ORG. Always listed — a connected account MUST appear
        # (feedback #132). Org filter = member scope ADR 0033 B4, aligned with
        # `status_for` and the call resolution (`get_unipile_account_id`).
        accounts = db.list_unipile_accounts(sub)
        if accounts:  # org resolved only if there is something to filter
            from .. import access
            org = access.current_org(sub)
            accounts = [a for a in accounts if a.get("org_id") == org]
        for a in accounts:
            out.append({
                "id": a["account_id"],
                "label": a.get("account_name") or a["account_id"],
                **(await _statut(a["account_id"], _live_status, _statut_mesure)),
                "is_default": a["account_id"] == _propre(a["provider"]),
                "channel": a["provider"],
            })
    # The accounts OTHER members of the org opened to the caller by their rules (#1499):
    # listed with `level` (what their rules give) and `owner`, operable NAMED
    # (`_account=<id>`) — never the default; unnamed only without an account of the
    # caller's own on the channel (`comptes_heberges.prete_sans_nom`). A shared BYO key
    # may already list one:
    # its entry is annotated rather than duplicated.
    seen = {i["id"]: i for i in out}
    # Off-loop: the rules and the accounts are read in the database (#867).
    for ident in await asyncio.to_thread(comptes_heberges.autrui_listes, sub, canal):
        existing = seen.get(ident["id"])
        if existing is not None:
            existing.update(level=ident["level"], owner=ident["owner"],
                            instance_id=ident["instance_id"], is_default=False)
            continue
        ident.update(await _statut(ident["id"], _live_status, _statut_mesure))
        out.append(ident)
    if canal:
        # An account whose channel is UNKNOWN (`channel=None` — seen in BYO when
        # Unipile doesn't return `type`) attaches to no channel card: silencing it
        # here is better than making it appear under all six.
        _c = canal.upper()
        out = [i for i in out if (i.get("channel") or "").upper() == _c]
    return out


async def _statut(account_id: str, live_status, mesure) -> dict:
    """The `status` of a hosted account, `unknown` (with its hint) when the probe did
    not answer for it — never the « ok » default."""
    statut = await live_status(account_id)
    if mesure(account_id):
        return {"status": statut}
    return {"status": "unknown", "status_measured": False,
            "status_hint": ("the liveness probe did not answer for this account: its state "
                            "is UNKNOWN, not ok — a dead account looks exactly like this. "
                            "Retry to measure, or call the channel's tool and read its "
                            "error.")}


async def _unipile_select(sub: str, identity_id: str, canal: str | None = None) -> dict:
    """Chooses the operated identity among the sub's OWN accounts (another person's
    account is never a default: named, `_account=`, or as the last resort of step 5).
    `canal` (a channel
    connector's card) = guard: you don't switch your Telegram identity from the WhatsApp
    card. The REAL channel is the ACCOUNT's, never the one we assume."""
    from .. import db

    def _exige_canal(trouve: str | None) -> None:
        """Refuses BEFORE writing if the account is not of the card's channel."""
        if canal and (trouve or "").upper() != canal.upper():
            raise ValueError(
                f"This account is a {(trouve or 'unknown').title()} account: it can't be "
                f"chosen from the {canal.title()} card. Use the card of "
                "its channel.")
    # 1) Another member's account: never a default — say how it serves.
    for ch in ([canal.upper()] if canal else comptes_heberges.canaux()):
        autres = await asyncio.to_thread(comptes_heberges.comptes_d_autrui_du_canal, sub, ch)
        if any(a["account_id"] == identity_id for a in autres):
            raise ValueError(
                f"`{identity_id}` is another member's {ch.title()} account: it is never "
                f"your default — name it on each call with `_account={identity_id}`.")
    # 2) An OWN account already linked: it is the one a call operates, nothing to write.
    own = next((a for a in db.list_unipile_accounts(sub)
                if a["account_id"] == identity_id), None)
    if own:
        _exige_canal(own["provider"])
        return {"id": identity_id, "channel": own["provider"], "is_default": True}
    # 3) BYO path: choose an account of ITS key (switches the connection).
    cli = _unipile_client(sub)
    if cli is None:
        raise ValueError("Account selection unavailable (platform key — use "
                         "the hosted connection).")
    # oto-backend#867 — an Unipile outage/slowness here must be stated, not confused with
    # an unknown id (`unknown_identity`, 404) — the capable layer (`_set_default`)
    # distinguishes this `RuntimeError` from a `ValueError`.
    try:
        accounts = await _call_unipile(cli.list_accounts)
    except Exception as e:
        raise RuntimeError(f"Unipile did not answer when choosing this account: {e}") from e
    match = next((a for a in accounts if a.get("id") == identity_id), None)
    if match is None:  # anti-binding: the id MUST exist on the key
        raise ValueError(f"Unknown Unipile account on this key: {identity_id}")
    ch = (match.get("type") or "LINKEDIN").upper()
    _exige_canal(ch)
    # Member scope (ADR 0033 B4): the binding applies in the context org. BYO →
    # not a platform seat (platform_seat=False), consistent with unipile_connect.
    from .. import access
    org = access.current_org(sub)
    if org is None:
        raise ValueError("No context org — unable to attach the account.")
    db.set_unipile_account(sub, identity_id, match.get("name"), org_id=org,
                           provider=ch, platform_seat=False)
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
        out.append(entry)
    if scope == "member":
        out += _hors_org(sub, connector, None, {i["id"] for i in out})
    return out


def _keyed_select(sub: str, connector: str, identity_id: str, scope: str = "member") -> dict:
    from .. import credentials_store
    ent = keyed_entity(sub, scope)
    if ent is None:
        raise ValueError("No context org/team — unable to choose an account.")
    accounts = [r["account"] for r in credentials_store.list_accounts(ent[0], ent[1], connector)]
    if identity_id not in accounts:
        raise ValueError(f"Unknown account `{identity_id}` for {connector}.")
    # UNIQUE default of the tier, with its author and date (D3, #1499): the chosen row
    # gets `is_default` + `default_set_by`/`default_set_at`, the others lose the mark.
    from ..db import regles_de_comptes
    from ..db._conn import _connect
    with _connect() as conn:
        regles_de_comptes.poser_le_defaut(conn, ent[0], ent[1], connector, identity_id,
                                          par=sub)
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


def _microsoft_list(sub: str, service: "str | None") -> list[dict]:
    """The member's Microsoft accounts that AUTHORIZED `service` (all of them for the
    carrier, `service=None`) — same shape as the generic keyed backend, filtered like a
    Google service: `oto_identity(connector='sharepoint')` must not offer an account that
    `sharepoint_file` will refuse. An account that is not hers (her team's, her org's, a
    peer's) carries the `level` its rules give her and its `owner` (#1499): operable
    through `_account=`, never her default."""
    from ..auth import microsoft as ms_auth
    comptes = ms_auth.avec_noms(ms_auth.accounts_for(sub, service))

    def _cle(c: dict) -> "tuple | None":
        if c.get("shared"):
            return (_TYPE_DU_PALIER[c["shared"]], str(c["shared_owner"]["id"]), c["account"])
        return None
    cles = {i: k for i, k in ((i, _cle(c)) for i, c in enumerate(comptes)) if k}
    niveaux = _niveaux(sub, "microsoft", service, list(cles.values()))
    out = []
    for i, c in enumerate(comptes):
        meta = c.get("meta") or {}
        ident = {"id": c["account"], "label": meta.get("label") or c["account"],
                 "status": _oauth_status(meta), "is_default": bool(meta.get("is_default")),
                 "channel": None}
        cle = cles.get(i)
        if cle is not None:
            ident.update(is_default=False, level=niveaux[cle],
                         owner={"type": cle[0], "id": cle[1],
                                **({"name": c["shared_owner"].get("name")}
                                   if c.get("shared") else {})})
        out.append(ident)
    return out + _hors_org(sub, "microsoft", service, {i["id"] for i in out})


def _register_microsoft_services() -> None:
    """One identity backend per Microsoft SERVICE: the accounts of the `microsoft`
    carrier that authorized it; the default is the carrier's (one default per person,
    shared by the services, as for Google). Registered BEFORE the generic keyed backend —
    which would otherwise take these multi-account connectors for vault keys under their
    name (no row lives there). The carrier itself goes through the generic backend
    (default, rename, the org and team tiers) except its member list, which says what a
    call reaches (`_register_microsoft_carrier`). Population derived from the registry."""
    from .. import providers
    for con in providers._REGISTRY_LIST:
        if con.credential_of != "microsoft":
            continue
        _LISTERS[con.name] = (
            lambda sub, scope="member", _s=con.name: _microsoft_list(sub, _s))
        _SELECTORS[con.name] = (
            lambda sub, iid, scope="member": _keyed_select(sub, "microsoft", iid))


def _register_microsoft_carrier() -> None:
    """The carrier's member list = the one of its services, unfiltered: her accounts,
    her team's, her org's and her peers' (with their `level`) — the generic keyed list
    only knows her own rows. Its org and team lists stay the generic ones."""
    keyed = _LISTERS["microsoft"]
    _LISTERS["microsoft"] = lambda sub, scope="member": (
        _microsoft_list(sub, None) if scope == "member" else keyed(sub, scope))


_register_google_services()
_register_microsoft_services()
_register_keyed_multi_account()
_register_microsoft_carrier()
_register_hosted_channels()
