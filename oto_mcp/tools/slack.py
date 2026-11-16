"""Slack — outbound messaging + reads on behalf of the authenticated user.

Per-user: each user sets their own **user token** (`xoxp-`) on
`/account` (provider `slack`), or an admin grants them the platform key
(bootstrapped from `SLACK_USER_TOKEN`). The key is resolved per call via
`access.resolve_api_key("slack")` — no shared server token in clear text.

An account (a workspace) carries up to two identities: the app (`xoxb-`) and a
person (`xoxp-`). READS are routed by the client (channel → bot, DM →
user). WRITES (post, delete, react) take `author`:
chosen by the caller, refused if ambiguous or without a token — never inferred
(decision of 23/09). The instance's published app, installed in one click, writes
the same credential (`auth/slack.py`).

An ORG or TEAM connection always acts as the app (15/11/2026): its user token is the
personal token of whoever clicked "Add to Slack", so it is never used (`_partage`).
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Literal, Optional, Union

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, credentials_store, file_content
from ..mcp_errors import McpError
from ..connectors import verify as connector_verify
from ..auth import slack as _slack_auth  # noqa: F401 — declares the "Add to Slack" flow
from .lecture import ACTE, LECTURE

logger = logging.getLogger(__name__)


#: What, in the body of `auth.test`, IDENTIFIES the application — and nothing else.
#: Slack does not return all these fields for all tokens (`bot_id`/`app_id` only exist
#: on the bot side): we relay what it ACTUALLY returned, never a key fabricated as
#: empty. A value we do not have is not returned as its default.
_IDENTITE = ("app_id", "bot_id", "team", "team_id", "url", "user", "user_id")


def _verify(fields: dict, config: dict | None = None) -> dict:  # noqa: ARG001 (config: probe contract)
    """Slack "test the connection" probe (signal #217): a token can be SET,
    authenticate, and still lack the read scopes → `slack_list_channels`
    fails with `missing_scope` and everything else is unreachable (no channel ID).
    Two stages, actionable message: (1) `auth.test` passes with ANY live token
    whatever its scopes → separates "dead token" from "token OK, scope missing";
    (2) a real channel read (`channels:read`) — its `missing_scope` is THE
    diagnostic that was missing.

    ⚠️ **The body of `auth.test` is now RETURNED, no longer thrown away** (signals 802/814, 08/09/2026).
    It was called for its success alone, and it is the response that carried the missing
    fact: the application's identity. A credential replaced with the tokens of ANOTHER
    Slack app authenticates perfectly — and starts from zero on channel memberships,
    which belong to the app, not the key. Lived on org 196: four private
    customer channels unreadable for six days, a `not_in_channel` that is the
    SAME code as a never-joined channel, and a "joined the channel" rewritten six times
    a day in a customer's channel — the only known remedy, for lack of knowing that
    the app had changed. The probe does not judge this change: it returns what is needed to
    notice it, which existed nowhere.

    ⚠️ **One `auth.test` per token set, each with its own.** The previous single call
    went out on the USER token as soon as there was one (`default_as_user`): it
    therefore NEVER identified the bot's app, which is precisely the one that carries
    the memberships. Two tokens = two distinct identities, and confusing them would
    answer "yes, same app" to a bot change.

    ⚠️ **Accepted consequence**: a dead bot token next to a live user token
    now MAKES the probe fail, where it used to pass green without having looked.
    It is one green fewer, not one red more — the bot half of the credential was
    simply never tested, and it is the one that reads channels."""
    from oto.tools.slack.client import SlackClient, SlackError

    bot = (fields.get("bot_token") or "").strip() or None
    user = (fields.get("user_token") or "").strip() or None
    if not bot and not user:  # legacy single-field credential (raw token) → routed by prefix
        raw = next((str(v).strip() for v in fields.values() if str(v or "").strip()), "")
        if raw.startswith("xoxb-"):
            bot = raw
        elif raw:
            user = raw
    if not bot and not user:
        raise ValueError("no Slack token set (bot_token `xoxb-` or user_token `xoxp-`)")

    client = SlackClient(bot_token=bot, user_token=user, default_as_user=bool(user))
    identite: dict = {}
    for genre, jeton in (("bot", bot), ("user", user)):
        if not jeton:
            continue
        try:
            corps = client._request("POST", "auth.test", as_user=(genre == "user"))
        except SlackError as e:
            # The kind is NAMED: with two tokens set, a bare "invalid Slack token"
            # leaves you hunting for which of the two to set again.
            raise ValueError(
                f"invalid Slack token (`{genre}`, {e.error}) — set a valid "
                f"`{'xoxb-' if genre == 'bot' else 'xoxp-'}` again") from None
        identite[genre] = {c: corps[c] for c in _IDENTITE if corps.get(c)}
    try:
        client.list_channels(types="public_channel")
    except SlackError as e:
        if e.error == "missing_scope":
            raise ValueError(
                "Slack token authenticated but SCOPES insufficient: "
                "`channels:read` is missing (without it, no channel ID is discoverable → "
                "`slack_read_history` unreachable). Reinstall the Slack app with "
                "`channels:read`, `groups:read`, `channels:history`, `groups:history`.") from None
        raise ValueError(f"Slack read failed ({e.error})") from None
    return {"identity": identite}


# Where a Slack right is granted. Written ONCE: this procedure is long,
# and it has no place in a docstring (copied into every session, ~470
# tools) — it lives in the refusal, paid for once, at the moment it is needed.
_OU_DONNER_UN_DROIT = (
    "api.slack.com/apps → your app → OAuth & Permissions → Scopes, then REINSTALL "
    "the app in the workspace (an added right is only carried by the token after "
    "reinstallation) and set the new token on /account"
)
# A private channel cannot be joined through ANY API: it is the only action that stays human.
_GESTE_INVITATION = (
    "a human who is already a member of the channel must type `/invite` followed by the name of your Slack app there"
)


# Who WRITES in Slack. An account can carry two identities — the app (bot token
# `xoxb-`) and a person (user token `xoxp-`) — and the client used to route
# writes to the second as soon as it existed: an agent posted in your name without
# having chosen it, or under the app's name without being able to do otherwise. Decision of
# 23/09: the author is CHOSEN. Ambiguous = named refusal, the same rule as for the
# workspace — a message that went out under the wrong name cannot be taken back.
Auteur = Literal["me", "app"]


def _refus_entree(message: str) -> McpError:
    """CURATED refusal: `McpError` INVALID_PARAMS, never a bare `ValueError`. The
    error taxonomy only returns an exception's text to the agent if it is
    curated; a `ValueError` without an upstream cause becomes "Internal server error",
    message lost and counted as a bug. Lived on the first send in prod (v1.334.0):
    the refusal was right, the agent only read the opacity — and again on
    `slack_read_thread` given a reply's ts (#1350)."""
    return McpError(ErrorData(code=INVALID_PARAMS, message=message))


def _parent_d_une_reponse(message: dict) -> Optional[str]:
    """The parent's ts when `message` is a thread REPLY (`thread_ts` ≠ `ts`), else None:
    a parent carries its own ts as `thread_ts`, a message without a thread carries none."""
    parent = message.get("thread_ts")
    return parent if parent and parent != message.get("ts") else None


def _fil(msgs: list, data: dict) -> dict:
    """A `conversations.replies` page in the tool's shape: the parent (Slack repeats
    it as `messages[0]` on every page) kept out of `replies`."""
    return {
        "parent": msgs[0],
        "replies": msgs[1:],
        "has_more": bool(data.get("has_more")),
        "next_cursor": (data.get("response_metadata") or {}).get("next_cursor"),
    }


#: Levels whose credential is SHARED by several people — never one person's identity.
_NIVEAUX_PARTAGES = (credentials_store.ORG, "group", credentials_store.TENANT)


def _partage(rc) -> bool:
    """Does this credential belong to the org, a team or the tenant — rather than to
    the person calling? (A platform grant keeps its historical routing.)

    ⚠️ The user token of such a credential is NOBODY's own: "Add to Slack" at the org
    tier stores `authed_user.access_token`, the token of whoever clicked install
    (`auth/slack.finish`). Posting with it put the installer's name on messages from
    another member's run, and `open_dm` opened the installer's DM, where the app cannot
    post — a likely path to an agent retrying as `author="me"`. Reported 15/11/2026;
    fixed by routing: a shared credential acts as the app, only a member's own
    credential speaks as them."""
    return getattr(rc, "entity_type", None) in _NIVEAUX_PARTAGES


def _auteur(bot: bool, user: bool, author: Optional[str], partage: bool = False) -> bool:
    """`as_user` to use for a write, or a refusal that says what to pass.

    A shared connection (`_partage`): always the app. A single token set: it is used,
    nothing to choose. Both: the caller names the author. An author requested without
    the token that carries it: refusal, never a fallback to the other identity."""
    if partage:
        if author == "me":
            raise _refus_entree(
                "`author=\"me\"` impossible: this Slack workspace is connected for the "
                "organisation or a team, and such a connection only writes as the app "
                "(`author=\"app\"`, or omit `author`). To write in your own name, connect "
                "your own Slack account on the connector card (for you, not the organisation).")
        if not bot:
            raise _refus_entree(
                "This Slack workspace is connected for the organisation or a team with a "
                "user token only (`xoxp-`), and such a connection only writes as the app. "
                "Reconnect it with \"Add to Slack\" (or set the app's bot token `xoxb-`) "
                "on the connector card.")
        return False
    if author is None:
        if bot and user:
            raise _refus_entree(
                "This workspace carries two Slack identities: specify who writes — "
                "`author=\"me\"` (in your name, user token) or `author=\"app\"` "
                "(under the app's name, bot token). No default is taken: a "
                "message that went out under the wrong name cannot be taken back.")
        return bool(user)
    if author == "me" and not user:
        raise _refus_entree(
            "`author=\"me\"` impossible: this workspace has no user token "
            "(`xoxp-`). Only the app can write there (`author=\"app\"`), or set a user "
            "token on the connector card.")
    if author == "app" and not bot:
        raise _refus_entree(
            "`author=\"app\"` impossible: this workspace has no bot token "
            "(`xoxb-`). Only you can write there (`author=\"me\"`), or set the "
            "app's bot token on the connector card.")
    return author == "me"


def _noter_pour_les_reponses(channel: str, tss: list, texte: str, auteur: str,
                             thread_ts: Optional[str]) -> None:
    """A message posted during a HOSTED run is noted under that run's agent. Its own —
    as the app, outside a thread — is one a reply in its thread goes to; every other
    one only tells the event route the message is OURS: posted with a user token it
    carries no `bot_id`, and an agent answering in its own thread would restart
    itself (`slack_events.py`).

    Only the job of the request names the agent (`verrou_org.travail_de_la_requete`,
    the job's delegation token; `db.agent_du_travail`), never an argument — a caller
    cannot file a message under someone else's agent. Outside a hosted run (a person
    in a conversation, a job queued by hand) nothing is noted. The message has GONE
    OUT when we get here: a failure is logged, it never turns the post into an error."""
    tss = [t for t in tss if t]
    if not tss:
        return
    from .. import db, verrou_org
    try:
        v = verrou_org.travail_de_la_requete()
        if v is None or v.job_id is None or v.org_id is None:
            return
        agent = db.agent_du_travail(v.job_id, v.org_id)
        if agent is None:
            return
        db.slack_agent_posts.noter(v.org_id, agent, v.job_id, channel, tss, texte,
                                   auteur, thread_ts)
    except Exception:  # noqa: BLE001 — the message is sent; replies only lose their route
        logger.exception("slack: message %s in %s not noted for replies", tss[0], channel)


def _refus(e, channel: Optional[str] = None) -> ValueError:
    """Translates a Slack rejection into an ACTIONABLE refusal — signals #510/#532/#549.

    A `Slack API error: not_in_channel` surfaced as is looks like an outage:
    the scheduled run of signal #549 failed two mornings in a row without
    anyone knowing the missing action was an invitation. Each code carries
    its way out here, and when oto CANNOT do it, it says so instead of letting
    people believe in an incident.

    ⚠️ The refusal is raised `from e`: `error_taxonomy` looks for the upstream status by
    WALKING UP the cause chain. Cutting the chain would make this credential 4xx
    count as a backend bug in Sentry.
    """
    code = getattr(e, "error", None) or "unknown"
    cible = ("`" + channel + "`") if channel else "this channel"

    if code == "missing_scope":
        # Slack ITSELF NAMES the missing right: we relay it, we do not guess
        # it (probed on 28/08: `needed=groups:history` on a private channel thread).
        needed = getattr(e, "needed", None)
        provided = getattr(e, "provided", None)
        if needed:
            msg = ("Slack refuses: the right `" + needed + "` is missing on this "
                   "workspace's token (several separated by a comma = any one is enough).")
        else:
            msg = ("Slack refuses for insufficient rights but does not name which: "
                   "compare the token's rights to the manifest on the connector card.")
        msg += " Grant it at " + _OU_DONNER_UN_DROIT + "."
        if provided:
            msg += " Rights seen by Slack on this token: " + provided + "."
        return ValueError(msg)

    if code == "not_in_channel":
        return ValueError(
            "Slack refuses: the app is not a member of " + cible + ". If the channel is "
            "PUBLIC, call `slack_join_channel` on it and call again. If it is PRIVATE, "
            "no Slack API lets you invite yourself: " + _GESTE_INVITATION + ".")

    if code == "channel_not_found":
        return ValueError(
            "Slack cannot see " + cible + ": either the ID is wrong, or it is a private "
            "channel the app is not in — Slack returns the same code in both cases. "
            "Check the ID with `slack_list_channels`; if it is private, "
            + _GESTE_INVITATION + ".")

    if code == "permission_denied":
        # User groups: the workspace setting "who can manage user groups" is
        # checked on the ACTOR. A bot token only passes when it is "everyone".
        return ValueError(
            "Slack refuses: this identity is not allowed to manage user groups in this "
            "workspace. A bot token (`author=\"app\"`) only can when the workspace lets "
            "everyone manage them; otherwise write with `author=\"me\"` from a member's OWN "
            "Slack connection (an org connection only acts as the app), that member "
            "being allowed by that setting (an admin, by default).")

    if code in ("plan_upgrade_required", "paid_teams_only"):
        return ValueError(
            "Slack refuses: user groups require a paid Slack plan (Pro or above) — this "
            "workspace does not have them.")

    if code == "no_such_subteam":
        return ValueError(
            "Slack does not know this user group ID. List them with "
            "`slack(op=\"usergroups\")` (`include_disabled=true` for disabled ones).")

    if code in ("name_already_exists", "handle_already_exists"):
        quoi = "name" if code.startswith("name") else "handle"
        return ValueError(
            "Slack refuses: this " + quoi + " is already used in the workspace (a "
            "disabled group keeps its " + quoi + "). Pick another, or update/enable the "
            "existing group — `slack(op=\"usergroups\", include_disabled=true)` finds it.")

    if code == "is_archived":
        return ValueError(
            "Slack refuses: " + cible + " is archived — it can no longer be posted to or "
            "joined. Unarchive it in Slack, or target another channel.")

    # Default: we NAME the Slack code without embellishing. And no "on this
    # channel" when the call did not target one (user lookup, DM opening)
    # — an invented location sends people looking in the wrong place.
    return ValueError("Slack refuses (" + code + ")"
                      + (" on " + cible if channel else "") + ".")


def register(mcp: FastMCP) -> None:
    from oto.tools.slack.client import SlackClient, SlackError
    connector_verify.register("slack", _verify)

    @contextmanager
    def _traduit(channel: Optional[str] = None):
        """Single seam for Slack calls: every upstream rejection comes out actionable.
        `except SlackError` is NARROW — a translation decision, not a safety net
        (the refusal stays loud, and its cause stays in the chain).

        ⚠️ It is a CONTEXT, deliberately: the version-skew probe
        (`test_tools_client_methods_exist`) only counts the attributes **called**
        on the client. Passing the method BY REFERENCE to a wrapper function
        would take the whole module out of its coverage SILENTLY — the hole lived through on
        apollo. Here `client.replies(…)` stays a literal call, hence verified
        against the pinned oto-core."""
        try:
            yield
        except SlackError as e:
            raise _refus(e, channel) from e

    def _jetons() -> tuple[Optional[str], Optional[str], bool, bool]:
        # Multi-field BYO (#25): bot token (xoxb-) and/or user token (xoxp-),
        # resolved by (sub, active org) via the credential cascade (user > active
        # group > active org). ⚠️ It is THIS result that says which identities the
        # account carries — never the client's attributes, which fall back to
        # environment keys when a field is empty.
        rc = access.resolve_credential("slack", want="byo")
        f = rc.fields
        bot = f.get("bot_token") or None
        user = f.get("user_token") or None
        if not bot and not user:
            # Legacy fallback: pre-multi-field credential = single raw token (not
            # JSON → rc.fields empty). Read via rc.key, routed by prefix.
            raw = (rc.key or "").strip()
            if raw.startswith("xoxb-"):
                bot = raw
            elif raw:
                user = raw
        partage = _partage(rc)
        if partage and bot:
            # A shared connection's user token is the installer's (`_partage`): never
            # used, for reads either — `open_dm` would open THEIR DM, history read
            # THEIR conversations. A legacy user-token-only org row keeps its reads.
            user = None
        return bot, user, rc.is_platform, partage

    def _client() -> tuple[SlackClient, bool]:
        # READS: the client routes itself (channel → bot, DM → user).
        bot, user, is_platform, _ = _jetons()
        return SlackClient(bot_token=bot, user_token=user,
                           default_as_user=bool(user)), is_platform

    def _ecrivain(author: Optional[str]) -> tuple[SlackClient, bool, str]:
        # WRITES: the author is chosen (`_auteur`), then held for the whole call.
        bot, user, is_platform, partage = _jetons()
        as_user = _auteur(bool(bot), bool(user), author, partage)
        client = SlackClient(bot_token=bot, user_token=user, default_as_user=as_user)
        return client, is_platform, "me" if as_user else "app"

    def _ou(client: SlackClient, channel: str) -> dict:
        """WHERE the message landed, in clear. An ID says neither its name nor who
        we are talking to: on 02/09, a reply meant for Tulina went to another
        person's channel. `shared_externally` says a third party reads. The message has GONE OUT when
        we get here: a read failure is named, it cancels nothing."""
        try:
            info = client.channel_info(channel).get("channel") or {}
        except (SlackError, ValueError) as e:
            return {"id": channel, "unknown": getattr(e, "error", None) or str(e)}
        return {"id": channel, "name": info.get("name"),
                "is_dm": bool(info.get("is_im")),
                "shared_externally": bool(info.get("is_ext_shared"))}

    @mcp.tool()
    def slack_post_message(
        channel: str,
        text: str,
        thread_ts: Optional[str] = None,
        author: Optional[Auteur] = None,
        unfurl: bool = True,
    ) -> dict:
        """Send a Slack message to a channel or DM. The response says who wrote
        (`_author`) and where it landed (`_channel`: name, DM, shared externally).

        ⚠️ **Long text is SPLIT, never truncated.** Above ~4,000 characters the
        text goes out as SEVERAL messages: the first one where you asked, each
        later part threaded under it. Nothing is lost. The response then carries
        `ts_all` (every ts, in order) and `split_into` next to `ts` — and `ts` is
        always the FIRST part, the one to reuse to reply in the same thread.
        A response with `ts` alone means one single message went out.

        **From a hosted agent's run**, a message posted as the app
        (`author="app"`) outside a thread is filed under that agent: a member's
        reply in its thread starts the agent again, with your message and their
        reply. Post as the app what people may answer.

        Read that before re-posting: a message can be deleted but NOT edited, so
        a caller who wrongly concludes it was truncated posts a duplicate instead
        of fixing one. `split_into` is what tells you, without reading the
        channel back — which some procedures forbid as a source.

        Args:
            channel: Channel ID (e.g. C0123456789), DM channel ID (D…), or an
                already-opened conversation. To DM a user by email, call
                `slack_find_user_by_email` + `slack_open_dm` first to get the channel ID.
            text: Message text (Slack mrkdwn supported).
            thread_ts: Parent message ts to reply into a thread.
            author: Who writes — `"me"` (as you, user token) or `"app"` (as the
                Slack app, bot token). Required when the workspace holds both
                tokens; with a single one, it is used and may be omitted. An
                org or team connection always writes as the app.
            unfurl: `false` = Slack shows NO preview under this message, no
                link card and no media (every part, when it is split). Default
                `true` = Slack's usual previews. A procedure that says "no
                link previews" passes `false`.
        """
        client, is_platform, qui = _ecrivain(author)
        with _traduit(channel):
            if unfurl:
                result = client.post_message(channel, text=text, thread_ts=thread_ts)
            else:
                # Both flags written out: the version probe sees them.
                result = client.post_message(channel, text=text, thread_ts=thread_ts,
                                             unfurl_links=False, unfurl_media=False)
        # BEFORE reading where it landed: Slack's event for it may already be on its way.
        _noter_pour_les_reponses(result.get("channel") or channel,
                                 list(result.get("ts_all") or [result.get("ts")]),
                                 text, qui, thread_ts)
        return {**result, "_author": qui, "_channel": _ou(client, channel)}

    @mcp.tool()
    def slack_delete_message(channel: str, ts: str,
                             author: Optional[Auteur] = None) -> dict:
        """Delete a message previously posted — by the same author that posted it.

        Args:
            channel: Channel ID.
            ts: Message timestamp returned by `slack_post_message`.
            author: Who writes — `"me"` (as you, user token) or `"app"` (as the
                Slack app, bot token). Required when the workspace holds both
                tokens; with a single one, it is used and may be omitted. An
                org or team connection always writes as the app.
        """
        client, is_platform, qui = _ecrivain(author)
        with _traduit(channel):
            result = client.delete_message(channel, ts)
        return {**result, "_author": qui}

    @mcp.tool()
    def slack_list_channels(types: str = "public_channel") -> dict:
        """List Slack channels visible to you.

        Args:
            types: Comma-separated channel types — public_channel, private_channel, mpim, im.
        """
        client, is_platform = _client()
        with _traduit():
            result = {"channels": client.list_channels(types=types)}
        return result

    @mcp.tool()
    def slack_read_history(
        channel: str,
        limit: int = 20,
        cursor: Optional[str] = None,
        oldest: Optional[str] = None,
        latest: Optional[str] = None,
        inclusive: bool = False,
    ) -> dict:
        """Read a channel/DM history — TOP-LEVEL messages only.

        Thread replies are NOT here: a parent carries `reply_count`/`latest_reply`
        but no reply body. Read them with `slack_read_thread`.

        Args:
            channel: Channel ID (C…/D…/G…).
            limit: Max messages (capped at 100 by Slack).
            cursor: Pagination cursor returned by a previous call.
            oldest: Only messages after this ts — exclusive. Windows the read
                instead of pulling a full page and filtering client-side.
            latest: Only messages before this ts — exclusive.
            inclusive: Also return the messages sitting exactly on oldest/latest.
        """
        client, is_platform = _client()
        with _traduit(channel):
            result = client.history(channel, limit=limit, cursor=cursor, oldest=oldest,
                                    latest=latest, inclusive=inclusive)
        return result

    @mcp.tool()
    def slack_read_thread(
        channel: str,
        thread_ts: str,
        limit: int = 50,
        cursor: Optional[str] = None,
        oldest: Optional[str] = None,
        latest: Optional[str] = None,
        inclusive: bool = False,
    ) -> dict:
        """Read the REPLIES of a Slack thread — where decisions actually land.

        `slack_read_history` returns top-level messages only; a parent with
        `reply_count > 0` hides its replies. This opens it.

        Returns `{parent, replies[], has_more, next_cursor}` — `parent` is kept out
        of `replies` because Slack repeats it on every page. Paginate by feeding
        `next_cursor` back as `cursor`; pages walk the thread newest → oldest.

        Given a REPLY's own ts (a `latest_reply` kept from an earlier sweep), the
        reply's thread is read instead, with the same window, and the answer says so:
        `thread_ts` (the parent actually read), `requested_ts` and a `note`. Keep
        `thread_ts` for the next pages.

        Args:
            channel: Channel ID (C…/D…/G…) holding the thread.
            thread_ts: `ts` of the PARENT message (a reply's `thread_ts`). A reply's
                own `ts` reads its thread, and the answer names the parent's ts.
            limit: Max replies per page (the parent does not count against it).
            cursor: `next_cursor` from a previous call.
            oldest: Only replies after this ts — exclusive.
            latest: Only replies before this ts — exclusive.
            inclusive: Also return replies sitting exactly on oldest/latest.
        """
        client, is_platform = _client()

        def _page(ts: str) -> tuple[list, dict]:
            with _traduit(channel):
                data = client.replies(channel, ts, limit=limit, cursor=cursor,
                                      oldest=oldest, latest=latest, inclusive=inclusive)
            msgs = data.get("messages") or []
            if msgs or not (oldest or latest or cursor):
                return msgs, data
            # A window can empty the answer: on a REPLY's ts, Slack's "thread" is that
            # one message, and `oldest` = its own ts (exclusive) filters it out — the
            # exact shape of a sweep restarting from a `latest_reply`.
            # One look WITHOUT the window says what this ts is: a reply (the caller
            # reads its parent), or a parent with nothing in the window (zero reply,
            # not an error).
            with _traduit(channel):
                sonde = client.replies(channel, ts, limit=1)
            return (sonde.get("messages") or [])[:1], {}

        msgs, data = _page(thread_ts)
        if not msgs:
            # Slack raises `thread_not_found` on an unknown ts; an empty `ok:true`
            # has never been observed. Refuse rather than return a phantom thread.
            raise _refus_entree(
                "Slack returns an empty thread for `" + thread_ts + "` in `" + channel
                + "` — check the parent message's ts with `slack_read_history`.")
        # ⚠️ Trap probed on 28/08: called with the `ts` of a REPLY, Slack does NOT return
        # the thread — it returns that single message, with `ok:true`. Returned as is, it says
        # "this thread has no reply": the reassuring message that excuses investigating,
        # on the most likely calling error. Slack names the parent: we read ITS thread,
        # with the caller's window, and say so.
        vrai_parent = _parent_d_une_reponse(msgs[0])
        if vrai_parent is None:
            return _fil(msgs, data)
        msgs, data = _page(vrai_parent)
        if not msgs or _parent_d_une_reponse(msgs[0]) is not None:
            raise _refus_entree(
                "`thread_ts`=`" + thread_ts + "` is a REPLY, not the thread's parent, and "
                "its thread (parent ts `" + vrai_parent + "`) could not be read. Call "
                "again with thread_ts=`" + vrai_parent + "`.")
        out = _fil(msgs, data)
        out["thread_ts"] = vrai_parent
        out["requested_ts"] = thread_ts
        out["note"] = ("`" + thread_ts + "` is a REPLY: its thread, parent ts `"
                       + vrai_parent + "`, was read instead, with the same window. Pass "
                       "thread_ts=`" + vrai_parent + "` on the next calls, `cursor` included.")
        return out

    @mcp.tool()
    def slack_join_channel(channel: str) -> dict:
        """Join a PUBLIC channel so reads and posts stop failing `not_in_channel`.

        A PRIVATE channel cannot be joined by any Slack API — it is refused here,
        naming the human gesture (`/invite`) instead of pretending. Already a
        member → `joined: false, already_member: true`, no call made.

        Args:
            channel: Channel ID (C…). Get it from `slack_list_channels`.
        """
        client, is_platform = _client()
        # Resolve the channel BEFORE attempting: `conversations.info` answers even
        # on a public channel we are not a member of (probed). This is what lets us
        # refuse a private channel without ever pretending to join it —
        # especially since `conversations.join` returns `missing_scope` on a private one as
        # on a wrong ID, so its error distinguishes nothing.
        with _traduit(channel):
            info = client.channel_info(channel).get("channel") or {}
        nom = info.get("name") or channel
        if info.get("is_archived"):
            raise _refus_entree(
                "#" + nom + " is archived: it can be neither joined nor posted to.")
        if info.get("is_member"):
            return {"channel": info, "joined": False, "already_member": True}
        if info.get("is_private"):
            raise _refus_entree(
                "#" + nom + " is a private channel: no Slack API lets you invite yourself "
                "(`conversations.join` only applies to public channels). "
                "oto cannot do it for you — " + _GESTE_INVITATION + ".")
        with _traduit(channel):
            client.join_channel(channel)
        return {"channel": info, "joined": True, "already_member": False}

    @mcp.tool()
    def slack_download_file(file_id: str, sheet: Optional[Union[int, str]] = None,
                            max_rows: Optional[int] = None) -> dict:
        """Download a file attached to a Slack message, by its file id.

        Get `file_id` from the `files[]` of a message returned by
        `slack_read_history`. The response depends on the file:
        - **small text** (Markdown/JSON/CSV/plain, ≤256 KB) → returned INLINE:
          `{encoding: "text", content}` — read it directly.
        - **PDF** → its extracted TEXT returned INLINE: `{encoding: "text",
          format: "pdf-text", content, pages, truncated}` plus `raw_url` (+
          `raw_expires_in`), a short-lived signed URL to the original PDF
          (layout, images). A scanned or protected PDF has no text: it comes
          back as a URL, with `text_unavailable` saying why.
        - **binary or large** (zip, image…) → uploaded to temporary storage
          and returned as a short-lived signed URL: `{encoding: "url", url,
          expires_in}` (seconds). Fetch the URL to get the bytes.
        - **spreadsheet (.xlsx)** → returned INLINE as CSV, one section per sheet:
          `{encoding: "text", format: "csv", content, sheets, sheet_names,
          truncated}`. Each section starts with `# sheet=<index> name="…"
          rows_total=… rows_rendered=… truncated=…` then the CSV rows (computed
          values, not formulas; dates ISO 8601). All sheets by default,
          `max_rows` rows each (default 200, max 5000), size-capped: if
          `truncated`, ask one sheet with `sheet` and/or raise `max_rows`; the
          render always carries `raw_url` (+ `raw_expires_in`, seconds): a
          short-lived signed URL to the FULL original file.

        Returns {filename, mimeType, size, encoding, content|url, expires_in?}.

        Args:
            file_id: Slack file id (e.g. F0BG…), from a message's `files[].id`.
            sheet: .xlsx only — the sheet to read, by name or 0-based index (see
                `sheet_names`). Omit for all sheets.
            max_rows: .xlsx only — rows per sheet (default 200, max 5000).
        """
        client, is_platform = _client()
        with _traduit():
            blob = client.fetch_file(file_id)
        sub = access.current_user_sub_or_raise()
        try:
            out = file_content.render_for_agent(
                blob["data"], blob["filename"], blob["mimetype"],
                sub=sub, prefix="slack-files", sheet=sheet, max_rows=max_rows)
        except (file_content.MediaUnavailable, file_content.SpreadsheetError) as e:
            raise _refus_entree(str(e))
        return out

    @mcp.tool()
    def slack_find_user_by_email(email: str) -> dict:
        """Look up a Slack user by email. Returns the user object (id, name, profile)."""
        client, is_platform = _client()
        with _traduit():
            result = client.find_user_by_email(email)
        return result

    @mcp.tool()
    def slack_open_dm(user: str) -> dict:
        """Open (or return) a DM channel with a user. Returns `{channel: {id: …}}`.

        Args:
            user: Slack user ID (U…). For email lookup, call `slack_find_user_by_email` first.
        """
        client, is_platform = _client()
        with _traduit():
            result = client.open_dm(user)
        return result

    @mcp.tool()
    def slack_add_reaction(channel: str, ts: str, name: str,
                           author: Optional[Auteur] = None) -> dict:
        """Add an emoji reaction to a message.

        Args:
            channel: Channel ID.
            ts: Message timestamp.
            name: Emoji name without colons (e.g. `white_check_mark`).
            author: Who writes — `"me"` (as you, user token) or `"app"` (as the
                Slack app, bot token). Required when the workspace holds both
                tokens; with a single one, it is used and may be omitted. An
                org or team connection always writes as the app.
        """
        client, is_platform, qui = _ecrivain(author)
        with _traduit(channel):
            result = client.add_reaction(channel, ts, name)
        return {**result, "_author": qui}

    # --- user groups (`@team` handles) — the connector's pair (17/10/2026) ---------
    # `slack` reads, `slack_act` acts: an MCP client authorizes TOOLS, so a write never
    # lives as an `op` of the reader (`lecture.py`, `tests/test_convention_act.py`). The
    # older Slack tools (one per gesture) join the pair when Slack is migrated.

    @mcp.tool(name="slack", annotations=LECTURE)
    def slack(
        op: Literal["usergroups", "usergroup_members"],
        usergroup: Optional[str] = None,
        include_users: bool = False,
        include_disabled: bool = False,
    ) -> dict:
        """Read Slack — ops: `usergroups`, `usergroup_members`. Changes: `slack_act`.

        - `usergroups`: the workspace's user groups (`@team` handles) →
          `{usergroups: [{id, name, handle, description, user_count, date_delete,
          prefs.channels, users?}]}` — `date_delete != 0` means disabled.
        - `usergroup_members`: `usergroup` → `{usergroup, users: [U…]}`, its member IDs.

        Requires the `usergroups:read` scope, and a paid Slack plan.

        Args:
            op: What to read (see above).
            usergroup: User group ID (S…) — `usergroup_members` only.
            include_users: `usergroups` — add each group's member IDs.
            include_disabled: Also return disabled groups (or read a disabled one).
        """
        if op == "usergroup_members":
            if not usergroup:
                raise _refus_entree("`op=\"usergroup_members\"` needs `usergroup` (S…) — "
                                    "find it with `slack(op=\"usergroups\")`.")
            if include_users:
                raise _refus_entree("`include_users` only applies to `op=\"usergroups\"`.")
        elif usergroup is not None:
            raise _refus_entree("`usergroup` only applies to `op=\"usergroup_members\"` — "
                                "`op=\"usergroups\"` lists every group.")
        client, is_platform = _client()
        if op == "usergroup_members":
            with _traduit():
                users = client.usergroup_users(usergroup, include_disabled=include_disabled)
            result = {"usergroup": usergroup, "users": users}
        else:
            with _traduit():
                groups = client.list_usergroups(include_users=include_users,
                                                include_disabled=include_disabled,
                                                include_count=True)
            result = {"usergroups": groups}
        return result

    @mcp.tool(name="slack_act", annotations=ACTE)
    def slack_act(
        op: Literal["create_usergroup", "update_usergroup", "disable_usergroup",
                    "enable_usergroup", "set_usergroup_members",
                    "add_usergroup_members", "remove_usergroup_members"],
        usergroup: Optional[str] = None,
        name: Optional[str] = None,
        handle: Optional[str] = None,
        description: Optional[str] = None,
        channels: Optional[list[str]] = None,
        users: Optional[list[str]] = None,
        author: Optional[Auteur] = None,
        dry_run: bool = False,
    ) -> dict:
        """Change Slack — ops: `create_usergroup`, `update_usergroup`,
        `disable_usergroup`, `enable_usergroup`, `set_usergroup_members`,
        `add_usergroup_members`, `remove_usergroup_members`. Reads: `slack`.

        - `create_usergroup`: `name` (+ `handle`, `description`, `channels`, `users`) —
          a group starts empty; `users` sets its members right after.
        - `update_usergroup`: `usergroup` + any of `name`, `handle` (without `@`),
          `description`, `channels` (default channel IDs, replaces the list).
        - `disable_usergroup` / `enable_usergroup`: `usergroup`. Disabling is how a
          group is emptied or retired; it is reversible and keeps the members.
        - `set_usergroup_members`: `usergroup` + `users` = the FULL new list (whoever
          is missing leaves). `add_usergroup_members` / `remove_usergroup_members`:
          `usergroup` + `users` to add or remove — the current list is read, then
          replaced. Every member op answers `{before, after, added, removed}`.

        `dry_run=true` validates and returns that diff (or the fields that would be
        sent) without writing. Slack refuses an empty member list: removing the
        last members is refused here — disable the group instead.

        Requires `usergroups:write`. ⚠️ A bot token (`author="app"`) can only write
        when the workspace lets everyone manage user groups; otherwise use
        `author="me"` with the user token of an allowed member.

        Args:
            op: The change to make (see above).
            usergroup: User group ID (S…), from `slack(op="usergroups")`. All ops but
                `create_usergroup`.
            name: Display name (`create_usergroup`, `update_usergroup`).
            handle: Mention handle without `@` (`create_usergroup`, `update_usergroup`).
            description: Description (`create_usergroup`, `update_usergroup`).
            channels: Default channel IDs (C…) (`create_usergroup`, `update_usergroup`).
            users: Member user IDs (U…) — the member ops; `create_usergroup` too.
            author: Who acts — `"me"` (user token) or `"app"` (bot token).
                Required when the workspace holds both tokens. An org or team
                connection always acts as the app.
            dry_run: Validate and preview, write nothing.
        """
        champs = {k: v for k, v in (("name", name), ("handle", handle),
                                    ("description", description),
                                    ("channels", channels)) if v is not None}
        if op == "create_usergroup":
            if not name:
                raise _refus_entree("`op=\"create_usergroup\"` needs `name`.")
        elif not usergroup:
            raise _refus_entree("`op=\"" + op + "\"` needs `usergroup` (S…) — "
                                "find it with `slack(op=\"usergroups\")`.")
        if op == "update_usergroup" and not champs:
            raise _refus_entree("`op=\"update_usergroup\"` needs at least one of `name`, "
                                "`handle`, `description`, `channels`.")
        membres_op = op in ("set_usergroup_members", "add_usergroup_members",
                            "remove_usergroup_members")
        if membres_op and not users:
            raise _refus_entree("`op=\"" + op + "\"` needs `users` (U… IDs).")
        if handle is not None:
            champs["handle"] = handle.lstrip("@")

        # The author is checked BEFORE anything is read: an ambiguous call is
        # refused without a single upstream request, dry_run included.
        client, is_platform, qui = _ecrivain(author)

        if membres_op:
            demandes = list(dict.fromkeys(u.strip() for u in users if u and u.strip()))
            with _traduit():
                avant = client.usergroup_users(usergroup, include_disabled=True)
            if op == "set_usergroup_members":
                apres = demandes
            elif op == "add_usergroup_members":
                apres = avant + [u for u in demandes if u not in avant]
            else:
                apres = [u for u in avant if u not in demandes]
            diff = {"usergroup": usergroup, "before": avant, "after": apres,
                    "added": [u for u in apres if u not in avant],
                    "removed": [u for u in avant if u not in apres]}
            if not apres:
                raise _refus_entree(
                    "This would leave the group with no members, which Slack refuses. "
                    "Use `op=\"disable_usergroup\"` to empty or retire the group.")
            if dry_run:
                return {**diff, "dry_run": True, "_author": qui}
            if not diff["added"] and not diff["removed"]:
                return {**diff, "changed": False, "_author": qui}
            with _traduit():
                client.set_usergroup_users(usergroup, apres)
            return {**diff, "changed": True, "_author": qui}

        if dry_run:
            apercu: dict = {"op": op, "dry_run": True, "_author": qui}
            if op == "create_usergroup":
                apercu["fields"] = champs
                if users:
                    apercu["users"] = users
            else:
                apercu["usergroup"] = usergroup
                with _traduit():
                    groupes = client.list_usergroups(include_disabled=True)
                actuel = next((g for g in groupes if g.get("id") == usergroup), None)
                if actuel is None:
                    raise _refus_entree(
                        "Slack does not know the user group `" + usergroup + "`. List them "
                        "with `slack(op=\"usergroups\", include_disabled=true)`.")
                if op == "update_usergroup":
                    courant = {"name": actuel.get("name"), "handle": actuel.get("handle"),
                               "description": actuel.get("description"),
                               "channels": (actuel.get("prefs") or {}).get("channels")}
                    apercu["changes"] = {k: {"from": courant.get(k), "to": v}
                                         for k, v in champs.items() if courant.get(k) != v}
                else:
                    apercu["currently_disabled"] = bool(actuel.get("date_delete"))
            return apercu

        if op == "create_usergroup":
            with _traduit():
                result = client.create_usergroup(
                    name, handle=champs.get("handle"), description=description,
                    channels=channels)
            cree = (result.get("usergroup") or {}).get("id")
            if users and cree:
                # The group EXISTS from here on: a member failure is named in the
                # response, it does not turn the creation into an error.
                try:
                    with _traduit():
                        result = client.set_usergroup_users(cree, users)
                except ValueError as e:
                    result = {**result, "members_error": str(e)}
        elif op == "update_usergroup":
            with _traduit():
                result = client.update_usergroup(
                    usergroup, name=name, handle=champs.get("handle"),
                    description=description, channels=channels)
        elif op == "disable_usergroup":
            with _traduit():
                result = client.disable_usergroup(usergroup)
        else:
            with _traduit():
                result = client.enable_usergroup(usergroup)
        return {**result, "_author": qui}
