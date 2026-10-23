"""HubSpot — the one-click "Connect HubSpot" (OAuth), next to the pasted key.

**Why.** The pasted key (a service key, or a legacy private app's token) makes the
customer tick about fifteen scopes by hand, and they get them wrong: most of
`connectors/docs/hubspot.md` and all of `tools/hubspot_scopes.py` exist to recover
from a missing ticket, list or pipeline scope. Here **oto asks for the scopes** in the
authorization URL — the same reason Zoho gained its server-based mode.

**The two shapes live on the SAME vault row** (connector `hubspot`, `secret_kind
api_key`, one secret):

- pasted: the secret is the Bearer token itself, `meta` carries nothing of ours;
- OAuth (this module): the secret is the **refresh token**, `meta.auth = "oauth"`
  with the portal (`hub_id`, `hub_domain`), who consented (`user`) and the granted
  `scopes`. Pasting a key over it rewrites `meta` (the upsert replaces it), so the row
  goes back to the pasted shape without anything to clean.

`bearer()` is the ONE way the HubSpot tools get their token: the pasted key as is, or
an access token minted from the refresh token (30 min, cached until a minute before
it expires). `HubSpotClient` (oto-core) still receives a plain Bearer — it does not
know which shape it is.

**Two apps, chosen by the level the user connects at** (Developer Platform
projects, `platformVersion 2026.09`). HubSpot fixes `isUserLevel` when an app is
created, and both behaviours are wanted:

- **personal** (`member`) → the USER-LEVEL app: each HubSpot user installs it for
  themselves, every call runs as them with their HubSpot permissions, and the records
  oto creates through it default to them as owner (`default_owner`);
- **org / team** (`org`, `group`) → the ACCOUNT-LEVEL app: installed once per portal
  by an admin, shared like a pasted key.

Their `client_id` / `client_secret` are PLATFORM `connector_settings`, set by the
operator — the `meta_ads` pattern, not the tenant (ADR 0052): `client_id` /
`client_secret` for the account-level app, `user_client_id` / `user_client_secret`
for the user-level one. A level whose app is not set keeps the form alone
(`app_ready_scopes`). The row says which app minted it (`meta.app`): a refresh token
only renews with the client that issued it.

**Endpoints are DATED** (`/oauth/2026-09/…`): HubSpot refuses new Marketplace
listings on the `/oauth/v1` ones. Refresh-token rotation is not documented: whatever
`refresh_token` a refresh returns is written back, conditionally (`_store_rotation`).
Never refreshed inside the callback — the Salesforce lesson (31/07): a probe that
consumes the token it verifies, on a path that cannot write the new one back, kills it.
"""
from __future__ import annotations

import contextvars
import hashlib
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

import requests

from .. import credentials_store
from ..connectors import flow as connector_flow
from . import flow as oauth_flow

logger = logging.getLogger(__name__)

CONNECTOR = "hubspot"
AUTH_OAUTH = "oauth"   # `meta.auth` of a row this module wrote

_AUD = "hubspot"
CALLBACK_PATH = "/api/hubspot/oauth/callback"

_API = "https://api.hubapi.com"
OAUTH_VERSION = "2026-09"
AUTHORIZE_URL = "https://app.hubspot.com/oauth/authorize"
TOKEN_URL = f"{_API}/oauth/{OAUTH_VERSION}/token"
INTROSPECT_URL = f"{_API}/oauth/{OAUTH_VERSION}/token/introspect"

_TIMEOUT = 20

# --- scopes -------------------------------------------------------------------
#
# Derived from `tools/hubspot_scopes.SCOPES`, the reference the probe and the refusals
# already read: the consent asks for what the tools need, by the same names.
# REQUIRED = a portal that cannot grant one cannot install. OPTIONAL (`optional_scope`)
# = HubSpot drops it silently where the portal lacks the tool: tickets (Service Hub
# objects, and a legacy `tickets` scope HubSpot is migrating away from) and the events
# API (`business-intelligence`, Professional/Enterprise). ⚠️ This list must be the one
# ticked in the app's `app-hsmeta.json` (`docs/hubspot-app/app-hsmeta.json`, held equal
# by `test_hubspot_oauth.py`): a required scope the app does not declare breaks the
# authorization page, and changing a REQUIRED scope after the first install makes every
# portal reconnect.


def _from(families: tuple[str, ...], *, write: bool) -> tuple[str, ...]:
    from ..tools.hubspot_scopes import SCOPES
    out: list[str] = []
    for fam in families:
        read, wr = SCOPES[fam]
        for s in (read[0],) + ((wr[0],) if write and wr else ()):
            if s not in out:
                out.append(s)
    return tuple(out)


#: The two apps (`meta.app`, the `kind` of the signed state).
APP_ACCOUNT = "account"
APP_USER = "user"
APPS = (APP_ACCOUNT, APP_USER)

_SCHEMAS_READ = ("crm.schemas.contacts.read", "crm.schemas.companies.read",
                 "crm.schemas.deals.read")
_TICKETS = ("crm.objects.tickets.read", "crm.objects.tickets.write",
            "crm.schemas.tickets.read")
_CUSTOM = ("crm.objects.custom.read", "crm.objects.custom.write",
           "crm.schemas.custom.read")

#: Per app: (required, optional). The personal app REQUIRES less: HubSpot lets a user
#: install it only if their own permissions cover every required scope, and a sales
#: rep often has no list rights — lists are optional there. Field writes
#: (`hubspot_property` create/update/delete) are an admin's job: org app only.
SCOPES_BY_APP: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    APP_ACCOUNT: (
        (*_from(("contacts", "companies", "deals", "lists"), write=True),
         *_from(("owners",), write=False), *_SCHEMAS_READ),
        (*_TICKETS, "business-intelligence",
         "crm.schemas.contacts.write", "crm.schemas.companies.write",
         "crm.schemas.deals.write", *_CUSTOM),
    ),
    APP_USER: (
        (*_from(("contacts", "companies", "deals"), write=True),
         *_from(("owners",), write=False), *_SCHEMAS_READ),
        (*_from(("lists",), write=True), *_TICKETS, "business-intelligence", *_CUSTOM),
    ),
}


def app_for_scope(scope: str) -> str:
    """The app a level connects with: personal → user-level, org/team → account-level."""
    return APP_USER if scope == "member" else APP_ACCOUNT


class HubSpotOAuthError(RuntimeError):
    """Acquisition or renewal failure — message WITHOUT secrets (incident #284)."""


class HubSpotReauthRequired(HubSpotOAuthError):
    """HubSpot no longer accepts this connection's refresh token: reconnect."""


# --- the app (client_id / client_secret) -------------------------------------

_SETTINGS = {APP_ACCOUNT: ("client_id", "client_secret"),
             APP_USER: ("user_client_id", "user_client_secret")}
_ALL_SETTINGS = tuple(k for keys in _SETTINGS.values() for k in keys)
_COMMAND = ('oto_admin_connector_setting(op="set", connector="hubspot", '
            'key="<{keys}>", value="<value>")')
_APP_LABEL = {APP_ACCOUNT: "org", APP_USER: "personal"}


def _settings() -> dict:
    """COLD read (`meta_ads._reglages`): start of a flow, return of a consent, and a
    token renewal — once per half hour and per row, never per tool call."""
    from ..db import connector_settings as store

    return {r["key"]: (r["value"] or "").strip()
            for r in store.list_connector_settings()
            if r["scope_type"] == "platform" and r["connector"] == CONNECTOR
            and r["key"] in _ALL_SETTINGS}


def missing_settings(kind: str = APP_ACCOUNT, poses: Optional[dict] = None) -> list[str]:
    poses = _settings() if poses is None else poses
    return [k for k in _SETTINGS[kind] if not poses.get(k)]


def app(kind: str = APP_ACCOUNT) -> tuple[str, str]:
    """`(client_id, client_secret)` of one of the instance's HubSpot apps, or a refusal
    that NAMES what is missing and who sets it."""
    poses = _settings()
    missing = missing_settings(kind, poses)
    if missing:
        raise HubSpotOAuthError(
            f"The {_APP_LABEL[kind]} HubSpot app is not configured on this instance: "
            f"missing {', '.join(missing)}. Nothing to do on your account — the "
            f"instance operator sets them with "
            f"{_COMMAND.format(keys='|'.join(_SETTINGS[kind]))}. Meanwhile, a service "
            "key pasted on the connector card works.")
    id_key, secret_key = _SETTINGS[kind]
    return poses[id_key], poses[secret_key]


def app_ready_scopes(sub: str) -> list[str]:
    """The levels whose app is set — what the card offers "Connect" on. ONE settings
    read for both apps."""
    del sub
    try:
        poses = _settings()
    except Exception as e:  # noqa: BLE001 — a settings outage must not break /api/me
        logger.warning("hubspot: app settings unreadable (%s)", type(e).__name__)
        return []
    out: list[str] = []
    if not missing_settings(APP_USER, poses):
        out.append("member")
    if not missing_settings(APP_ACCOUNT, poses):
        out += ["org", "group"]
    return out


def app_ready(sub: str) -> bool:
    """Any level ready — the single answer of fronts that predate `app_ready_scopes`."""
    return bool(app_ready_scopes(sub))


# --- the signed state --------------------------------------------------------

def make_state(sub: str, org_id: int, scope: str, group_id: Optional[int] = None,
               return_app: str = "") -> str:
    """Bound to the `hubspot` audience and single-use (`jti`, `consume_state`): the
    callback WRITES a credential, a replayed state must not write it twice."""
    payload = {"sub": sub, "org": org_id, "scope": scope, "app": return_app,
               "kind": app_for_scope(scope), "jti": oauth_flow.new_jti()}
    if group_id is not None:
        payload["group"] = group_id
    return oauth_flow.sign_state(_AUD, payload)


def read_state(state: Optional[str]) -> Optional[dict]:
    """The payload if valid, unexpired and issued FOR this flow — else None. A
    `group` scope without its team is refused (we do not guess where to write)."""
    data = oauth_flow.read_state(_AUD, state)
    if not data:
        return None
    if (not isinstance(data.get("sub"), str) or not isinstance(data.get("org"), int)
            or data.get("scope") not in ("member", "org", "group")):
        return None
    if data["scope"] == "group" and not isinstance(data.get("group"), int):
        return None
    # The app is the level's, never chosen apart: a state whose app does not match
    # its level would store a user-level grant as the org's (or the reverse).
    if data.get("kind") != app_for_scope(data["scope"]):
        return None
    if not isinstance(data.get("app"), str):
        data["app"] = ""
    return data


def can_write(sub: str, org_id: int, scope: str, group_id: Optional[int]) -> bool:
    """The right to write at `scope` — checked at start AND again at the callback
    (the state lives 10 min, a role can be lost in between: ADR 0038)."""
    from .. import roles
    if scope == "org":
        return roles.is_org_admin(sub, org_id)
    if scope == "group":
        return group_id is not None and roles.can_admin_group(sub, group_id)
    return True


# --- flow start --------------------------------------------------------------

def build_auth_url(sub: str, scope: str = "member", return_app: str = "") -> str:
    """HubSpot's consent URL. Raises `ValueError` (bad scope), `PermissionError`
    (not admin of the requested level) or `HubSpotOAuthError` (app not configured)."""
    from .. import access

    if scope not in ("member", "org", "group"):
        raise ValueError(f"invalid scope: {scope!r} (expected 'member', 'org' or 'group')")
    org_id = access.current_org(sub)
    if org_id is None:
        raise HubSpotOAuthError(
            "No org in context — cannot scope the HubSpot connection. Sign in again "
            "and retry.")
    group_id = access.current_group(sub) if scope == "group" else None
    if scope == "group" and group_id is None:
        raise HubSpotOAuthError(
            "No active team — select one to connect HubSpot for your team.")
    if not can_write(sub, org_id, scope, group_id):
        who = "an org admin" if scope == "org" else "a team lead"
        raise PermissionError(
            f"Only {who} can connect HubSpot on behalf of the whole "
            f"{'org' if scope == 'org' else 'team'}.")
    kind = app_for_scope(scope)
    client_id, _ = app(kind)
    required, optional = SCOPES_BY_APP[kind]
    params = {
        "client_id": client_id,
        "redirect_uri": oauth_flow.redirect_uri(CALLBACK_PATH),
        "scope": " ".join(required),
        "optional_scope": " ".join(optional),
        "state": make_state(sub, org_id, scope, group_id,
                            oauth_flow.resolve_return_app(return_app)),
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _start_flow(ctx, values: dict) -> "connector_flow.FlowStart":
    from ..capabilities._types import AuthzDenied

    values = values or {}
    scope = values.get("scope") or "member"
    try:
        url = build_auth_url(ctx.sub, scope, values.get("app") or "")
    except ValueError as e:
        raise AuthzDenied(400, "invalid_scope_param", str(e))
    except PermissionError as e:
        raise AuthzDenied(403, "org_admin_required", str(e))
    except HubSpotOAuthError as e:
        raise AuthzDenied(503, "oauth_misconfigured", str(e))
    return connector_flow.FlowStart(auth_url=url, details={
        "scope": scope, "app": app_for_scope(scope)})


# The scope (member | org | group) is DEDUCED from the surface and passed by the caller
# (`values["scope"]`), like Salesforce: no "for whom?" parameter.
connector_flow.declare(
    CONNECTOR,
    start=_start_flow,
    label="Connect HubSpot",
    callback_path=CALLBACK_PATH,
    app_ready=app_ready,
    app_ready_scopes=app_ready_scopes,
)


# --- the return: exchange, identify, store ----------------------------------

def _post(url: str, data: dict) -> requests.Response:
    """Form-encoded body, never a query string: secrets stay out of URLs (#284)."""
    return requests.post(url, data=data, timeout=_TIMEOUT)


def exchange_code(code: str, kind: str = APP_ACCOUNT) -> dict:
    client_id, client_secret = app(kind)
    try:
        return oauth_flow.exchange_code(
            TOKEN_URL, code=code, client_id=client_id, client_secret=client_secret,
            redirect=oauth_flow.redirect_uri(CALLBACK_PATH))
    except oauth_flow.OAuthFlowError as e:
        raise HubSpotOAuthError(str(e)) from e


def introspect(token: str, hint: str = "refresh_token", kind: str = APP_ACCOUNT) -> dict:
    """What HubSpot says of a token: `hub_id`, `hub_domain`, `user`, `scopes`…
    Introspecting the REFRESH token proves the grant is alive and names the portal
    WITHOUT minting an access token — nothing rotates."""
    client_id, client_secret = app(kind)
    r = _post(INTROSPECT_URL, {"client_id": client_id, "client_secret": client_secret,
                               "token": token, "token_type_hint": hint})
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400 or not isinstance(body, dict):
        msg = body.get("message") if isinstance(body, dict) else None
        raise HubSpotOAuthError(
            f"HubSpot refused the token introspection (HTTP {r.status_code})"
            + (f": {str(msg)[:200]}" if msg else "."))
    if body.get("active") is False:
        raise HubSpotReauthRequired(
            "HubSpot says this connection is no longer active (uninstalled or "
            "revoked): reconnect HubSpot from the connector card.")
    return body


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connection_meta(info: dict, sub: str, kind: str = APP_ACCOUNT) -> dict:
    scopes = info.get("scopes") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    return {"auth": AUTH_OAUTH,
            "app": kind,
            "hub_id": info.get("hub_id"),
            "hub_domain": info.get("hub_domain"),
            "user": info.get("user"),
            "scopes": " ".join(sorted(str(s) for s in scopes)),
            "installed_by": sub,
            "connected_at": _iso_now()}


def owner_id_of(access_token: str, email: Optional[str]) -> Optional[str]:
    """The HubSpot OWNER id of the user who consented, by their email — what records
    created through a personal connection get as `hubspot_owner_id`. `None` when the
    user has no owner record (no seat) or the read fails: the connection still works,
    records simply stay unassigned."""
    if not email or not access_token:
        return None
    try:
        r = requests.get(f"{_API}/crm/v3/owners",
                         params={"email": email, "limit": 1},
                         headers={"Authorization": f"Bearer {access_token}"},
                         timeout=_TIMEOUT)
        results = (r.json() or {}).get("results") if r.status_code < 400 else None
    except (requests.RequestException, ValueError) as e:
        logger.warning("hubspot: owner lookup failed (%s)", type(e).__name__)
        return None
    if not results:
        logger.info("hubspot: no owner record for the consenting user")
        return None
    owner = results[0].get("id")
    return str(owner) if owner else None


def persist(sub: str, org_id: int, scope: str, group_id: Optional[int],
            tokens: dict) -> dict:
    """Synchronous (HTTP + DB): the callback runs it off the loop. Identifies the
    portal (introspection), then writes the row at the requested level. Returns what
    the log may say — never a token."""
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise HubSpotOAuthError("HubSpot did not return a refresh token: restart the "
                                "connection.")
    kind = app_for_scope(scope)
    info = introspect(refresh, kind=kind)
    meta = connection_meta(info, sub, kind)
    if kind == APP_USER:
        # With the access token the exchange gave — no refresh in the callback.
        meta["owner_id"] = owner_id_of(tokens.get("access_token") or "", info.get("user"))
    entity_type, entity_id = credentials_store.entity_for_scope(
        scope, org_id, sub, group_id)
    credentials_store.set_credential(entity_type, entity_id, CONNECTOR, refresh,
                                     set_by=sub, meta=meta)
    # Seed the cache with the access token the exchange already gave: the first tool
    # call does not pay a refresh.
    if tokens.get("access_token") and tokens.get("expires_in"):
        _keep((entity_type, entity_id, ""), refresh, tokens["access_token"],
              int(tokens["expires_in"]))
    return {"hub_id": meta["hub_id"], "scope": scope, "app": kind}


# --- the Bearer the tools use ------------------------------------------------

_TOKENS: dict[str, tuple[str, float]] = {}
_LOCK = threading.Lock()


def _key(row: tuple, refresh_token: str) -> str:
    """The row and its refresh token, hashed — a reconnection changes the key."""
    return hashlib.sha256("|".join((*row, refresh_token)).encode()).hexdigest()


def _keep(row: tuple, refresh_token: str, access_token: str, expires_in: int) -> None:
    with _LOCK:
        _TOKENS[_key(row, refresh_token)] = (access_token, time.time() + expires_in)


def _store_rotation(row: tuple, read: str, new: str) -> None:
    """The refresh token rotated: write the new one on THIS row, its `meta` passed back
    (the upsert would wipe it). Conditional: a concurrent renewal that already wrote a
    newer value keeps it."""
    entity_type, entity_id, account = row
    current = credentials_store.get_credential_with_meta(
        entity_type, entity_id, CONNECTOR, account=account)
    if not current or current.get("secret") != read:
        return
    credentials_store.set_credential(entity_type, entity_id, CONNECTOR, new,
                                     set_by=current.get("set_by"),
                                     meta=current.get("meta") or {}, account=account)


def refresh(refresh_token: str, kind: str = APP_ACCOUNT) -> dict:
    """`refresh_token` → `{access_token, expires_in, refresh_token?}`. A dead grant
    (`invalid_grant`) raises `HubSpotReauthRequired`; a configuration fault (wrong
    client secret…) raises `HubSpotOAuthError` and destroys nothing (`grant_is_dead`).
    `kind` = the app that issued the token: another app's client cannot renew it."""
    client_id, client_secret = app(kind)
    r = _post(TOKEN_URL, {"grant_type": "refresh_token", "client_id": client_id,
                          "client_secret": client_secret, "refresh_token": refresh_token})
    if r.status_code < 400:
        try:
            body = r.json()
        except ValueError:
            body = {}
        if body.get("access_token"):
            return body
    text = (r.text or "")[:300]
    if oauth_flow.grant_is_dead(r.status_code, text):
        raise HubSpotReauthRequired(
            "HubSpot no longer accepts this connection (the app was uninstalled from "
            "the portal, or the authorization was revoked). Reconnect HubSpot from the "
            "connector card. Nothing was done.")
    raise HubSpotOAuthError(
        f"HubSpot refused to renew the connection's token (HTTP {r.status_code}). "
        "This is usually the instance's HubSpot app configuration, not your account.")


def access_token_for(rc) -> str:
    """A valid access token for the resolved OAuth row, renewed when it expires in
    less than a minute. A dead grant marks THIS row "to reconnect" on its card and
    raises."""
    from ..connectors import health as connector_health

    row = (rc.entity_type or "", rc.entity_id or "", rc.account or "")
    refresh_token = rc.key
    with _LOCK:
        cached = _TOKENS.get(_key(row, refresh_token))
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    try:
        grant = refresh(refresh_token, app_of(rc))
    except HubSpotReauthRequired as e:
        connector_health.mark_rejected(rc.entity_type, rc.entity_id, CONNECTOR,
                                       rc.account or "", str(e),
                                       verdict=connector_health.UNAUTHORIZED)
        raise
    new_refresh = grant.get("refresh_token") or refresh_token
    if new_refresh != refresh_token:
        _store_rotation(row, refresh_token, new_refresh)
    _keep(row, new_refresh, grant["access_token"], int(grant.get("expires_in") or 1800))
    return grant["access_token"]


# --- removal: uninstall the app from the portal --------------------------------

UNINSTALL_URL = f"{_API}/appinstalls/v3/external-install"


def _uninstall_call(access_token: str) -> requests.Response:
    return requests.delete(UNINSTALL_URL,
                           headers={"Authorization": f"Bearer {access_token}"},
                           timeout=_TIMEOUT)


def _install_held_elsewhere(hub_id, kind: str, row: tuple) -> bool:
    """Does ANOTHER vault row hold this app in this portal?

    HubSpot's uninstall removes the app from the WHOLE portal (and emails every Super
    Admin of it), the user-level app included: a member removing their personal
    connection would cut every colleague connected through the same app. So the key is
    (`hub_id`, app), whoever connected. When in doubt (unreadable vault), YES: the row
    goes anyway, and one uninstall too many breaks live connections."""
    try:
        holders = credentials_store.holders_by_meta(CONNECTOR, "hub_id", str(hub_id))
    # noqa: SILENT — when in doubt we do not uninstall (the row goes anyway)
    except Exception:
        return True
    for h in holders:
        if (h["entity_type"], str(h["entity_id"]), h.get("account") or "") == row:
            continue
        meta = h.get("meta") or {}
        if meta.get("auth") == AUTH_OAUTH and app_of(meta) == kind:
            return True
    return False


def uninstall_before_clear(entity_type: str, entity_id: str, account: str = "") -> None:
    """Removing an OAuth row also uninstalls the app from its portal, unless another
    row holds the same install. Best-effort, like Google's revoke: a refusal, a dead
    grant or an unreachable HubSpot never stops the removal — the row goes in any case.
    A pasted key has nothing to uninstall. Synchronous (HTTP), outside any transaction:
    called by the three removal capabilities (member, org, team) just before they clear
    the row."""
    row = (entity_type, str(entity_id), account or "")
    try:
        current = credentials_store.get_credential_with_meta(
            entity_type, entity_id, CONNECTOR, account=account)
    # noqa: SILENT — undecryptable row: removed without uninstalling
    except Exception:
        logger.warning("hubspot: row unreadable, removed without uninstalling the app")
        return
    meta = (current or {}).get("meta") or {}
    if not current or meta.get("auth") != AUTH_OAUTH or not current.get("secret"):
        return
    with _LOCK:
        _TOKENS.pop(_key(row, current["secret"]), None)
    kind, hub_id = app_of(meta), meta.get("hub_id")
    if hub_id is None:
        logger.info("hubspot: OAuth row removed without uninstalling: no portal on it")
        return
    if _install_held_elsewhere(hub_id, kind, row):
        logger.info("hubspot: OAuth row removed without uninstalling (hub_id=%s app=%s): "
                    "another row holds the same install", hub_id, kind)
        return
    try:
        r = _uninstall_call(refresh(current["secret"], kind)["access_token"])
    except (HubSpotOAuthError, requests.RequestException) as e:
        logger.warning("hubspot: uninstall skipped (hub_id=%s app=%s): %s",
                       hub_id, kind, type(e).__name__)
        return
    if r.status_code >= 400:
        logger.warning("hubspot: uninstall refused (hub_id=%s app=%s): HTTP %s",
                       hub_id, kind, r.status_code)
    else:
        logger.info("hubspot: app uninstalled from portal (hub_id=%s app=%s)", hub_id, kind)


def is_oauth(rc) -> bool:
    return (rc.config or {}).get("auth") == AUTH_OAUTH


def app_of(rc_or_meta) -> str:
    """The app that minted an OAuth row (`meta.app`). Absent = account-level: the
    only app before the personal one existed."""
    meta = rc_or_meta if isinstance(rc_or_meta, dict) else (rc_or_meta.config or {})
    kind = meta.get("app")
    return kind if kind in APPS else APP_ACCOUNT


#: Which shape served the current call — read by the scope refusal
#: (`hubspot_scopes._scope_to_tick`): a missing scope is a checkbox on a pasted key,
#: but a RECONNECTION on an OAuth connection (the consent asks for today's scopes).
_SERVED_BY: contextvars.ContextVar[str] = contextvars.ContextVar(
    "hubspot_served_by", default="")


def served_by_oauth() -> bool:
    return _SERVED_BY.get() == AUTH_OAUTH


#: The owner id of the PERSONAL connection that served the current call, "" otherwise
#: — read by `default_owner` on the create paths.
_OWNER: contextvars.ContextVar[str] = contextvars.ContextVar("hubspot_owner", default="")

#: The objects whose records carry `hubspot_owner_id` and get the default.
_OWNED = frozenset({"contacts", "companies", "deals", "tickets",
                    "0-1", "0-2", "0-3", "0-5"})


def default_owner(object_type: Optional[str], properties: dict) -> dict:
    """On a personal connection, a CREATED record defaults to the person who connected
    as owner — HubSpot assigns none through the API. The call's own owner always wins
    (even an empty one: a deliberate "unassigned"); an org connection, a pasted key or
    an update never get one. Returns the properties to send."""
    owner = _OWNER.get()
    if not owner or (object_type or "") not in _OWNED or "hubspot_owner_id" in properties:
        return properties
    return {**properties, "hubspot_owner_id": owner}


def bearer(account: Optional[str] = None) -> str:
    """The Bearer for the current call — what every HubSpot tool hands `HubSpotClient`.
    Same resolution as `access.resolve_api_key("hubspot")` (member > team > org, byo);
    an OAuth row is turned into an access token."""
    from .. import access

    rc = access.resolve_credential(CONNECTOR, want="auto", account=account)
    if not is_oauth(rc):
        _SERVED_BY.set("key")
        _OWNER.set("")
        return rc.key
    _SERVED_BY.set(AUTH_OAUTH)
    owner = (rc.config or {}).get("owner_id") if app_of(rc) == APP_USER else None
    _OWNER.set(str(owner) if owner else "")
    return access_token_for(rc)

