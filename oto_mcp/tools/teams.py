"""Teams — a person's Microsoft Teams, via Microsoft Graph (`TeamsClient`).

Credential = the PERSON's Microsoft 365 account (carrier `microsoft`, OAuth, delegated
permissions) that has authorized THIS service (scopes `TEAMS`); several linked accounts
are chosen by the generic `_account=` axis.

**Surface** — the connector's PAIR (`tools/lecture.py`): an MCP client authorizes a TOOL,
never an op, so "always allow" on what reads must never allow a post.
- `teams` READS: where one can read and post (op=teams/channels/chats), and the
  messages of a channel (`team_id` + `channel_id`) or of a chat (`chat_id`), exactly one
  of the two (op=list/replies);
  An image pasted in a message is not in its text: the message lists it under `images`
  (references), and op=image returns it as image content the agent sees;
- `teams_act` WRITES one, at the same place (op=post/reply). The old names
  (`teams_spaces`, `teams_message`, `teams_send`) refuse, naming the call to make
  (`outils_retires.RENOMMAGES`).

**Two consent levels** (`oto.tools.microsoft.teams`): the person consents alone to
teams, channels, posting and chats; READING a channel's messages (`list`/`replies` on a
channel) needs the administrator tier `scopes.TEAMS_ADMIN`, which only a tenant
administrator grants (`auth/microsoft.SERVICE_ADMIN_TIER`). Without it, the read is
refused with the approval link to send to that administrator (`admin_consent_url`) —
never a mute 403. An approval is only seen at a token renewal: when the vault does not
know it yet, or Graph still answers 403 while the vault says it is granted (a token
cached before the approval), the read renews the token ONCE, then decides.

⚠️ `teams_act` WRITES: a message is visible at once to the channel or chat members;
Teams has no draft.
"""
from __future__ import annotations

import html
import re
from typing import Literal, Optional, Union

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from .. import access
from ._microsoft_graph import (bad, markdown_html, need, raise_refusal, raw, refuse_ignored,
                               run, token)
from .lecture import ACTE, LECTURE

#: The Microsoft service these tools call: its scopes, its card (`auth/microsoft`).
_SERVICE = "teams"
_LABEL = "Teams"
_BALISE = re.compile(r"<[^>]+>")
#: An image up to this size comes back as image content; beyond, as a signed URL (the
#: clients refuse an inline image of a few MB).
_IMAGE_INLINE_MAX = 3 * 1024 * 1024

def _equipe(t: dict) -> dict:
    return {k: t.get(k) for k in ("id", "displayName", "description")}


def _canal(ch: dict) -> dict:
    return {k: ch.get(k) for k in ("id", "displayName", "description", "membershipType",
                                   "webUrl")}


def _conversation(ch: dict) -> dict:
    return {k: ch.get(k) for k in ("id", "topic", "chatType", "lastUpdatedDateTime",
                                   "webUrl")}


def _texte(corps: Optional[dict]) -> Optional[str]:
    """A message body as text: Teams sends HTML; the view keeps what a person reads."""
    contenu = (corps or {}).get("content")
    if contenu is None or str((corps or {}).get("contentType")).lower() != "html":
        return contenu
    return html.unescape(_BALISE.sub("", contenu)).strip()


def _lieu(op: str, team_id: Optional[str], channel_id: Optional[str],
          chat_id: Optional[str]) -> tuple:
    """The place of a message: `(True, team, channel)` for a channel, `(False, None,
    None)` for a chat — exactly one of the two, refused otherwise (never guessed)."""
    canal = team_id is not None or channel_id is not None
    if canal == (chat_id is not None):
        raise bad("name the place: `team_id` + `channel_id` (a channel) OR `chat_id` "
                  "(a chat) — exactly one of the two.")
    if not canal:
        return False, None, None
    return True, need(team_id, "team_id", op), need(channel_id, "channel_id", op)


def _message(m: dict) -> dict:
    """A message's view: who, when, what, where to answer it, and its pasted images
    (`images`, only when it has some: the references op=image takes)."""
    from oto.tools.microsoft import image_refs

    auteur = m.get("from") or {}
    qui = (auteur.get("user") or auteur.get("application") or {}).get("displayName")
    vue = {
        "id": m.get("id"),
        "from": qui,
        "createdDateTime": m.get("createdDateTime"),
        "subject": m.get("subject"),
        "text": _texte(m.get("body")),
        "replyToId": m.get("replyToId"),
        "messageType": m.get("messageType"),
        "attachments": len(m.get("attachments") or []),
        "webUrl": m.get("webUrl"),
    }
    corps = m.get("body") or {}
    if str(corps.get("contentType")).lower() == "html":
        images = image_refs(corps.get("content") or "")
        if images:
            vue["images"] = images
    return vue


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.microsoft import TeamsClient

    from ..auth import microsoft as ms_auth

    def _client(renew: bool = False) -> TeamsClient:
        """The Graph client of THIS caller — `renew=True` with a token renewed
        whatever the cache holds."""
        return TeamsClient(token(_SERVICE, renew=renew))

    def _run(fn):
        return run(fn, _LABEL)

    def _meta() -> dict:
        """The vault `meta` of the account this call designates (`_account=`…)."""
        try:
            return ms_auth.resolve_account(access.current_user_sub_or_raise(),
                                           _SERVICE)[1]
        except RuntimeError as e:
            raise bad(str(e))

    def _refus_palier_admin(meta: dict):
        """Reading a channel without the administrator tier: say why, and give the link
        to send to the administrator of the account's directory."""
        try:
            lien = ms_auth.admin_consent_url(access.current_user_sub_or_raise(),
                                             (_SERVICE,), meta.get("tenant"),
                                             connector=_SERVICE)
        except (RuntimeError, ValueError) as e:
            raise bad("Reading channel messages requires your Microsoft admin's approval, "
                      f"and the approval link could not be built: {e}")
        return bad(
            "Reading channel messages requires your Microsoft admin's approval: Teams lets "
            "a person post in a channel and read their chats on their own, but reading a "
            "channel's messages needs a Microsoft 365 administrator to approve oto once "
            "for the whole organization (permission ChannelMessage.Read.All). Send this "
            f"link to that administrator (valid seven days): {lien} — once approved, "
            "retry: nothing to reconnect. Chats (`chat_id`) and posting work without it.")

    def _lecture_canal(lire):
        """`lire(c)` — a read of a channel's messages, under the administrator tier.

        Refused with the approval link when the account does not hold it. The vault
        learns an approval at a token renewal only: when it does not know it yet, the
        token is renewed ONCE before refusing; when it does but Graph answers 403 (a
        token cached before the approval, in this process or another one), renewed
        ONCE before retrying. Never more than one renewal per call."""
        palier = ms_auth.admin_scopes(_SERVICE)
        renouvele = False
        meta = _meta()
        if not ms_auth.has_scopes(meta, palier):
            c = _client(renew=True)
            renouvele = True
            meta = _meta()
            if not ms_auth.has_scopes(meta, palier):
                raise _refus_palier_admin(meta)
        else:
            c = _client()
        try:
            return lire(c)
        except UpstreamHTTPError as e:
            if e.status_code != 403 or renouvele:
                raise_refusal(e, _LABEL)
        c = _client(renew=True)
        return _run(lambda: lire(c))

    def _image(ref: str) -> ToolResult:
        """A pasted image: a channel's under the administrator tier, like its messages."""
        from ..file_content import MediaUnavailable, render_for_agent
        from .web import image_format

        if "/channels/" in ref:
            octets = _lecture_canal(lambda c: c.get_hosted_content(ref))
        else:
            c = _client()
            octets = _run(lambda: c.get_hosted_content(ref))
        fmt = image_format(octets)
        if fmt is not None and len(octets) <= _IMAGE_INLINE_MAX:
            from fastmcp.utilities.types import Image

            # Plain text, not JSON: no field to redact, and the image block is never
            # rebuilt away by a middleware that re-serialises a JSON payload (web_image).
            return ToolResult(content=[
                TextContent(type="text", text=f"Image pasted in Teams — {fmt}, "
                                              f"{len(octets)} bytes."),
                Image(data=octets, format=fmt).to_image_content()])
        try:
            servi = render_for_agent(octets, f"teams-image.{fmt or 'bin'}",
                                     f"image/{fmt}" if fmt else "application/octet-stream",
                                     sub=access.current_user_sub_or_raise(),
                                     prefix="teams-images")
        except MediaUnavailable as e:
            raise bad(str(e))
        return {"image": servi, "note": (
            "over 3 MB: served as a signed URL" if fmt else
            "not a png/jpeg/gif/webp image: served as a signed URL")}

    @mcp.tool(name="teams", annotations=LECTURE)
    def teams(
        op: Literal["teams", "channels", "chats", "list", "replies", "image"] = "teams",
        team_id: Optional[str] = None,
        channel_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        message_id: Optional[str] = None,
        image: Optional[str] = None,
        limit: int = 50,
        full: bool = False,
    ) -> Union[dict, ToolResult]:
        """Reads Microsoft Teams: where the person can read and post, and the messages
        of a channel or a chat. Posting and replying is `teams_act`.

        `op` — where:
        - **"teams"** (default): the teams the person is a member of
          ({teams: [{id, displayName, description}]}).
        - **"channels"**: the channels of `team_id` the person can see
          ({channels: [{id, displayName, membershipType, webUrl}]}).
        - **"chats"**: the person's one-on-one, group and meeting chats, at most
          `limit` ({chats: [{id, topic, chatType, lastUpdatedDateTime}]}).

        `op` — the messages of a place, a CHANNEL (`team_id` + `channel_id`) OR a CHAT
        (`chat_id`), exactly one of the two:
        - **"list"**: the latest messages, at most `limit` — a channel's root messages
          (without their replies), or a chat's messages.
        - **"replies"**: the replies to the channel message `message_id` (a chat has no
          threads).
        Ids carry `:` and `@` (`19:…@thread.tacv2`): pass them as returned. A message
        with pasted images (screenshots…) lists them under `images`: their text is not
        in the message, look at them with op="image".

        `op` — a pasted image:
        - **"image"**: the image `image` (one of a message's `images`, as returned),
          as image content you can look at, after one text line (format, size). Over
          3 MB, or not a png/jpeg/gif/webp: a signed URL instead.

        ⚠️ Reading a CHANNEL's messages requires your Microsoft admin's approval, once for
        the whole organization: without it the call is refused and returns the approval
        link to send to the administrator. Chats do not need it.

        Args:
            op: teams (default) | channels | chats | list | replies | image.
            team_id: op="channels" — the team (from op="teams"); op="list"/"replies" —
                a channel's team (with `channel_id`).
            channel_id: the channel (with `team_id`).
            chat_id: the chat, instead of a channel (from op="chats").
            message_id: op="replies" — the channel message (from op="list").
            image: op="image" — a reference from a message's `images`.
            limit: op="chats"/"list"/"replies" — max items (default 50).
            full: return Graph's raw objects instead of the trimmed view.
        """
        if op == "image":
            refuse_ignored(op, team_id=team_id, channel_id=channel_id, chat_id=chat_id,
                           message_id=message_id)
            if full:
                raise bad("`full` does not apply to op='image'.")
            return _image(need(image, "image", op))
        refuse_ignored(op, image=image)
        if op in ("teams", "channels", "chats"):
            # Refused BEFORE any token: an argument this op does not use is an intent
            # error (a message's place on a discovery op).
            refuse_ignored(op, channel_id=channel_id, chat_id=chat_id,
                           message_id=message_id)
            if op == "channels":
                equipe = need(team_id, "team_id", op)
                c = _client()
                canaux = _run(lambda: c.list_channels(equipe))
                return {"team_id": equipe, "channels": [raw(ch) if full else _canal(ch)
                                                        for ch in canaux],
                        "count": len(canaux)}
            refuse_ignored(op, team_id=team_id)
            c = _client()
            if op == "teams":
                equipes = _run(c.list_joined_teams)
                return {"teams": [raw(t) if full else _equipe(t) for t in equipes],
                        "count": len(equipes)}
            chats = _run(lambda: c.list_chats(limit=limit))
            return {"chats": [raw(ch) if full else _conversation(ch) for ch in chats],
                    "count": len(chats)}
        if op not in ("list", "replies"):
            raise bad("op must be 'teams', 'channels', 'chats', 'list', 'replies' or 'image' — "
                      "posting and replying is `teams_act`.")
        canal, equipe, fil = _lieu(op, team_id, channel_id, chat_id)
        vue = raw if full else _message

        def _liste(messages: list) -> dict:
            return {"messages": [vue(m) for m in messages], "count": len(messages)}

        if op == "list":
            refuse_ignored(op, message_id=message_id)
            if not canal:
                c = _client()
                return _liste(_run(lambda: c.list_chat_messages(chat_id, limit=limit)))
            return _liste(_lecture_canal(
                lambda c: c.list_channel_messages(equipe, fil, limit=limit)))
        if not canal:
            raise bad("a chat has no threads: read it with op='list'.")
        racine = need(message_id, "message_id", op)
        return _liste(_lecture_canal(
            lambda c: c.list_replies(equipe, fil, racine, limit=limit)))

    @mcp.tool(name="teams_act", annotations=ACTE)
    def teams_act(
        op: Literal["post", "reply"] = "post",
        team_id: Optional[str] = None,
        channel_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        message_id: Optional[str] = None,
        body: Optional[str] = None,
        full: bool = False,
    ) -> dict:
        """⚠️ WRITES in Microsoft Teams, in the person's name: a message is visible AT
        ONCE to the channel or chat members — Teams has no draft. Reading is `teams`.

        The place is a CHANNEL (`team_id` + `channel_id`, from `teams`) OR a CHAT
        (`chat_id`) — exactly one of the two.

        `op`:
        - **"post"** (default): a new message (`body`, markdown): a new thread in the
          channel, or a message in the chat.
        - **"reply"**: a reply (`body`) in the thread of the channel message
          `message_id` (from `teams(op="list")`). In a chat, use op="post".

        Posting needs no administrator's approval.

        Args:
            op: post (default) | reply.
            team_id: a channel's team (with `channel_id`).
            channel_id: the channel (with `team_id`).
            chat_id: the chat, instead of a channel.
            message_id: op="reply" — the channel message to reply to.
            body: the message, in markdown.
            full: return Graph's raw message instead of the trimmed view.
        """
        if op not in ("post", "reply"):
            raise bad("op must be 'post' or 'reply'.")
        # Refused BEFORE any credential resolution: a post without a place or a body
        # never reaches Graph.
        canal, equipe, fil = _lieu(op, team_id, channel_id, chat_id)
        contenu = markdown_html(need(body, "body", op))
        vue = raw if full else _message
        if op == "post":
            refuse_ignored(op, message_id=message_id)
            c = _client()
            if canal:
                return vue(_run(lambda: c.post_channel_message(equipe, fil,
                                                               html=contenu)))
            return vue(_run(lambda: c.post_chat_message(chat_id, html=contenu)))
        if not canal:
            raise bad("a chat has no threads: post in it with op='post'.")
        racine = need(message_id, "message_id", op)
        c = _client()
        return vue(_run(lambda: c.reply_channel_message(equipe, fil, racine,
                                                        html=contenu)))
