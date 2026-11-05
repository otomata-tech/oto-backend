"""Google OAuth — web flow, per-user, tokens persisted in SQLite.

Flow:
1. Authenticated user (Logto JWT) calls `GET /api/google/oauth/start` →
   we return a Google URL with an HMAC-signed `state` containing their `sub`.
2. User redirected to Google, consents, redirected to
   `/api/google/oauth/callback?code=…&state=…`.
3. We verify the state, exchange the code for a refresh+access token,
   persist it in the encrypted vault (`connector_credentials`, connector='google').

To use the credentials (tools side): `credentials_for(sub, account, service)`
chooses the account by the common resolution (`access.resolve_credential`:
`account`/`_account=` > project pin > single > default), transparently refreshes
if expired, returns a valid `google.oauth2.credentials.Credentials`.

Setup ops:
- Env `GOOGLE_WORKSPACE_CLIENT_ID` + `GOOGLE_WORKSPACE_CLIENT_SECRET` —
  OAuth client of type **Web application** in Google Cloud Console. The backend
  emits `{OTO_MCP_PUBLIC_URL}/api/google/oauth/callback` as redirect URI: this
  EXACT URL must appear in the client's "Authorized redirect URIs", otherwise
  Google returns "invalid request" (redirect_uri_mismatch). Since the ADR 0040
  cutover (2026-07-06) the client is shared prod + preprod → declare both:
    - `https://mcp.oto.cx/api/google/oauth/callback`    (PROD)
    - `https://mcp.oto.ninja/api/google/oauth/callback` (PREPROD)
- Google Chat (`chat.*` scopes below): in the Google Cloud project that holds
  the client, enable the Google Chat API AND configure it (Google Chat API →
  Configuration: name, avatar, description, interactive features
  disabled). Without this Chat app, every write under the user's
  identity returns 404 "Google Chat app not found"; reads do without it
  (otomata-tech/oto#190). Also applies to the project of a tenant that sets its own app.
- Env `OTO_MCP_PUBLIC_URL` (already used for Logto) — base for the
  redirect URI; locally it can be overridden to point to localhost.
- Env `OTO_MCP_OAUTH_STATE_SECRET` — HMAC secret to sign the anti-CSRF
  state (generate with `python -c 'import secrets; print(secrets.token_urlsafe(32))'`).

**A TENANT's app** (23/09/2026) — a partner who wants THEIR OWN consent screen
(their brand, their Google Cloud project, their scopes verified under their name) sets their client
as the **publisher app** of the `google` connector, in the tenants' namespace
(`editor:tenant:<slug>`, `credentials_store.tenant_app_key`): from THEIR dashboard
(`tenant_apps`, `PUT /api/admin/tenants/{slug}/apps/google`) or by the operator
(`POST /api/admin/editor-apps {connector: "google", data_center: "tenant:<slug>", …}`) —
REST only, encrypted vault, never the env. Once it is set, `app_for(sub)` serves it
to every account qualified under this tenant, and the callback goes to the first host the
tenant HOLDS (`tenancy.callback_host`, e.g. `https://<host>/api/google/oauth/callback`):
it is THIS URL that the partner declares at Google — their client only accepts their own
domains, not ours. With no app set, the tenant stays on ours and on our
callback: the previous state, byte for byte.
⚠️ **A token only refreshes with the client that issued it.** Setting, changing or removing
a tenant's app makes the tokens issued by the previous app unusable. The issuing client
is recorded on the token (`persist_token` → meta `client_id`; absent = our app, that of
all tokens from before this batch): a token from another client is refused BEFORE any network
call, account marked, "reconnect this account" — never purged. A client refused by
Google at refresh (`unauthorized_client`/`invalid_client`) is a named
`GoogleClientRejected`, not an internal error — and not a dead grant (config ≠ revocation).
⚠️ The tenant's host routes to ONE instance (prod): a consent started in
preprod with the tenant's app calls back to prod — the state is verified there with prod's
secret. Testing a tenant's app means doing it where its host lands.
"""
from __future__ import annotations

import hmac
import hashlib
import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .. import credentials_store, db
from . import flow as oauth_flow
from ..connectors import flow as connector_flow
from ..connectors import health as connector_health
from ..connectors import link as connector_link

logger = logging.getLogger(__name__)


# IDENTITY scopes — requested at EVERY consent, whatever the service: it is
# through them that the account gets NAMED (`userinfo` → email), without depending on the Gmail scope
# as before the split (a Drive-only consent has no Gmail profile to read).
# Not sensitive at Google.
IDENTITY_SCOPES = ("openid", "https://www.googleapis.com/auth/userinfo.email")

# The scopes of EACH service — one connector per service since the split of
# 2026-09-26 (`providers/google.service`): connecting Drive only asks for Drive, as an
# incremental authorization on the same account (`include_granted_scopes`). A tenant
# no longer has to get a service it does not offer verified at Google, and a person
# who only wants their calendar does not hand over their mailbox.
SERVICE_SCOPES: dict[str, tuple[str, ...]] = {
    # SENSITIVE scope → Google verification at publication, no CASA audit.
    "sheets": ("https://www.googleapis.com/auth/spreadsheets",),
    # FULL Drive (RESTRICTED) — manage ALL the user's files (not only
    # those created by oto). Also covers the datastore export (#29). Supersedes drive.file.
    "drive": ("https://www.googleapis.com/auth/drive",),
    # Gmail full surface (read/send/reply/draft/archive/trash). RESTRICTED →
    # CASA audit required if the consent screen goes to published.
    "gmail": ("https://www.googleapis.com/auth/gmail.modify",),
    # Google Tasks (read/write). SENSITIVE, not restricted.
    "tasks": ("https://www.googleapis.com/auth/tasks",),
    # Google Calendar (read/write events). SENSITIVE, not restricted.
    "calendar": ("https://www.googleapis.com/auth/calendar",),
    # Google Chat (RESTRICTED) — read spaces + read/post messages.
    "chat": ("https://www.googleapis.com/auth/chat.spaces.readonly",
             "https://www.googleapis.com/auth/chat.messages"),
    # BigQuery (SENSITIVE). NOT `bigquery.readonly`: `jobs.query`, `getQueryResults`
    # and `jobs.insert` (the dry run) do not accept it (discovery document v2,
    # rev. 20260811). The scope would allow writing: read-only is enforced by
    # the tools (dry run → `statementType` SELECT required, `tools/bigquery.py`).
    "bigquery": ("https://www.googleapis.com/auth/bigquery",),
    # Google Ads (SENSITIVE). Google publishes NO read-only scope: `adwords` would
    # allow mutating campaigns. Read-only is enforced by the oto-core client, which
    # calls only `listAccessibleCustomers`, `googleAds:search` and `googleAdsFields:search`
    # (`oto.tools.google.ads`) — no mutate endpoint is ever built.
    "google_ads": ("https://www.googleapis.com/auth/adwords",),
    # YouTube (SENSITIVE, not restricted). `youtube.force-ssl` is the only scope that
    # covers comments (`comments.insert`, `setModerationStatus`) as well as
    # `videos.update`, `thumbnails.set` and video upload (`videos.insert` accepts it,
    # so `youtube.upload` is never asked for); Analytics `reports.query` requires
    # `youtube.readonly` plus `yt-analytics.readonly`.
    "youtube": ("https://www.googleapis.com/auth/youtube.force-ssl",
                "https://www.googleapis.com/auth/youtube.readonly",
                "https://www.googleapis.com/auth/yt-analytics.readonly"),
}
SERVICES: tuple[str, ...] = tuple(SERVICE_SCOPES)
SERVICE_LABELS = {"gmail": "Gmail", "drive": "Google Drive", "sheets": "Google Sheets",
                  "calendar": "Google Calendar", "tasks": "Google Tasks",
                  "chat": "Google Chat", "bigquery": "Google BigQuery",
                  "google_ads": "Google Ads", "youtube": "YouTube"}

# What the ACCOUNT (`google`) requests under our app: the six services from before the
# split, for a dashboard with a single Google card. FROZEN at these six — a service
# added since (bigquery 2026-10-02, google_ads 2026-10-08, youtube 2026-11-04) is authorized ONLY from its own card, it is not
# silently added to the account's consent.
_CARRIER_SERVICES = ("sheets", "drive", "gmail", "tasks", "calendar", "chat")
SCOPES = [scope for svc in _CARRIER_SERVICES for scope in SERVICE_SCOPES[svc]]


def account_services() -> tuple[str, ...]:
    """The services the ACCOUNT (`google`) requests under our app, on THIS instance.

    `GOOGLE_ACCOUNT_SERVICES` narrows `_CARRIER_SERVICES`: a comma-separated subset
    (`gmail,drive,sheets,calendar`), or `none` for the identity alone — the account
    card then behaves as under a tenant's app, each service adding its scopes from its
    own card. Unset or empty = the six, the state before this setting.

    Why an instance setting: Google verifies an OAuth project for a declared list of
    scopes, and refuses an app that requests scopes outside it. An instance whose
    project is verified for fewer services than the six must not request the others
    through the account card — hiding their cards (`connector_availability`) is not
    enough, the account card would still ask for them. A value naming a service the
    account cannot carry is a configuration error: refused, never guessed."""
    raw = (os.environ.get("GOOGLE_ACCOUNT_SERVICES") or "").strip().lower()
    if not raw:
        return _CARRIER_SERVICES
    if raw == "none":
        return ()
    wanted = {s.strip() for s in raw.split(",") if s.strip()}
    if not wanted:
        raise RuntimeError(
            f"GOOGLE_ACCOUNT_SERVICES is set to {raw!r}, which names no service: "
            f"expected a comma-separated subset of {', '.join(_CARRIER_SERVICES)}, "
            "or none.")
    unknown = sorted(wanted - set(_CARRIER_SERVICES))
    if unknown:
        raise RuntimeError(
            f"GOOGLE_ACCOUNT_SERVICES names {', '.join(unknown)}: expected a subset of "
            f"{', '.join(_CARRIER_SERVICES)}, or none.")
    return tuple(s for s in _CARRIER_SERVICES if s in wanted)
# What a vault row can carry: nothing else gets in (`persist_token`),
# even if a partner's client has other scopes granted elsewhere.
KNOWN_SCOPES = (frozenset(IDENTITY_SCOPES)
                | frozenset(sc for svc in SERVICES for sc in SERVICE_SCOPES[svc]))


def services_granted(scopes) -> list[str]:
    """The services whose scopes ALL appear in `scopes` (string or list) —
    what an account has actually authorized, in service order."""
    have = set(scopes.split() if isinstance(scopes, str) else (scopes or ()))
    return [svc for svc in SERVICES if set(SERVICE_SCOPES[svc]) <= have]


def scopes_for(connector: str, app: "OAuthApp") -> list[str]:
    """The scopes THIS consent requests — the identity, then:

    - a service: its own, and nothing else;
    - the account (`google`): under OUR app, the six services from before the split
      (`_CARRIER_SERVICES`, for a single-card dashboard), narrowed by the instance's
      `GOOGLE_ACCOUNT_SERVICES` (`account_services`); under a TENANT's app,
      nothing more than the identity — a partner never requests a scope that
      their Google project does not declare, their services add them one by one.

    An unknown connector is refused: nothing here guesses a scope."""
    if connector == "google":
        base = ([sc for svc in account_services() for sc in SERVICE_SCOPES[svc]]
                if app.origin == "env" else [])
    elif connector in SERVICE_SCOPES:
        base = list(SERVICE_SCOPES[connector])
    else:
        raise RuntimeError(f"\"{connector}\" is not a known Google service.")
    return list(IDENTITY_SCOPES) + base

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_STATE_TTL = 600  # 10 min


def _client_id() -> str:
    """Our client — the env, fallback when the caller's tenant has not set its own."""
    v = os.environ.get("GOOGLE_WORKSPACE_CLIENT_ID")
    if not v:
        raise RuntimeError("GOOGLE_WORKSPACE_CLIENT_ID env var missing")
    return v


def _client_secret() -> str:
    v = os.environ.get("GOOGLE_WORKSPACE_CLIENT_SECRET")
    if not v:
        raise RuntimeError("GOOGLE_WORKSPACE_CLIENT_SECRET env var missing")
    return v


def _state_secret() -> bytes:
    v = os.environ.get("OTO_MCP_OAUTH_STATE_SECRET")
    if not v:
        raise RuntimeError("OTO_MCP_OAUTH_STATE_SECRET env var missing")
    return v.encode()


_CALLBACK_PATH = "/api/google/oauth/callback"


def _redirect_uri() -> str:
    """OUR callback — on the instance's public address, never guessed
    (`config.public_base_url()` raises, tripwire `test_url_publique_sans_repli`)."""
    return oauth_flow.redirect_uri(_CALLBACK_PATH)


@dataclass(frozen=True)
class OAuthApp:
    """The OAuth app that requests the consent FOR THIS ACCOUNT, and its exact callback.

    The three go together: the code is exchanged and the token refreshed with the client
    that requested the consent, and Google only accepts the callback byte for byte at
    THAT client. Separating them means an opaque `redirect_uri_mismatch` or
    `invalid_client` — hence a single value, resolved once per action.
    `origin` says where it comes from (`tenant:<slug>` or `env`): for the log and
    tests, never to decide."""
    client_id: str
    # Out of the `repr`: an app ends up in an assertion message, a debug log,
    # a trace — the secret has no business there.
    client_secret: str = field(repr=False)
    redirect_uri: str
    origin: str = "env"


def app_for(sub: str) -> OAuthApp:
    """The app to use for this sub: that of ITS tenant if it is set, ours otherwise.

    The tenant is read from the qualified sub (`tenancy.tenant_of`, by prefix, never by
    splitting) — derived from the token, a caller cannot claim the app of a tenant
    they do not belong to. The tenant's app is the publisher app of the connector
    `google` stored in the tenants' namespace
    (`credentials_store.tenant_app_key(slug)`), and its callback is set on the first
    host the tenant HOLDS (`tenancy.callback_host`) — never on a host it
    declared but another tenant holds: the code and the signed state would go to them.
    ⚠️ This host must ROUTE to this backend (condition of its declaration,
    `docs/tenants.md`): this cannot be verified from here.

    ⚠️ A vault error BUBBLES UP, it does not fall back to the env: otherwise a
    partner whose app becomes unreadable would see its users consent under
    OUR brand with nothing saying so (same lesson as `zoho_oauth.app_fields`,
    inventory of silences of 2026-08-27, site B7). Only the ABSENCE (`None`) is a
    legitimate fallback — it is the state of every tenant that has set nothing.

    **The primary tenant never probes the vault**: its app IS the env, just as its shared
    keys are the platform instances (`tenant_vault.rung_tenant`, same
    rule, same reason — an `editor:oto` row that nobody reads would be a second
    mechanism for the same function, #409). Measurable consequence: at 99% of traffic,
    this rung costs NO read — every token refresh passes through here.
    """
    from .. import tenancy  # lazy: avoids any import cycle at boot
    registre = tenancy.current()
    slug = registre.tenant_of(sub)
    app = (credentials_store.get_editor_app("google", credentials_store.tenant_app_key(slug))
           if slug != tenancy.primary_slug() else None)
    if app:
        host = registre.callback_host(slug)
        return OAuthApp(client_id=app["client_id"], client_secret=app["client_secret"],
                        redirect_uri=oauth_flow.redirect_uri(_CALLBACK_PATH, host=host),
                        origin=f"tenant:{slug}")
    return OAuthApp(client_id=_client_id(), client_secret=_client_secret(),
                    redirect_uri=_redirect_uri(), origin="env")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _ctx_org(sub: str) -> int:
    """Context org (`current_org` seam, ADR 0023) — the MEMBER scope of Google
    accounts (ADR 0033 B3). Raises an actionable error rather than a silent scope."""
    from .. import access  # lazy: avoids any import cycle at boot
    org = access.current_org(sub)
    if org is None:
        raise RuntimeError(
            "No context org — cannot scope the Google account. "
            "Sign in again and retry.")
    return org


def make_state(sub: str, org_id: int, return_app: str = "",
               connector: str = "google", scope: str = "member",
               group_id: Optional[int] = None) -> str:
    """HMAC-signed state : `<b64(payload)>.<b64(sig)>` — payload = {sub, org, ts, app, c}.

    `connector` (`c`): WHICH card requested the consent — the account, or one of
    its six services (split of 2026-09-26). The callback returns there; without it, a
    person authorizing Drive landed on the account card.

    The org at START travels to the callback (which comes from Google, without the
    consultation headers): the account is scoped to the org where the user clicked
    "connect" (ADR 0033 B3).

    `return_app` carries which FRONT requested the connection. Same reason as the org:
    the callback arrives FROM Google, with no header or session — what the state does not
    carry is lost. Without it, a user coming from a third-party front landed
    on our side after consenting (oto-backend#877).

    ⚠️ The value is validated by the CALLER (`resolve_return_app`) before arriving
    here: the state must never carry an unverified front key, otherwise it
    signs an open redirect."""
    payload = json.dumps({"sub": sub, "org": org_id, "ts": int(time.time()),
                          "app": return_app, "c": connector,
                          # WHO the account is entrusted to (2026-09-27): the member, or their
                          # org / their team (SHARED account, set by an admin).
                          "s": scope, "g": group_id},
                         separators=(",", ":")).encode()
    sig = hmac.new(_state_secret(), payload, hashlib.sha256).digest()
    return f"{_b64url(payload)}.{_b64url(sig)}"


def verify_state(state: str) -> Optional[tuple]:
    """Returns (sub, org_id, return_app, connector, scope, group_id) if the state is valid and
    unexpired, otherwise None. `scope` falls back to `"member"` for a state from before
    shared accounts; a `scope="group"` without a team is refused.

    `connector` falls back to `"google"` for a state issued BEFORE the split: the account,
    exactly the card that existed then.

    `return_app` falls back to `""` for a state issued BEFORE this batch: they live
    a few minutes, some are in flight at deploy time, and breaking them would return
    an error to someone who has just authorized correctly."""
    if not state or "." not in state:
        return None
    p_b64, sig_b64 = state.split(".", 1)
    try:
        payload = _b64url_decode(p_b64)
        sig = _b64url_decode(sig_b64)
    # noqa: SILENT — fail-closed: a callback never distinguishes the causes of a refusal
    except Exception:
        return None
    expected = hmac.new(_state_secret(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(payload)
    # noqa: SILENT — fail-closed: a callback never distinguishes the causes of a refusal
    except Exception:
        return None
    if int(time.time()) - int(data.get("ts", 0)) > _STATE_TTL:
        return None
    sub, org = data.get("sub"), data.get("org")
    if not isinstance(sub, str) or not isinstance(org, int):
        return None
    # `app` missing = state issued before oto-backend#877: return to the default front,
    # never a refusal. Re-validated here although it was already filtered at start: the
    # state is signed, but a key removed from `RETURN_APPS` between the click and the
    # return must not come back to life through its signature.
    from . import flow as oauth_flow

    connector = data.get("c") or "google"
    if connector != "google" and connector not in SERVICE_SCOPES:
        return None
    scope, group = data.get("s") or "member", data.get("g")
    if scope not in ("member", "org", "group") or (
            scope == "group" and not isinstance(group, int)):
        return None
    return (sub, org, oauth_flow.resolve_return_app(data.get("app") or ""), connector,
            scope, group if scope == "group" else None)


def build_auth_url(sub: str, return_app: str = "", connector: str = "google",
                   scope: str = "member") -> str:
    """The Google consent URL — for the account, or for ONE service (its scopes
    only, cf. `scopes_for`).

    `return_app`: front key declared by the CALLER (e.g. a third-party front), never
    a sniffed Origin — capabilities are transport-agnostic (ADR 0009). Validated
    HERE, once, BEFORE `make_state`: `resolve_return_app` reduces any
    value outside its closed list to `""`, so the state never carries an unverified
    client value (no open redirect)."""
    from urllib.parse import urlencode

    from ..connectors.activation_gate import exiger_connectable_capacite
    from . import flow as oauth_flow

    # The account and each service card start here — including the historical
    # `GET /api/google/oauth/start`, which does not go through `connector_flow.start`.
    # A cut connector asks Google for nothing.
    exiger_connectable_capacite(connector, sub, perso=scope == "member")
    org_id = _ctx_org(sub)
    group_id: Optional[int] = None
    if scope != "member":
        # A SHARED account is only connected on behalf of those one administers:
        # org admin for the org, team lead for the team (same rule as
        # Salesforce). Raises PermissionError / ValueError — the flow names them.
        _, target = scope_target(sub, scope)
        group_id = target if scope == "group" else None
    resolved_app = oauth_flow.resolve_return_app(return_app)
    app = app_for(sub)
    params = {
        "client_id": app.client_id,
        "redirect_uri": app.redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes_for(connector, app)),
        "access_type": "offline",
        # consent → forces refresh_token; select_account → lets the user choose
        # which Google account to connect (key to multi-account).
        "prompt": "consent select_account",
        "state": make_state(sub, org_id, resolved_app, connector, scope, group_id),
        # INCREMENTAL consent: the token also carries the scopes already granted to this
        # client. That is what makes the split hold — authorizing Drive after Gmail yields ONE
        # token that knows both, on the same vault row. Under a partner's client too:
        # their client is dedicated to this product, and what enters the vault
        # is filtered anyway on `KNOWN_SCOPES` (`persist_token`) — and, for a
        # SHARED account, on what THIS consent requests: the union also carries the
        # holder's personal rights.
        "include_granted_scopes": "true",
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, sub: str) -> dict:
    """Exchange the OAuth code for tokens. Returns Google's response dict.

    `sub` (qualified, read back from the signed state) designates the app that requested the consent:
    the code is only exchanged with IT and its exact callback.
    Expected keys: `access_token`, `refresh_token`, `expires_in`, `scope`.
    """
    import requests
    app = app_for(sub)
    r = requests.post(
        _TOKEN_URL,
        data={
            "code": code,
            "client_id": app.client_id,
            "client_secret": app.client_secret,
            "redirect_uri": app.redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _fetch_email(access_token: str, scopes=()) -> str:
    """Fetch the address of the Google account that has just consented.

    Via `userinfo` as soon as the identity scope is there (every consent since the
    split requests it) — a Drive-only consent has no Gmail profile to read.
    Falls back to the Gmail profile for an earlier token, which only has `gmail.modify`.
    """
    import requests
    if IDENTITY_SCOPES[1] in set(scopes or ()):
        r = requests.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        r.raise_for_status()
        email = r.json().get("email")
        if not email:
            raise RuntimeError("userinfo without email — cannot identify the account.")
        return email
    r = requests.get(
        "https://gmail.googleapis.com/gmail/v1/users/me/profile",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    r.raise_for_status()
    email = r.json().get("emailAddress")
    if not email:
        raise RuntimeError("Gmail profile without emailAddress — cannot identify the account.")
    return email


class GoogleScopeMissing(RuntimeError):
    """Google token response without a `scope` field: granted rights unknown."""


class GoogleScopeRejected(RuntimeError):
    """Google refuses to bound the access token to the registered scopes (`invalid_scope`)."""


def persist_token(sub: str, org_id: int, token_response: dict,
                  client_id: Optional[str] = None, scope: str = "member",
                  group_id: Optional[int] = None,
                  connector: Optional[str] = None) -> str:
    """Persist the tokens (member scope: the org comes from the state, captured at
    flow start) and return the email of the connected Google account.

    `client_id`: the client that ISSUED this token, recorded on it — a token only
    refreshes with its issuer (cf. `credentials_for`). When absent, it is the app that
    `app_for(sub)` serves at this moment, the one that has just exchanged the code.

    `connector`: the card that requested the consent (carried by the state).
    Required for a SHARED account: it bounds what the share records."""
    if scope != "member" and connector is None:
        raise ValueError("persist_token: `connector` is required for a shared account.")
    refresh_token = token_response.get("refresh_token")
    if not refresh_token:
        # `build_auth_url` enforces `prompt=consent` + `access_type=offline`,
        # so Google MUST issue a refresh_token. If we get here, it is
        # a problem on Google's side → we bubble it up rather than mask it.
        raise RuntimeError(
            "Google did not issue a refresh_token despite prompt=consent. "
            "Check the OAuth client configuration in GCP."
        )
    access_token = token_response.get("access_token")
    expires_in = int(token_response.get("expires_in", 0) or 0)
    expires_at = datetime.fromtimestamp(time.time() + expires_in, tz=timezone.utc).isoformat() if expires_in else None
    # What enters the vault: the scopes the token CARRIES (the union, through
    # `include_granted_scopes`), bounded to those we know — never those of another
    # product of the same client. An exchange response WITHOUT `scope` is not "everything":
    # we do not know what was granted, so we refuse rather than silently open the six
    # services (review of #1081).
    brut = token_response.get("scope")
    if not brut:
        raise GoogleScopeMissing(
            "Google did not say which rights it grants (response without a `scope` field): "
            "nothing was saved. Restart the Google account connection.")
    granted = [sc for sc in brut.split() if sc in KNOWN_SCOPES]
    scopes = " ".join(granted)
    email = _fetch_email(access_token, granted)
    if scope != "member":
        # SHARED account: stored under the org or team of the state (verified signed), with
        # who connected it — never under the member who clicked.
        target = group_id if scope == "group" else org_id
        # The token carries the UNION of the holder's rights (`include_granted_scopes`),
        # their PERSONAL rights included: sharing Drive must not hand the whole
        # org the Gmail mailbox they only authorized for themselves (review of #1081). The
        # shared row only keeps what THIS consent requests, plus what it
        # already shared; and not the exchange access token, which carries the union: the
        # first use draws one by refresh, bounded to the row's scopes.
        deja = next((a.get("scopes") or "" for a in db.list_shared_google_accounts(scope, target)
                     if a.get("google_email") == email), "")
        app = app_for(sub)
        permis = set(scopes_for(connector, app)) | set(deja.split())
        scopes = " ".join(sc for sc in granted if sc in permis)
        db.set_shared_google_oauth(
            scope, target, set_by=sub,
            google_email=email, refresh_token=refresh_token, scopes=scopes,
            access_token=None, expires_at=None, client_id=client_id or app.client_id)
        logger.info("shared Google account connected: %s=%s account=%s by=%s scopes=%s",
                    scope, target, email, sub, scopes)
        return email
    emetteur = client_id or app_for(sub).client_id
    db.set_google_oauth(
        sub,
        org_id,
        google_email=email,
        refresh_token=refresh_token,
        scopes=scopes,
        access_token=access_token,
        expires_at=expires_at,
        client_id=emetteur,
    )
    return email


class GoogleReauthRequired(RuntimeError):
    """Google refresh token dead (invalid_grant) → the user must reconnect.

    `RuntimeError` and not `Exception` (#875/#876): the six Google tools translate
    the `RuntimeError`s of `credentials_for` into a readable refusal, and only those. A
    dead grant therefore ended up as "Internal server error" — the only case where
    the caller has a precise action to take (reconnect THIS account) was the one where
    we told them nothing."""


class GoogleClientRejected(RuntimeError):
    """Google refuses the OAuth CLIENT at refresh (`unauthorized_client`,
    `invalid_client`): the token was issued by another client, or the client's
    configuration is wrong (identifier, secret).

    `RuntimeError` for the same reason as `GoogleReauthRequired` (the Google tools only
    translate those into a readable refusal) — but NOT a subclass: it is not a dead
    grant, and the account is not marked (`oauth_flow.grant_is_dead`: a wrong
    config must destroy nothing nor get the account blamed)."""


def _emis_par_un_autre_client(row: dict, app: "OAuthApp") -> bool:
    """Was this row's token issued by a client other than `app`?

    A token with no recorded client dates from before this note: it comes from OUR app, the only
    one that existed then — hence from another client as soon as the served app is a
    tenant's."""
    emetteur = row.get("client_id")
    if not emetteur:
        return app.origin != "env"
    return emetteur != app.client_id


def config_dashboard(sub) -> str:
    """The dashboard of ITS product — where a service card connects."""
    from .. import config
    return config.dashboard_url_for(sub)


def _reconnecter(sub) -> str:
    """Where THIS account will reconnect its Google — the dashboard of ITS product.

    It used to be a constant pointing to ours. Served as is, it sent a partner's agent
    to us for an action they must do on their side: the defect of the onboarding
    base (13/08), found in a corner that no guard was watching —
    the hard-coded-addresses tripwire then only watched preproduction.
    """
    from .. import config
    return f"{config.dashboard_url_for(sub)}/ (Google section)"


def _refresh_access_token(refresh_token: str, sub: str,
                          app: Optional[OAuthApp] = None,
                          scopes: Optional[str] = None) -> dict:
    """`app`: the app already resolved by the caller (one vault read fewer);
    when absent, the one `app_for(sub)` serves.

    `scopes`: the scopes RECORDED for this row. Passed to Google, they BOUND
    the returned access token: with `include_granted_scopes` (the per-service split needs
    it), a tenant client's refresh token also carries the rights that client obtained
    elsewhere — the access token, for its part, only carries ours (review of
    #1081). The refresh token stays broad: that is structural."""
    import requests
    app = app or app_for(sub)
    data = {
        "client_id": app.client_id,
        "client_secret": app.client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if scopes:
        data["scope"] = scopes
    r = requests.post(_TOKEN_URL, data=data, timeout=15)
    # `invalid_grant` ALONE means "reauth" (same rule as atlassian/folk/zoho,
    # `oauth_flow.grant_is_dead`) — any other 4xx (misconfigured client) must
    # bubble up, not be confused with a dead grant.
    body = (r.text or "")[:300]
    if r.status_code in (400, 401) and oauth_flow.grant_is_dead(r.status_code, body):
        raise GoogleReauthRequired(body)
    if r.status_code in (400, 401) and any(
            code in body.lower() for code in ("unauthorized_client", "invalid_client")):
        raise GoogleClientRejected(body)
    if r.status_code == 400 and "invalid_scope" in body.lower():
        # The bounding is refused: we do NOT fall back to an unbounded token.
        raise GoogleScopeRejected(body)
    r.raise_for_status()
    return r.json()


def _no_account_message(sub: str, org_id: Optional[int], account: Optional[str]) -> str:
    """The "no account connected" message — naming the accounts that ARE, and the expected form.

    The message said neither, although it already knows the caller got the value
    wrong and `list_google_accounts` knows the right answer. Cost measured on
    14/08: four attempts looking for a nonexistent parameter, the call rebuilt from scratch
    to start on a good footing — and the `mode=draft` parameter forgotten along the way.
    Three emails sent to a client.

    The precise confusion to close: `otomata` is an ALIAS of the CLI convention
    (`oto -a otomata`), not an email. Here we expect the Google account's email."""
    try:
        connectes = [a["google_email"] for a in db.list_google_accounts(sub, org_id)
                     if a.get("google_email")]
    # noqa: SILENT — help message: connected-accounts list missing rather than wrong
    except Exception:      # never turn an input error into an outage
        connectes = []
    # The reachable SHARED accounts are also named in `account`: a message that omits them
    # sends the caller looking for a mailbox they could already reach. Apart from the
    # block above: an unreadable share must not erase the member's accounts.
    try:
        for scope, target in _shared_targets(sub, org_id):
            connectes += [a["google_email"] for a in db.list_shared_google_accounts(scope, target)
                          if a.get("google_email") and a["google_email"] not in connectes]
    # noqa: SILENT — help message: without the shared ones rather than an outage
    except Exception:
        pass
    dash = _reconnecter(sub)
    # Accounts are connected PER ORG: an empty list in this org says nothing about
    # the others. Name the org, and the two ways out.
    ici = (f" in org #{org_id} — connect one there, or call from an org where one is "
           "connected" if org_id is not None else "")
    if not account:
        return (f"No Google account connected{ici}. Connect one at {dash}."
                if not connectes else
                "No default Google account. Pass `_account=` — connected accounts: "
                f"{', '.join(connectes)}.")
    if not connectes:
        return (f"No Google account connected{ici} (you asked for `{account}`). "
                f"Connect one at {dash}.")
    return (f"No Google account connected for `{account}` — no other account was used. "
            f"Connected accounts: {', '.join(connectes)} — `_account` expects the "
            "account's EMAIL, not an alias or an organization name. The full list: "
            "google_accounts().")


# --- SHARED accounts (org / team, 2026-09-27) ---------------------------------
#
# An org admin or a team lead connects ONE Google account on behalf of everyone — a
# shared mailbox, a team calendar. Resolution order at call time: the MEMBER's account
# first (their own, or the one they name), then that of their active TEAM,
# then that of their ORG. Nobody reaches another person's PERSONAL account: only the
# accounts an admin has set as such are shared.

def _shared_targets(sub: str, org_id: int) -> list:
    """The shared-account holders reachable by this member, from nearest to
    broadest: their active team, the owning team lent to them by a share,
    then their org.

    Same rungs as the key cascade (`access.cascade`, #480): under `_project=`
    of an org the caller is not a member of, the org's account is only lent to them
    if the share granted it (`credentials="inherit"`) — otherwise a mere
    beneficiary read and sent from the org's shared mailbox."""
    from .. import access
    from ..access import heritage
    return heritage.paliers_partages(org_id, access.current_group(sub),
                                     heritage.du_contexte(sub, org_id))


def scope_target(sub: str, scope: str) -> tuple:
    """`(org_id, target_id)` for an ADMIN action on the shared accounts of a
    scope — raises `PermissionError` if the caller does not administer this scope."""
    from .. import access, roles
    org_id = _ctx_org(sub)
    if scope == "org":
        if not roles.is_org_admin(sub, org_id):
            raise PermissionError(
                "Only an organization admin connects or manages a Google account "
                "shared by the whole organization.")
        return org_id, org_id
    if scope == "group":
        group = access.current_group(sub)
        if group is None:
            raise PermissionError(
                "No active team: choose the team before sharing a Google account with it.")
        if not roles.can_admin_group(sub, int(group)):
            raise PermissionError(
                "Only a team lead connects or manages a Google account shared by the team.")
        return org_id, int(group)
    raise ValueError(f"invalid scope: {scope!r} (expected 'member', 'org' or 'group')")


def list_shared_accounts(sub: str) -> list[dict]:
    """The shared Google accounts reachable by this member (active team, org)."""
    from .. import access
    org_id = access.current_org(sub)
    if org_id is None:
        return []
    out: list[dict] = []
    for scope, target in _shared_targets(sub, org_id):
        out.extend(db.list_shared_google_accounts(scope, target))
    return out


def reachable_accounts(sub: str, service: Optional[str] = None) -> list[dict]:
    """Everything a call can name in `account`: the member's accounts, then the reachable
    SHARED accounts, from the nearest holder to the broadest — the order of the
    key cascade (`access.walk_cascade`) that `credentials_for` walks. An address
    present at two levels appears only once, at the level that resolves it.

    `shared` is `None` on a member account, `"group"`/`"org"` on a shared one.
    `is_default` says what a call WITHOUT `account` resolves: the member's default if they have an
    account, otherwise the default of the nearest shared holder.

    `service` (e.g. `"gmail"`) excludes the SHARED accounts that have not authorized this
    service: a mailbox shared for Drive only is not a Gmail mailbox. The
    member's accounts all stay listed, as before shared accounts."""
    own = list_accounts(sub)
    shared = list_shared_accounts(sub)
    out = [{**a, "shared": None} for a in own]
    vus = {a.get("google_email") for a in own}
    # With no account of their own, a call without `account` resolves the default of the nearest
    # holder: the head of `shared` (each holder is sorted default first).
    defaut = shared[0].get("google_email") if (not own and shared) else None
    for a in shared:
        email = a.get("google_email")
        if not email or email in vus:
            continue
        vus.add(email)
        if service and service not in services_granted(a.get("scopes")):
            continue
        out.append({**a, "shared": a.get("scope") or "org", "is_default": email == defaut})
    return out


def set_default_shared(sub: str, account: str, scope: str) -> bool:
    """The default shared account of a scope — admin action (`scope_target`)."""
    _, target = scope_target(sub, scope)
    return db.set_default_shared_google_account(scope, target, account)


def _refuse_two_names(account: Optional[str]) -> None:
    """The tool's `account` parameter and the call axis `_account=` name ONE choice.
    Both given, with two different addresses: a named refusal — serving either one
    would act from a mailbox the agent did not mean (oto-backend#1160)."""
    from .. import session_org
    from ..mcp_errors import McpError
    from mcp.types import ErrorData, INVALID_PARAMS
    axe = session_org.current_call_account()
    if account and axe and account.strip().casefold() != axe.strip().casefold():
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"`account=\"{account}\"` and `_account=\"{axe}\"` name two different "
                     "Google accounts: pass only one (`_account=`, the call's account). "
                     "Nothing was done."),
            data={"code": "account_conflict", "retryable": False}))


def _resolved_row(rc) -> Optional[dict]:
    """The vault row that the resolution designated, in the shape the refresh reads:
    the secret is the refresh token, the satellites live in its meta (no second
    decryption — `list_accounts` returns no secret)."""
    meta = next((a.get("meta") or {} for a in credentials_store.list_accounts(
        rc.entity_type, rc.entity_id, "google") if a["account"] == (rc.account or "")), None)
    if meta is None:
        return None
    return {"google_email": rc.account or None, "refresh_token": rc.key,
            "access_token": meta.get("access_token"), "expires_at": meta.get("expires_at"),
            "scopes": meta.get("scopes"), "client_id": meta.get("client_id")}


def credentials_for(sub: str, account: Optional[str] = None,
                    service: Optional[str] = None, *, source_account: bool = False):
    """Return a valid `google.oauth2.credentials.Credentials` for this sub.

    The account is chosen by the COMMON resolution of multi-account connectors
    (`access.resolve_credential`, oto-backend#1160), under the SERVICE called
    (`gmail`, `drive`…; the Google account itself when `service` is None): `account`
    (the tool's parameter, an email) or the call axis `_account=` — one choice, two
    different names refused (`_refuse_two_names`) —, otherwise the account pinned by
    the project (on the service's card, otherwise on the Google account's), otherwise
    the single account, otherwise the default one, otherwise a refusal that names the
    accounts. The member's account first, then the reachable SHARED ones (active
    team, org) — the key cascade. An unknown account is refused, never replaced by
    another. The account that served is noted on the call: the `_account` echo of
    the response names it.

    `source_account=True`: `account` names the account of a FILE SOURCE inside the
    call (an attachment, `file_source`) — it may differ from the call's `_account=`
    (send from one mailbox a file of another account's Drive).

    Transparent refresh if access_token is missing or expired. Raises an actionable
    RuntimeError (no account, unknown account, service not authorized) or a
    McpError (ambiguity, conflicting names, refused instance).
    """
    from .. import access  # lazy: avoids any import cycle at boot
    account = account or None
    if not source_account:
        _refuse_two_names(account)
    org_id = _ctx_org(sub)
    try:
        rc = access.resolve_credential(service or "google", want="byo", sub=sub,
                                       account=account)
    except access.CompteIntrouvable as e:
        raise RuntimeError(_no_account_message(sub, org_id, e.account)) from None
    except access.CredentialUnavailable:
        raise RuntimeError(_no_account_message(sub, org_id, None)) from None
    row = _resolved_row(rc)
    if row is None:   # removed between the resolution and this read
        raise RuntimeError(_no_account_message(sub, org_id, rc.account))
    if service and service not in services_granted(row.get("scopes")):
        # The account exists but has not authorized THIS service (split of 2026-09-26):
        # name the card to open, not "reconnect" — the Google API, for its part,
        # would answer a 403 `insufficientPermissions` without saying which.
        label = SERVICE_LABELS.get(service, service)
        raise RuntimeError(
            f"The Google account {row.get('google_email') or ''} has not "
            f"yet authorized {label}: connect {label} from its card at "
            f"{config_dashboard(sub)}. Nothing was done.")

    from google.oauth2.credentials import Credentials

    access_token = row.get("access_token")
    expires_at = row.get("expires_at")
    needs_refresh = not access_token
    if not needs_refresh and expires_at:
        try:
            exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            # 60s of margin to avoid blowing up mid-call
            if exp.timestamp() - time.time() < 60:
                needs_refresh = True
        # noqa: SILENT — unreadable credential ⇒ forced refresh, never a stale token served
        except Exception:
            needs_refresh = True

    # ONE read of the app per call (vault + decryption for a tenant account):
    # it serves the issuer check, the refresh and the returned object.
    app = app_for(sub)
    # The vault entity that carries the resolved row (member, team, org…): the
    # refresh and the health are written there, on THIS account's row.
    et, eid = rc.entity_type, rc.entity_id
    email = row.get("google_email") or ""
    if _emis_par_un_autre_client(row, app):
        # The served app has changed since the connection (set, changed or removed): this
        # token will no longer refresh. We say so before any network call — and before
        # a still-valid access_token masks the failure for an hour.
        connector_health.mark_rejected(
            et, eid, "google", email,
            "token issued by an OAuth client other than the served app")
        raise GoogleReauthRequired(
            f"The Google account {email or '(no email)'} was connected under a different OAuth "
            "app than the one served today (your organization's app has changed): "
            f"reconnect this account at {_reconnecter(sub)}. Nothing was done.")

    if needs_refresh:
        account = email
        scope = (et, eid, account)
        try:
            resp = _refresh_access_token(row["refresh_token"], sub, app,
                                         scopes=row.get("scopes"))
        except GoogleScopeRejected as e:
            raise GoogleScopeRejected(
                f"Google refuses to bound the token of account {account or '(no email)'} "
                f"to the recorded rights ({str(e)[:120]}): reconnect this account at "
                f"{_reconnecter(sub)}. Nothing was done.") from e
        except GoogleClientRejected as e:
            raise GoogleClientRejected(
                f"Google refuses the OAuth client when refreshing account "
                f"{account or '(no email)'} ({str(e)[:120]}): the app's configuration "
                "(identifier, secret) must be checked by an administrator. "
                "Nothing was done.") from e
        except GoogleReauthRequired as e:
            # Dead grant: we MARK (shared helper oto#25 lot b2), never purge —
            # same scope guard as atlassian/folk/salesforce/zoho. We raise
            # AFTERWARDS, without changing the contract of `credentials_for` (always valid
            # `Credentials` or an exception, never a silent `None`).
            connector_health.mark_rejected(
                et, eid, "google", account, str(e) or None)
            raise GoogleReauthRequired(
                f"The token of Google account {account or '(no email)'} is expired or "
                "revoked (Google answers invalid_grant): reconnect this account at "
                f"{_reconnecter(sub)}. Nothing was done.") from e
        access_token = resp["access_token"]
        expires_in = int(resp.get("expires_in", 0) or 0)
        new_exp = datetime.fromtimestamp(time.time() + expires_in, tz=timezone.utc).isoformat()
        credentials_store.update_meta(et, eid, "google", email,
                                      {"access_token": access_token, "expires_at": new_exp})
        # `update_meta` MERGES the meta (JSONB ||):
        # a `health_ko` set by an earlier dead refresh would never be
        # cleared by this path without this explicit call (oto#25 lot b3, same
        # reason as the Salesforce rotation — unlike atlassian/folk, whose
        # refresh REPLACES the whole meta and already unmarks by that fact alone).
        connector_health.record_health("google", scope, True, None)

    # The Google client also refreshes ON ITS OWN (googleapiclient): it needs
    # the app that issued the token — verified above, it is `app`.
    return Credentials(
        token=access_token,
        refresh_token=row["refresh_token"],
        token_uri=_TOKEN_URL,
        client_id=app.client_id,
        client_secret=app.client_secret,
        scopes=row["scopes"].split() if row.get("scopes") else SCOPES,
    )


def list_accounts(sub: str) -> list[dict]:
    """Google accounts connected by the user IN the context org (email, default, scopes)."""
    from .. import access  # lazy
    return db.list_google_accounts(sub, access.current_org(sub))


def _link_state(sub: str) -> connector_link.LinkState:
    """Link state for `/api/me`. Google is MULTI-ACCOUNT: one vault row per
    address (`account = email`), with its satellites in `meta`. A generic loop
    looking for "the" member row would find none."""
    # An account SHARED by the org or team also links the card: the tools
    # resolve it (`credentials_for`), the card must not say "to connect".
    accounts = list_accounts(sub) + list_shared_accounts(sub)
    return connector_link.LinkState(
        linked=bool(accounts), accounts=len(accounts),
        set_at=max((a.get("set_at") or "" for a in accounts), default="") or None)


connector_link.register("google", _link_state)


def _link_state_for(service: str):
    """The link state of ONE service: the accounts that have AUTHORIZED it, not all
    the holder's accounts — otherwise the Drive card would say "connected" to someone who
    only consented to Gmail, and the first `drive_file` would fail."""
    def read(sub: str) -> connector_link.LinkState:
        accounts = [a for a in list_accounts(sub) + list_shared_accounts(sub)
                    if service in services_granted(a.get("scopes"))]
        return connector_link.LinkState(
            linked=bool(accounts), accounts=len(accounts),
            set_at=max((a.get("set_at") or "" for a in accounts), default="") or None)
    return read


for _svc in SERVICES:
    connector_link.register(_svc, _link_state_for(_svc))


def _start_flow(ctx, values: dict) -> "connector_flow.FlowStart":
    """The "connect" action, declared like that of any other connector (#300).

    It existed — but **outside the checkpoint**: a hand-written REST route
    returned `{auth_url}` by coincidence, with nothing forcing it to, and the guard
    that enforces the common shape only sees capabilities.

    ⚠️ A missing OAuth configuration raises a `RuntimeError` here that the route
    translated into a 500. Under the seam, it is an INPUT refusal (the platform has no
    Google app configured), not an outage: translated into a named error, the caller
    will know that retrying will not change anything.
    """
    from ..capabilities._types import AuthzDenied
    try:
        # `app` is a HIDDEN key, not a declared `FlowParam`: the front passes it
        # outside the form (the client knows who it is), it must never become
        # a field visible to the user. Same convention as the four other
        # OAuth connectors — Google was the only one to ignore it (oto-backend#877).
        app_key, scope = (values or {}).get("app") or "", (values or {}).get("scope") or "member"
        url = (build_auth_url(ctx.sub, app_key) if scope == "member"
               else build_auth_url(ctx.sub, app_key, scope=scope))
        return connector_flow.FlowStart(auth_url=url)
    except PermissionError as e:
        raise AuthzDenied(403, "scope_forbidden", str(e))
    except ValueError as e:
        raise AuthzDenied(400, "invalid_scope", str(e))
    except RuntimeError as e:
        raise AuthzDenied(503, "oauth_misconfigured", str(e))


connector_flow.declare(
    "google",
    start=_start_flow,
    label="Link a Google account",
    callback_path="/api/google/oauth/callback",
)


def _start_flow_for(service: str):
    """The "connect" action of ONE service (split of 2026-09-26): the same flow as
    the account, bounded to its scopes (`scopes_for`), and a state that names the card —
    the callback brings the user back there."""
    def start(ctx, values: dict) -> "connector_flow.FlowStart":
        from ..capabilities._types import AuthzDenied
        try:
            app_key = (values or {}).get("app") or ""
            scope = (values or {}).get("scope") or "member"
            url = (build_auth_url(ctx.sub, app_key, connector=service) if scope == "member"
                   else build_auth_url(ctx.sub, app_key, connector=service, scope=scope))
            return connector_flow.FlowStart(auth_url=url)
        except PermissionError as e:
            raise AuthzDenied(403, "scope_forbidden", str(e))
        except ValueError as e:
            raise AuthzDenied(400, "invalid_scope", str(e))
        except RuntimeError as e:
            raise AuthzDenied(503, "oauth_misconfigured", str(e))
    return start


for _svc in SERVICES:
    connector_flow.declare(
        _svc,
        start=_start_flow_for(_svc),
        label=f"Authorize {SERVICE_LABELS[_svc]}",
        callback_path="/api/google/oauth/callback",
    )


def _grant_held_elsewhere(email: Optional[str], client_id: Optional[str],
                          entity: tuple) -> bool:
    """Does ANOTHER vault row carry this Google account, issued by the same client?

    Google treats a `/revoke` as the end of the APP's access to the whole account, not
    of a single token: every token this client issued for this address falls with it.
    Yet one address often lives in two places — a member's mailbox connected for
    themselves AND shared with their org, or connected in two orgs. Revoking while removing one
    silently killed the other, discovered at the first `invalid_grant`.

    When in doubt (unreadable vault, unknown issuer on one of the rows), we answer YES:
    the row is deleted regardless, and a token we no longer hold is of no use to
    anyone — whereas one revocation too many breaks a live connection."""
    if not email:
        return False
    try:
        holders = db.google_grant_holders(email)
    # noqa: SILENT — when in doubt we do not revoke at Google (the row goes anyway)
    except Exception:
        return True
    et, eid = entity[0], str(entity[1])
    for h in holders:
        if (h["entity_type"], str(h["entity_id"])) == (et, eid):
            continue
        if client_id is None or h.get("client_id") is None or h["client_id"] == client_id:
            return True
    return False


def _revoke_shared(sub: str, account: Optional[str], scope: str) -> None:
    """Remove a SHARED account (or all those of the scope) — admin action."""
    import requests

    _, target = scope_target(sub, scope)
    for r in db.list_shared_google_accounts(scope, target):
        if account is not None and r.get("google_email") != account:
            continue
        # Who removes, and who had connected it: the row disappears with its meta.
        logger.info("shared Google account removed: %s=%s account=%s by=%s connected_by=%s",
                    scope, target, r.get("google_email"), sub, r.get("connected_by"))
        try:
            row = db.get_shared_google_oauth(scope, target, account=r.get("google_email"))
        # noqa: SILENT — declared debt: undecryptable credential ⇒ we delete anyway (#424)
        except Exception:
            row = None
        if row and row.get("refresh_token") and _grant_held_elsewhere(
                row.get("google_email"), row.get("client_id"), (scope, target)):
            logger.info("shared Google account removed without Google revocation: %s=%s account=%s "
                        "— another row carries the same access", scope, target,
                        r.get("google_email"))
        elif row and row.get("refresh_token"):
            try:
                requests.post("https://oauth2.googleapis.com/revoke",
                              data={"token": row["refresh_token"]}, timeout=10)
            # noqa: SILENT — declared debt: the refresh_token stays alive at Google (#424, verdict C)
            except Exception:
                pass
    db.delete_shared_google_oauth(scope, target, account=account)


def revoke(sub: str, account: Optional[str] = None, scope: str = "member") -> None:
    """Revoke on the Google side + delete from the DB.

    `account` (email) targets one account; None revokes all the user's accounts.
    `scope` `org`/`group`: the SHARED accounts of this scope (admin only).
    """
    import requests

    if scope != "member":
        _revoke_shared(sub, account, scope)
        return
    org_id = _ctx_org(sub)
    if account is None:
        rows = db.list_google_accounts(sub, org_id)
        targets = [r.get("google_email") for r in rows]
    else:
        targets = [account]

    for email in targets:
        # Revoking on the Google side is best-effort: an undecryptable credential
        # (row encrypted with an outdated master key → InvalidTag) must NOT
        # prevent the deletion. The contract of revoke = delete in the DB.
        try:
            row = db.get_google_oauth(sub, org_id, account=email)
        # noqa: SILENT — declared debt: undecryptable credential ⇒ we delete anyway (#424)
        except Exception:
            row = None
        if row and row.get("refresh_token") and _grant_held_elsewhere(
                email, row.get("client_id"),
                (credentials_store.MEMBER, credentials_store.member_id(org_id, sub))):
            logger.info("Google account removed without Google revocation: account=%s — another "
                        "row carries the same access", email)
        elif row and row.get("refresh_token"):
            try:
                requests.post(
                    "https://oauth2.googleapis.com/revoke",
                    # `data=` (body) and not `params=`: in the query string the refresh
                    # token ends up in the URL → Sentry breadcrumbs, proxy logs.
                    data={"token": row["refresh_token"]},
                    timeout=10,
                )
            # noqa: SILENT — declared debt: the refresh_token stays alive at Google (#424, verdict C)
            except Exception:
                pass  # we delete in the DB anyway
    db.delete_google_oauth(sub, org_id, account=account)
