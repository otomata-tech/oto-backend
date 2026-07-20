"""Slack — outbound messaging + reads on behalf of the authenticated user.

Per-user : chaque user pose son propre **user token** (`xoxp-`) sur
`/account` (provider `slack`), ou un admin lui grant la clé plateforme
(bootstrappée depuis `SLACK_USER_TOKEN`). La clé est résolue par appel via
`access.resolve_api_key("slack")` — pas de token serveur partagé en clair.

Tous les appels passent par le user token (`as_user=True`) : les messages
apparaissent comme l'humain qui a installé l'app. ⚠️ Aujourd'hui ce `xoxp-` est
posé à la main et souvent partagé en clé plateforme → tout le monde poste comme
le même humain. La cible (per-user OAuth : clé app plateforme + `xoxp-` per-user,
+ mode bot `xoxb-` pour les comptes de service) est suivie en
otomata-tech/otomata-private#7.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access, connector_verify, file_content


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001 (config: contrat de sonde)
    """Sonde « tester la connexion » Slack (signal #217) : un token peut être POSÉ,
    authentifier, et pourtant manquer les scopes de lecture → `slack_list_channels`
    échoue en `missing_scope` et tout le reste est inatteignable (pas d'ID de channel).
    Deux étages, message actionnable : (1) `auth.test` passe avec TOUT token vivant
    quels que soient ses scopes → sépare « token mort » de « token OK, scope manquant » ;
    (2) une lecture réelle de channels (`channels:read`) — son `missing_scope` est LE
    diagnostic qui manquait."""
    from oto.tools.slack.client import SlackClient, SlackError

    bot = (fields.get("bot_token") or "").strip() or None
    user = (fields.get("user_token") or "").strip() or None
    if not bot and not user:  # credential mono-champ legacy (token brut) → routé au préfixe
        raw = next((str(v).strip() for v in fields.values() if str(v or "").strip()), "")
        if raw.startswith("xoxb-"):
            bot = raw
        elif raw:
            user = raw
    if not bot and not user:
        raise ValueError("aucun token Slack posé (bot_token `xoxb-` ou user_token `xoxp-`)")

    client = SlackClient(bot_token=bot, user_token=user, default_as_user=bool(user))
    try:
        client._request("POST", "auth.test")
    except SlackError as e:
        raise ValueError(
            f"token Slack invalide ({e.error}) — repose un `xoxb-`/`xoxp-` valide") from None
    try:
        client.list_channels(types="public_channel")
    except SlackError as e:
        if e.error == "missing_scope":
            raise ValueError(
                "token Slack authentifié mais SCOPES insuffisants : il manque "
                "`channels:read` (sans lui, aucun ID de channel n'est découvrable → "
                "`slack_read_history` inatteignable). Réinstalle l'app Slack avec "
                "`channels:read`, `groups:read`, `channels:history`, `groups:history`.") from None
        raise ValueError(f"lecture Slack échouée ({e.error})") from None


def register(mcp: FastMCP) -> None:
    from oto.tools.slack.client import SlackClient
    connector_verify.register("slack", _verify)

    def _client() -> tuple[SlackClient, bool]:
        # BYO multi-champs (#25) : bot token (xoxb-) et/ou user token (xoxp-),
        # résolus par (sub, org active) via la cascade credential (user > groupe
        # actif > org active). default_as_user suit la présence d'un user token
        # (préserve le comportement legacy : un token unique = user token).
        rc = access.resolve_credential("slack", want="byo")
        f = rc.fields
        bot = f.get("bot_token") or None
        user = f.get("user_token") or None
        if not bot and not user:
            # Fallback legacy : credential pré-multichamps = token unique brut (non
            # JSON → rc.fields vide). Lu via rc.key, routé par préfixe.
            raw = (rc.key or "").strip()
            if raw.startswith("xoxb-"):
                bot = raw
            elif raw:
                user = raw
        client = SlackClient(bot_token=bot, user_token=user,
                             default_as_user=bool(user))
        return client, rc.is_platform

    def _record_if_platform(is_platform: bool) -> None:
        if is_platform:
            access.record_platform_usage("slack")

    @mcp.tool()
    def slack_post_message(
        channel: str,
        text: str,
        thread_ts: Optional[str] = None,
    ) -> dict:
        """Send a Slack message to a channel or DM (appears as you).

        Args:
            channel: Channel ID (e.g. C0123456789), DM channel ID (D…), or an
                already-opened conversation. To DM a user by email, call
                `slack_find_user_by_email` + `slack_open_dm` first to get the channel ID.
            text: Message text (Slack mrkdwn supported).
            thread_ts: Parent message ts to reply into a thread.
        """
        client, is_platform = _client()
        result = client.post_message(channel, text=text, thread_ts=thread_ts)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    def slack_delete_message(channel: str, ts: str) -> dict:
        """Delete a message you previously posted.

        Args:
            channel: Channel ID.
            ts: Message timestamp returned by `slack_post_message`.
        """
        client, is_platform = _client()
        result = client.delete_message(channel, ts)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    def slack_list_channels(types: str = "public_channel") -> dict:
        """List Slack channels visible to you.

        Args:
            types: Comma-separated channel types — public_channel, private_channel, mpim, im.
        """
        client, is_platform = _client()
        result = {"channels": client.list_channels(types=types)}
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    def slack_read_history(
        channel: str,
        limit: int = 20,
        cursor: Optional[str] = None,
    ) -> dict:
        """Read recent messages from a channel or DM.

        Args:
            channel: Channel ID (C…/D…/G…).
            limit: Max messages (capped at 100 by Slack).
            cursor: Pagination cursor returned by a previous call.
        """
        client, is_platform = _client()
        result = client.history(channel, limit=limit, cursor=cursor)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    def slack_download_file(file_id: str) -> dict:
        """Download a file attached to a Slack message, by its file id.

        Get `file_id` from the `files[]` of a message returned by
        `slack_read_history`. The response depends on the file:
        - **small text** (Markdown/JSON/CSV/plain, ≤256 KB) → returned INLINE:
          `{encoding: "text", content}` — read it directly.
        - **binary or large** (zip, image, PDF…) → uploaded to temporary storage
          and returned as a short-lived signed URL: `{encoding: "url", url,
          expires_in}` (seconds). Fetch the URL to get the bytes.

        Args:
            file_id: Slack file id (e.g. F0BG…), from a message's `files[].id`.

        Returns {filename, mimeType, size, encoding, content|url, expires_in?}.
        """
        client, is_platform = _client()
        blob = client.fetch_file(file_id)
        sub = access.current_user_sub_or_raise()
        try:
            out = file_content.render_for_agent(
                blob["data"], blob["filename"], blob["mimetype"],
                sub=sub, prefix="slack-files")
        except file_content.MediaUnavailable as e:
            raise ValueError(str(e))
        _record_if_platform(is_platform)
        return out

    @mcp.tool()
    def slack_find_user_by_email(email: str) -> dict:
        """Look up a Slack user by email. Returns the user object (id, name, profile)."""
        client, is_platform = _client()
        result = client.find_user_by_email(email)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    def slack_open_dm(user: str) -> dict:
        """Open (or return) a DM channel with a user. Returns `{channel: {id: …}}`.

        Args:
            user: Slack user ID (U…). For email lookup, call `slack_find_user_by_email` first.
        """
        client, is_platform = _client()
        result = client.open_dm(user)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    def slack_add_reaction(channel: str, ts: str, name: str) -> dict:
        """Add an emoji reaction to a message.

        Args:
            channel: Channel ID.
            ts: Message timestamp.
            name: Emoji name without colons (e.g. `white_check_mark`).
        """
        client, is_platform = _client()
        result = client.add_reaction(channel, ts, name)
        _record_if_platform(is_platform)
        return result
