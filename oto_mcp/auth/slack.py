"""Slack — installing the instance's PUBLISHED app in one click (OAuth v2).

Until now a Slack credential was obtained one way only: the user creates their own
app from a manifest, installs it, and pastes its tokens into the card. That path
stays — it is the only one for an instance that publishes no app, and the only one
exempt from Slack's read limits on unlisted apps. This module adds the other path,
on the common pattern (`connectors/flow` + `auth/flow`):

1. "Add to Slack" on the card → `start` returns `oauth/v2/authorize`, with a signed,
   single-use `state` carrying the identity and the tier (`member`|`org`|`group`);
2. Slack brings the browser back to `/api/slack/oauth/callback` (`api/slack.py`),
   where the code becomes tokens (`oauth.v2.access`);
3. the tokens go into the connector's EXISTING multi-field credential — `bot_token`
   from `access_token`, `user_token` from `authed_user.access_token` — so the tools
   change nothing: a row written here reads exactly like a pasted one.

**One account = one workspace** (oto-backend#409): the account is found by the
workspace id (`meta.team_id`) — a reinstall of the same workspace REPLACES its row,
a second workspace adds one. A new account takes the workspace's name.

⚠️ **The app's coordinates are the instance's**, PLATFORM scope of
`connector_settings`, set by the operator (`client_id`, `client_secret`) — as for
`meta_ads`. Not per tenant (`docs/tenants.md` §Convention). Without them, the flow is
still declared but `app_ready` says no, and the card keeps its form.

⚠️ **Switching app is not free.** Channel memberships belong to the APP, not to the
key: replacing a workspace's pasted tokens (their own app) by this app's starts from
zero on private channels (`tools/slack._verify`). The replacement is the user's
explicit gesture — "Add to Slack" on a workspace already connected — and the
callback logs it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

from . import flow as oauth_flow

logger = logging.getLogger(__name__)

CONNECTOR = "slack"
AUD = "slack"
CALLBACK_PATH = "/api/slack/oauth/callback"
_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
_SCOPES = ("member", "org", "group")

#: Only `client_secret` is secret — and it carries the `secret` that the admin
#: console masks (`capabilities/platform_connectors._sans_les_secrets`).
_REGLAGES = ("client_id", "client_secret")
_COMMANDE = ('oto_admin_connector_setting(op="set", connector="slack", '
             'key="<key>", value="<value>")')

#: The scopes asked at install — the manifest of `connectors/docs/slack.md` (what the
#: tools use) plus `app_mentions:read`, for the in-Slack bot (`slack_events.py`): a
#: mention of the app in a channel reaches us only with it. DMs to the bot need
#: nothing more (`im:history`). No `assistant:` nor slash command: not built.
#: ⚠️ Adding one later means EVERY connected workspace reinstalls: this list moves
#: with the app's declared scopes, never ahead of them.
#: Asked by the published app ONLY: the bot answers through the instance's events
#: endpoint, which an app you create yourself never calls — the manifest of the card
#: does not carry them.
SCOPES_DU_BOT = ("app_mentions:read",)
BOT_SCOPES = (
    *SCOPES_DU_BOT,
    "channels:history", "channels:join", "channels:read", "chat:write", "files:read",
    "groups:history", "groups:read", "im:history", "im:read", "im:write",
    "mpim:history", "mpim:read", "reactions:write", "usergroups:read",
    "usergroups:write", "users:read", "users:read.email",
)
USER_SCOPES = ("chat:write", "search:read", "usergroups:read", "usergroups:write")


class ConnectRefused(ValueError):
    """Actionable refusal at start — message safe to display."""


class SlackAppNotPublished(RuntimeError):
    """The instance publishes no Slack app: its coordinates (`client_id`,
    `client_secret`) are missing at the PLATFORM scope of `connector_settings`. The
    message names the missing keys and the operator's command."""


class SlackTokenRotationEnabled(RuntimeError):
    """Slack returned a `refresh_token`: token rotation is enabled on the published
    app, and this flow does not handle it (no renewal of an expiring `xoxe-` token).
    A misconfiguration of OUR app, not a refusal by the user — it goes to Sentry, and
    nothing is stored."""


# --- the application's coordinates ----------------------------------------

def _reglages() -> dict:
    """COLD read: only at the start of a flow or on return from a consent."""
    from ..db import connector_settings as store

    return {r["key"]: (r["value"] or "").strip()
            for r in store.list_connector_settings()
            if r["scope_type"] == "platform" and r["connector"] == CONNECTOR
            and r["key"] in _REGLAGES}


def coordonnees_manquantes() -> list[str]:
    poses = _reglages()
    return [nom for nom in _REGLAGES if not poses.get(nom)]


def app() -> dict:
    """`{client_id, client_secret}`, or a refusal that NAMES what is missing."""
    manquantes = coordonnees_manquantes()
    if manquantes:
        raise SlackAppNotPublished(
            f"This instance publishes no Slack app: missing {', '.join(manquantes)}. "
            f"Nothing to do on your account — paste your own app's tokens instead, or "
            f"ask the operator to set them with {_COMMANDE}.")
    return _reglages()


#: The app's Events API signing secret — same platform scope, separate from
#: `_REGLAGES` on purpose: the install works without it, only the bot needs it.
SIGNING_SECRET = "signing_secret"


def signing_secret() -> Optional[str]:
    """The signing secret of the published app, or None — read on EACH event (one
    row, platform scope): rotating it in the console takes effect at once."""
    from ..db import connector_settings as store

    for r in store.list_connector_settings(SIGNING_SECRET):
        if r["scope_type"] == "platform" and r["connector"] == CONNECTOR:
            return (r["value"] or "").strip() or None
    return None


def app_disponible(sub: str) -> bool:
    del sub
    return not coordonnees_manquantes()


# --- flow start --------------------------------------------------------------

def start(ctx, values: dict):
    """`connector_flow` → Slack's install URL. `values`: `scope` and `app` (set by the
    front, outside any form)."""
    from .. import access, roles
    from ..connectors import flow as connector_flow

    scope = (values.get("scope") or "member").strip() or "member"
    if scope not in _SCOPES:
        raise ConnectRefused(f"unknown tier: {scope!r}")
    org_id = access.current_org(ctx.sub)
    if org_id is None:
        raise ConnectRefused("no context org — sign in again and retry.")
    group_id: Optional[int] = None
    if scope == "org" and not roles.is_org_admin(ctx.sub, org_id):
        raise PermissionError("only an org admin can connect a workspace for the whole org.")
    if scope == "group":
        group_id = access.current_group(ctx.sub)
        if group_id is None or not roles.can_admin_group(ctx.sub, group_id):
            raise PermissionError("only a team lead can connect a workspace for the team.")

    coordonnees = app()
    payload = {"sub": ctx.sub, "org": org_id, "scope": scope,
               "app": oauth_flow.resolve_return_app(values.get("app")),
               "jti": oauth_flow.new_jti()}
    if group_id is not None:
        payload["group"] = group_id
    params = {"client_id": coordonnees["client_id"],
              "scope": ",".join(BOT_SCOPES), "user_scope": ",".join(USER_SCOPES),
              "redirect_uri": oauth_flow.redirect_uri(CALLBACK_PATH),
              "state": oauth_flow.sign_state(AUD, payload)}
    return connector_flow.FlowStart(auth_url=f"{_AUTHORIZE_URL}?{urlencode(params)}")


def _start_flow(ctx, values: dict):
    """Entry point of the `connector_flow` seam — refusals become named responses."""
    from ..capabilities._types import AuthzDenied

    try:
        return start(ctx, values or {})
    except PermissionError as e:
        raise AuthzDenied(403, "org_admin_required", str(e))
    except ConnectRefused as e:
        raise AuthzDenied(400, "connect_refused", str(e))
    except SlackAppNotPublished as e:
        raise AuthzDenied(503, "oauth_misconfigured", str(e))


def read_state(state: Optional[str]) -> Optional[dict]:
    data = oauth_flow.read_state(AUD, state)
    if not data:
        return None
    if (not isinstance(data.get("sub"), str) or not isinstance(data.get("org"), int)
            or data.get("scope") not in _SCOPES or not isinstance(data.get("jti"), str)):
        return None
    if data["scope"] == "group" and not isinstance(data.get("group"), int):
        return None
    if not isinstance(data.get("app"), str):
        data["app"] = ""
    return data


def still_allowed(parsed: dict) -> bool:
    """The right to write at the state's tier, re-read ON RETURN. Synchronous (SQL)."""
    from .. import roles

    if parsed["scope"] == "org":
        return roles.is_org_admin(parsed["sub"], parsed["org"])
    if parsed["scope"] == "group":
        return roles.can_admin_group(parsed["sub"], parsed["group"])
    return True


# --- the exchange and the vault ----------------------------------------------

def exchange(code: str) -> dict:
    """`oauth.v2.access`. ⚠️ Slack answers HTTP 200 with `ok: false` on a refusal
    (`invalid_code`, `bad_redirect_uri`, `invalid_client_id`…): that is a refusal of
    the exchange, never a server error — `OAuthExchangeRefused`, dropped from Sentry."""
    coordonnees = app()
    body = oauth_flow.exchange_code(
        _TOKEN_URL, code=code, client_id=coordonnees["client_id"],
        client_secret=coordonnees["client_secret"],
        redirect=oauth_flow.redirect_uri(CALLBACK_PATH))
    if body.get("ok") is not True:
        raise oauth_flow.OAuthExchangeRefused(
            f"Slack refused the exchange: {body.get('error') or 'ok is not true'}.")
    if not ((body.get("team") or {}).get("id")):
        raise oauth_flow.OAuthExchangeRefused("Slack returned no workspace id.")
    return body


def _account_for(rows: list[dict], team_id: str, team_name: str) -> tuple[str, Optional[dict]]:
    """`(account, row it replaces)` — the row of THIS workspace if one exists (found by
    `meta.team_id`, whatever its name), otherwise a new account named after the
    workspace, suffixed by its id when that name is already taken by another row."""
    for r in rows:
        if (r.get("meta") or {}).get("team_id") == team_id:
            return r["account"], r
    pris = {r["account"] for r in rows}
    nom = (team_name or "").strip() or team_id
    return (nom if nom not in pris else f"{nom} ({team_id})"), None


def _refuser_la_rotation(body: dict) -> None:
    """A `refresh_token` in the grant (bot or user) = rotation enabled on the app."""
    if body.get("refresh_token") or (body.get("authed_user") or {}).get("refresh_token"):
        raise SlackTokenRotationEnabled(
            "Slack returned a refresh_token: token rotation is enabled on the published "
            "Slack app, which oto does not handle — nothing was saved. The app must run "
            "without rotation (`token_rotation_enabled: false`, as in the manifest of "
            "the connector's card).")


def _meme_app(meta: dict, app_id: Optional[str]) -> bool:
    """The row being replaced was installed by THIS flow and by THIS app."""
    return (meta.get("install_source") == "oauth" and bool(app_id)
            and meta.get("app_id") == app_id)


def finish(parsed: dict, body: dict) -> str:
    """Stores the tokens of an exchange. Synchronous (SQL) — the caller runs it off
    the loop. Returns the account name set. Never logs a token."""
    from .. import credentials_store

    _refuser_la_rotation(body)
    team = body.get("team") or {}
    team_id, team_name = str(team["id"]), str(team.get("name") or "")
    # A token Slack did not return is ABSENT, never "" — for `merge_with_existing` an
    # empty key is an explicit erase.
    recus = {"bot_token": body.get("access_token"),
             "user_token": (body.get("authed_user") or {}).get("access_token")}
    fields = {k: v for k, v in recus.items() if v}
    sub, org_id = parsed["sub"], parsed["org"]
    etype, eid = credentials_store.entity_for_scope(parsed["scope"], org_id, sub,
                                                    parsed.get("group"))
    account, remplacee = _account_for(
        credentials_store.list_accounts(etype, eid, CONNECTOR), team_id, team_name)
    app_id = body.get("app_id") or None
    if remplacee and _meme_app(remplacee.get("meta") or {}, app_id):
        # Same app reinstalled: a token Slack did not return this time (user scopes
        # declined) keeps the one already set. Never across apps — a pasted user token,
        # or one from another published app, belongs to ANOTHER app than this bot.
        fields = credentials_store.merge_with_existing(etype, eid, CONNECTOR, account, fields)
    elif remplacee:
        logger.info("slack: workspace %s switches app (from %s to %s, org=%s, tier=%s) — "
                    "channel memberships start from zero", team_id,
                    (remplacee.get("meta") or {}).get("app_id") or "its own app", app_id,
                    org_id, parsed["scope"])
    fields = credentials_store.validate_fields(CONNECTOR, fields)
    enterprise = body.get("enterprise") or {}
    meta = {"team_id": team_id, "team_name": team_name,
            "enterprise_id": enterprise.get("id") or None,
            "is_enterprise_install": bool(body.get("is_enterprise_install")),
            "bot_user_id": body.get("bot_user_id") or None,
            "app_id": app_id,
            "install_source": "oauth",
            "connected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    credentials_store.guard_account_write(etype, eid, CONNECTOR, account, org=org_id)
    credentials_store.set_credential(etype, eid, CONNECTOR,
                                     credentials_store.pack_secret(CONNECTOR, fields),
                                     set_by=sub, account=account, meta=meta)
    logger.info("slack: workspace %s installed (org=%s, tier=%s, user_token=%s)",
                team_id, org_id, parsed["scope"], bool(fields.get("user_token")))
    return account


def _declare_flow() -> None:
    from ..connectors import flow as connector_flow

    connector_flow.declare(
        CONNECTOR,
        start=_start_flow,
        label="Add to Slack",
        callback_path=CALLBACK_PATH,
        app_ready=app_disponible,
    )


_declare_flow()
