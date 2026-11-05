"""YouTube — a channel's Studio surface (oto-core `YouTubeClient`), per user, multi-account.

Ninth service of the Google account (2026-11-04): scopes `youtube.force-ssl`,
`youtube.readonly`, `yt-analytics.readonly`, granted from ITS card only. The channel is
the one the person picked at consent (a Google account can own several brand
channels); `op="channel"` says which.

**Two tools (the `<ns>` / `<ns>_act` pair, `lecture.py`)** — an MCP client authorizes
TOOLS, not arguments:
- `youtube` reads: accounts, channel, videos (uploads, private and scheduled included),
  video (details), playlists, playlist_items, comments (one video, or the whole channel,
  moderation queue included), replies, analytics, categories — `LECTURE`;
- `youtube_act` acts: update_video (metadata only, read-merge-write, `dry_run` diff),
  upload_video (a new video, resumable, private by default), set_thumbnail, reply,
  set_moderation, delete_comment, add_to_playlist — `ACTE`.

**One op, one change**: `set_thumbnail` never touches the title or description and
`update_video` never touches the thumbnail — an argument that does not belong to the op
is refused, never ignored.

**Quota** is the platform OAuth app's (10,000 units a day, shared): reads cost 1 unit a
page, each write ~50. Uploads are listed through the uploads playlist, never
`search.list`.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional

import requests
from fastmcp import FastMCP
from mcp.types import INVALID_PARAMS, ErrorData

from .. import access, file_source
from ..auth import google as google_oauth
from ..mcp_errors import McpError
from .lecture import ACTE, LECTURE

if TYPE_CHECKING:  # the annotation only — never evaluated at runtime
    from oto.tools.google.youtube import YouTubeClient

_SERVICE = "youtube"
_HTTP_TIMEOUT = (10, 25)          # under the 45 s of a REST invocation, token included
_TOKEN_TIMEOUT_S = 20
_THUMB_MAX_BYTES = 25 * 1024 * 1024
#: A video goes through the backend's RAM (file_source loads it whole), within the
#: 120 s of an MCP tool call: 100 MB, the same ceiling as an audio for transcription.
_VIDEO_MAX_BYTES = 100 * 1024 * 1024
_VIDEO_EXT = {"mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime",
              "webm": "video/webm", "mkv": "video/x-matroska", "avi": "video/x-msvideo",
              "mpg": "video/mpeg", "mpeg": "video/mpeg", "wmv": "video/x-ms-wmv",
              "flv": "video/x-flv", "3gp": "video/3gpp"}
_DEFAULT_METRICS = ("views,estimatedMinutesWatched,averageViewDuration,"
                    "subscribersGained,subscribersLost,likes,comments")

_OPS = ("accounts", "channel", "videos", "video", "playlists", "playlist_items",
        "comments", "replies", "analytics", "categories")
_ACT_OPS = ("update_video", "upload_video", "set_thumbnail", "reply", "set_moderation",
            "delete_comment", "add_to_playlist")

#: The arguments each op uses (besides `op` and `account`). Anything else given = refused.
_ARGS = {
    "accounts": set(), "channel": set(),
    "videos": {"max_results", "page_token"},
    "video": {"video_id", "video_ids"},
    "playlists": {"max_results", "page_token"},
    "playlist_items": {"playlist_id", "max_results", "page_token"},
    "comments": {"video_id", "moderation_status", "search_terms", "order",
                 "max_results", "page_token"},
    "replies": {"comment_id", "max_results", "page_token"},
    "analytics": {"start_date", "end_date", "metrics", "dimensions", "filters", "sort",
                  "max_results"},
    "categories": {"region_code"},
    "update_video": {"video_id", "title", "description", "tags", "category_id",
                     "default_language", "privacy_status", "publish_at", "embeddable",
                     "license", "public_stats_viewable", "made_for_kids",
                     "contains_synthetic_media", "dry_run"},
    "upload_video": {"video", "title", "description", "tags", "category_id",
                     "default_language", "privacy_status", "publish_at", "embeddable",
                     "license", "made_for_kids", "contains_synthetic_media",
                     "notify_subscribers"},
    "set_thumbnail": {"video_id", "image"},
    "reply": {"comment_id", "text"},
    "set_moderation": {"comment_ids", "moderation_status", "ban_author"},
    "delete_comment": {"comment_id"},
    "add_to_playlist": {"playlist_id", "video_id", "position"},
}
_DEFAULTS = {"max_results": None, "order": "time", "dry_run": False, "ban_author": False,
             "notify_subscribers": True}


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _check_args(op: str, given: dict) -> None:
    extra = sorted(k for k, v in given.items()
                   if k not in _ARGS[op] and v is not None and v != _DEFAULTS.get(k))
    if extra:
        raise _bad(f"op='{op}' does not take {', '.join(f'`{k}`' for k in extra)} — "
                   "nothing was done. One op, one change: call the op that owns them.")


def _need(value, name: str, op: str):
    if value is None or (isinstance(value, (str, list)) and not value):
        raise _bad(f"op='{op}' requires `{name}`")
    return value


# --- the client, per call ------------------------------------------------------------

def _client_for_user(account: Optional[str] = None):
    from oto.tools.google.youtube import YouTubeClient

    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account, service=_SERVICE)
    except RuntimeError as e:
        raise _bad(str(e))
    return YouTubeClient(creds.token, timeout=_HTTP_TIMEOUT)


async def _client(account: Optional[str] = None) -> YouTubeClient:
    try:
        return await asyncio.wait_for(asyncio.to_thread(_client_for_user, account),
                                      timeout=_TOKEN_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise _bad(f"Google did not respond within {_TOKEN_TIMEOUT_S}s "
                   "(token refresh) — try again.")


async def _run(fn: Callable[[], Any]) -> Any:
    from oto.tools.google.youtube import YouTubeError

    try:
        return await asyncio.to_thread(fn)
    except YouTubeError as e:
        raise _api_error(e)
    except ValueError as e:
        raise _bad(str(e))
    except requests.Timeout:
        raise _bad(f"YouTube did not answer within {_HTTP_TIMEOUT[1]}s — try again.")
    except requests.RequestException as e:
        raise _bad(f"YouTube is unreachable ({type(e).__name__}) — try again later.")


def _api_error(e) -> McpError:
    """A `YouTubeError` → what to do next, read from Google's REASONS, not the text."""
    http, detail = e.status_code, e.message
    if e.has("SERVICE_DISABLED", "accessNotConfigured"):
        msg = ("The YouTube API is not enabled in the Google Cloud project of the OAuth "
               "app that issued the connection (YouTube Data API v3, and YouTube "
               "Analytics API for analytics) — a setting for that app's administrator; "
               f"reconnecting changes nothing. Google detail: {detail}")
    elif e.has("ACCESS_TOKEN_SCOPE_INSUFFICIENT", "insufficientPermissions"):
        msg = ("This Google account has not authorized YouTube (or not all of it): "
               "connect YouTube from its card, on this account.")
    elif e.has("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded",
               "RESOURCE_EXHAUSTED"):
        msg = ("The YouTube API quota of the platform's Google app is used up for today "
               "(it resets at midnight Pacific time) — try again later, and prefer "
               "fewer pages / fewer writes.")
    elif e.has("uploadLimitExceeded"):
        msg = ("This channel reached its video upload limit for now (YouTube caps uploads "
               "per channel, more for unverified channels) — try again tomorrow.")
    elif e.has("uploadRateLimitExceeded"):
        msg = "This channel uploaded too many thumbnails recently — try again later."
    elif e.has("invalidImage", "mediaBodyRequired"):
        msg = f"YouTube refused the image: {detail} — send a JPEG or PNG (1280×720 recommended)."
    elif e.has("youtubeSignupRequired", "channelNotFound", "noLinkedYoutubeAccount"):
        msg = ("This Google account has no YouTube channel — connect the account (or "
               "brand channel) that owns it.")
    elif e.has("videoNotFound", "commentNotFound", "playlistNotFound", "parentNotFound",
               "notFound") or http == 404:
        msg = f"YouTube cannot find it, or this channel cannot see it: {detail}"
    elif http == 401:
        msg = "Google refused the access token — reconnect YouTube from its card."
    elif http == 403:
        msg = (f"YouTube refused: {detail} — the connected channel must own the video, "
               "comment or playlist; custom thumbnails also need a verified channel "
               "(youtube.com/verify).")
    elif http >= 500:
        msg = f"YouTube is temporarily unavailable (HTTP {http}) — try again later."
    else:
        reasons = ", ".join(e.reasons) or "?"
        msg = f"YouTube refused the request (HTTP {http}, {reasons}): {detail}"
    return _bad(msg)


# --- shaping: what the agent needs, not Google's whole resource -------------------------

def _thumb(snippet: dict) -> Optional[str]:
    t = snippet.get("thumbnails") or {}
    for k in ("maxres", "standard", "high", "medium", "default"):
        if (t.get(k) or {}).get("url"):
            return t[k]["url"]
    return None


def _video(v: dict, full: bool = False) -> dict:
    sn, st, stats = v.get("snippet") or {}, v.get("status") or {}, v.get("statistics") or {}
    out = {
        "id": v.get("id"), "title": sn.get("title"), "published_at": sn.get("publishedAt"),
        "privacy": st.get("privacyStatus"), "publish_at": st.get("publishAt"),
        "duration": (v.get("contentDetails") or {}).get("duration"),
        "views": stats.get("viewCount"), "likes": stats.get("likeCount"),
        "comments": stats.get("commentCount"),
        "url": f"https://www.youtube.com/watch?v={v.get('id')}",
        "studio_url": f"https://studio.youtube.com/video/{v.get('id')}/edit",
    }
    if full:
        out.update({
            "description": sn.get("description"), "tags": sn.get("tags") or [],
            "category_id": sn.get("categoryId"), "default_language": sn.get("defaultLanguage"),
            "thumbnail": _thumb(sn), "upload_status": st.get("uploadStatus"),
            "embeddable": st.get("embeddable"), "license": st.get("license"),
            "made_for_kids": st.get("madeForKids"),
        })
    return {k: v_ for k, v_ in out.items() if v_ is not None}


def _comment(cm: dict) -> dict:
    sn = cm.get("snippet") or {}
    return {"id": cm.get("id"), "author": sn.get("authorDisplayName"),
            "author_channel_id": (sn.get("authorChannelId") or {}).get("value"),
            "text": sn.get("textDisplay") or sn.get("textOriginal"),
            "likes": sn.get("likeCount"), "published_at": sn.get("publishedAt"),
            "moderation_status": sn.get("moderationStatus")}


def _thread(t: dict) -> dict:
    sn = t.get("snippet") or {}
    out = {"thread_id": t.get("id"), "video_id": sn.get("videoId"),
           "comment": _comment(sn.get("topLevelComment") or {}),
           "reply_count": sn.get("totalReplyCount", 0)}
    replies = (t.get("replies") or {}).get("comments") or []
    if replies:
        out["replies"] = [_comment(r) for r in replies]
    return out


def _image_mime(data: bytes, declared: str) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    raise _bad(f"the thumbnail must be a JPEG or PNG image (got {declared or 'unknown'}) "
               "— nothing was uploaded.")


def _video_mime(data: bytes, name: str, declared: str) -> str:
    """The upload's media type: the declared `video/*`, else the file extension, else
    the container's bytes (ISO MP4/MOV `ftyp`, WebM/MKV EBML), else octet-stream —
    which Google accepts and sniffs itself. An image, audio, text or PDF is refused."""
    if (declared or "").startswith("video/"):
        return declared
    ext = name.rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    if ext in _VIDEO_EXT:
        return _VIDEO_EXT[ext]
    if data[4:8] == b"ftyp":
        return "video/mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    if (declared or "").startswith(("image/", "audio/", "text/")) or data[:4] == b"%PDF":
        raise _bad(f"this file is not a video (got {declared or name or 'unknown'}) "
                   "— nothing was uploaded.")
    return "application/octet-stream"


def register(mcp: FastMCP) -> None:

    @mcp.tool(name="youtube", annotations=LECTURE)
    async def youtube(
        op: Literal["accounts", "channel", "videos", "video", "playlists",
                    "playlist_items", "comments", "replies", "analytics",
                    "categories"] = "channel",
        video_id: Optional[str] = None,
        video_ids: Optional[list[str]] = None,
        playlist_id: Optional[str] = None,
        comment_id: Optional[str] = None,
        moderation_status: Optional[Literal["heldForReview", "likelySpam", "published"]] = None,
        search_terms: Optional[str] = None,
        order: Literal["time", "relevance"] = "time",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        metrics: Optional[str] = None,
        dimensions: Optional[str] = None,
        filters: Optional[str] = None,
        sort: Optional[str] = None,
        region_code: Optional[str] = None,
        max_results: Optional[int] = None,
        page_token: Optional[str] = None,
        account: Optional[str] = None,
    ) -> dict:
        """Read a YouTube channel like YouTube Studio: its videos (private and scheduled
        included), their details and stats, comments (moderation queue included),
        playlists, and YouTube Analytics. Reads only — changes go through `youtube_act`.

        The channel is the one the connected Google account picked when it authorized
        YouTube; `op="channel"` says which. Each page costs 1 unit of a daily quota
        shared by the platform: do not loop over pages without need.

        Ops:
        - accounts: the Google accounts that authorized YouTube.
        - channel: id, title, handle, subscribers, views, video count, uploads playlist.
        - videos: the channel's uploads, newest first, with privacy, schedule and stats
          (`max_results` ≤ 50, `page_token`).
        - video: details of `video_id` (or up to 50 `video_ids`): title, description,
          tags, category, privacy, thumbnail url, stats, Studio link.
        - playlists / playlist_items (`playlist_id`): the channel's playlists, their videos.
        - comments: top-level comments with their first replies — on `video_id`, or on
          the whole channel when omitted; `moderation_status="heldForReview"` (or
          `likelySpam`) reads the review queue; `search_terms`, `order`.
        - replies: every reply to the top-level comment `comment_id`.
        - analytics: YouTube Analytics report. `start_date`/`end_date` YYYY-MM-DD
          (default: the last 28 days; data lags ~2 days); `metrics` comma-separated
          (default views, estimatedMinutesWatched, averageViewDuration,
          subscribersGained/Lost, likes, comments); `dimensions` e.g. `day`, `video`
          (with `sort="-views"` and `max_results`), `country`, `trafficSourceType`,
          `deviceType`; `filters` e.g. `video==VIDEO_ID`. Returns {columns, rows}.
        - categories: assignable video category ids for `region_code` (default US).

        Args:
            op: what to read.
            video_id: a video id (op video / comments).
            video_ids: up to 50 video ids (op video).
            playlist_id: a playlist id (op playlist_items).
            comment_id: a top-level comment id (op replies).
            moderation_status: comments in this moderation state (op comments).
            search_terms: only comments containing this text (op comments).
            order: "time" (default) or "relevance" (op comments).
            start_date: first day, YYYY-MM-DD (op analytics).
            end_date: last day, YYYY-MM-DD (op analytics).
            metrics: Analytics metrics, comma-separated (op analytics).
            dimensions: Analytics dimensions, comma-separated (op analytics).
            filters: Analytics filters, e.g. "video==abc123" (op analytics).
            sort: Analytics sort, e.g. "-views" (op analytics).
            region_code: ISO country code (op categories).
            max_results: page size (videos/playlists ≤ 50, comments ≤ 100).
            page_token: `next_page_token` of the previous page.
            account: email of the Google account to use — same choice as `_account`.
        """
        if op not in _OPS:
            raise _bad("op must be one of " + ", ".join(_OPS))
        _check_args(op, dict(
            video_id=video_id, video_ids=video_ids, playlist_id=playlist_id,
            comment_id=comment_id, moderation_status=moderation_status,
            search_terms=search_terms, order=order, start_date=start_date,
            end_date=end_date, metrics=metrics, dimensions=dimensions, filters=filters,
            sort=sort, region_code=region_code, max_results=max_results,
            page_token=page_token))

        if op == "accounts":
            sub = access.current_user_sub_or_raise()
            accounts = await asyncio.to_thread(
                google_oauth.reachable_accounts, sub, service=_SERVICE)
            return {"accounts": [{"email": a.get("google_email"),
                                  "is_default": a.get("is_default", False),
                                  "shared": a.get("shared")} for a in accounts]}

        c = await _client(account)

        if op == "channel":
            chans = await _run(c.my_channels)
            if not chans:
                raise _bad("This Google account has no YouTube channel.")
            return {"channels": [{
                "id": ch.get("id"), "title": (ch.get("snippet") or {}).get("title"),
                "handle": (ch.get("snippet") or {}).get("customUrl"),
                "subscribers": (ch.get("statistics") or {}).get("subscriberCount"),
                "views": (ch.get("statistics") or {}).get("viewCount"),
                "videos": (ch.get("statistics") or {}).get("videoCount"),
                "uploads_playlist_id": ((ch.get("contentDetails") or {})
                                        .get("relatedPlaylists") or {}).get("uploads"),
                "studio_url": f"https://studio.youtube.com/channel/{ch.get('id')}",
            } for ch in chans]}

        if op == "videos":
            page = await _run(lambda: c.list_uploads(max_results=max_results or 25,
                                                     page_token=page_token))
            ids = [(i.get("contentDetails") or {}).get("videoId") for i in page["items"]]
            ids = [i for i in ids if i]
            vids = await _run(lambda: c.get_videos(ids)) if ids else []
            by_id = {v.get("id"): v for v in vids}
            return {"channel_id": page["channel_id"],
                    "videos": [_video(by_id[i]) for i in ids if i in by_id],
                    "next_page_token": page["next_page_token"]}

        if op == "video":
            if (video_id is None) == (video_ids is None):
                raise _bad("op='video' takes exactly one of `video_id` or `video_ids`")
            ids = [video_id] if video_id else list(video_ids)
            vids = await _run(lambda: c.get_videos(ids))
            found = {v.get("id") for v in vids}
            out: dict = {"videos": [_video(v, full=True) for v in vids]}
            missing = [i for i in ids if i not in found]
            if missing:
                out["not_found"] = missing
            return out

        if op == "playlists":
            page = await _run(lambda: c.list_playlists(max_results=max_results or 25,
                                                       page_token=page_token))
            return {"playlists": [{
                "id": p.get("id"), "title": (p.get("snippet") or {}).get("title"),
                "privacy": (p.get("status") or {}).get("privacyStatus"),
                "videos": (p.get("contentDetails") or {}).get("itemCount"),
            } for p in page.get("items") or []], "next_page_token": page.get("nextPageToken")}

        if op == "playlist_items":
            pid = _need(playlist_id, "playlist_id", op)
            page = await _run(lambda: c.list_playlist_items(
                pid, max_results=max_results or 25, page_token=page_token))
            return {"items": [{
                "playlist_item_id": i.get("id"),
                "video_id": (i.get("contentDetails") or {}).get("videoId"),
                "title": (i.get("snippet") or {}).get("title"),
                "position": (i.get("snippet") or {}).get("position"),
                "privacy": (i.get("status") or {}).get("privacyStatus"),
            } for i in page.get("items") or []], "next_page_token": page.get("nextPageToken")}

        if op == "comments":
            channel_id = None
            if video_id is None:
                chans = await _run(lambda: c.my_channels(parts="id"))
                if not chans:
                    raise _bad("This Google account has no YouTube channel.")
                channel_id = chans[0].get("id")
            page = await _run(lambda: c.list_comment_threads(
                video_id=video_id, channel_id=channel_id,
                moderation_status=moderation_status, search_terms=search_terms,
                order=order, max_results=max_results or 20, page_token=page_token))
            return {"threads": [_thread(t) for t in page.get("items") or []],
                    "next_page_token": page.get("nextPageToken")}

        if op == "replies":
            cid = _need(comment_id, "comment_id", op)
            page = await _run(lambda: c.list_replies(cid, max_results=max_results or 50,
                                                     page_token=page_token))
            return {"replies": [_comment(r) for r in page.get("items") or []],
                    "next_page_token": page.get("nextPageToken")}

        if op == "analytics":
            end = end_date or date.today().isoformat()
            start = start_date or (date.fromisoformat(end) - timedelta(days=27)).isoformat()
            data = await _run(lambda: c.report(
                start_date=start, end_date=end, metrics=metrics or _DEFAULT_METRICS,
                dimensions=dimensions, filters=filters, sort=sort, max_results=max_results))
            return {"start_date": start, "end_date": end,
                    "columns": [h.get("name") for h in data.get("columnHeaders") or []],
                    "rows": data.get("rows") or []}

        # op == "categories"
        cats = await _run(lambda: c.list_video_categories(region_code or "US"))
        return {"categories": [{"id": x.get("id"), "title": (x.get("snippet") or {}).get("title")}
                               for x in cats]}

    @mcp.tool(name="youtube_act", annotations=ACTE)
    async def youtube_act(
        op: Literal["update_video", "upload_video", "set_thumbnail", "reply", "set_moderation",
                    "delete_comment", "add_to_playlist"],
        video_id: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[list[str]] = None,
        category_id: Optional[str] = None,
        default_language: Optional[str] = None,
        privacy_status: Optional[Literal["private", "unlisted", "public"]] = None,
        publish_at: Optional[str] = None,
        embeddable: Optional[bool] = None,
        license: Optional[Literal["youtube", "creativeCommon"]] = None,
        public_stats_viewable: Optional[bool] = None,
        made_for_kids: Optional[bool] = None,
        contains_synthetic_media: Optional[bool] = None,
        dry_run: bool = False,
        video: Optional[dict] = None,
        notify_subscribers: bool = True,
        image: Optional[dict] = None,
        comment_id: Optional[str] = None,
        comment_ids: Optional[list[str]] = None,
        text: Optional[str] = None,
        moderation_status: Optional[Literal["published", "heldForReview", "rejected"]] = None,
        ban_author: bool = False,
        playlist_id: Optional[str] = None,
        position: Optional[int] = None,
        account: Optional[str] = None,
    ) -> dict:
        """Change things on the connected YouTube channel, as its owner — each op does ONE
        change; an argument that belongs to another op is refused. ~50 quota units each
        (upload_video: its own bucket, 100 a day).

        Ops:
        - update_video: change only the metadata fields you pass (title, description,
          tags, category_id, default_language, privacy_status, publish_at, embeddable,
          license, public_stats_viewable, made_for_kids, contains_synthetic_media) on
          `video_id`; everything else is kept (the video is read, merged, written).
          Making a video public reaches its audience: confirm with the person first.
          `dry_run=true` returns the exact diff {field: {from, to}} without writing.
          `publish_at` (RFC 3339, e.g. 2026-12-01T09:00:00Z) schedules a PRIVATE video.
          Does NOT change the thumbnail.
        - upload_video: upload `video` (a file oto can reach, same shapes as `image`;
          ≤ 100 MB) as a NEW video of the channel, with `title` (required) and the same
          metadata fields as update_video. PRIVATE unless `privacy_status` says
          otherwise; `publish_at` schedules it (stays private until then). Publishing it
          public reaches the audience: confirm with the person first. 100 uploads a day
          for the whole platform. Then set its thumbnail with set_thumbnail.
        - set_thumbnail: upload `image` as the custom thumbnail of `video_id` — JPEG or
          PNG, 1280×720 recommended, the channel must be verified. `image` is a file
          oto can reach: {"kind": "project_file", "project_id", "file_id"},
          {"kind": "drive", "file_id"}, {"kind": "url", "url"}… Does NOT change the
          title or description.
        - reply: reply `text` as the channel to the top-level comment `comment_id`
          (a public reply: show it to the person first).
        - set_moderation: set `comment_ids` to `moderation_status` (published,
          heldForReview, rejected — rejected is final); `ban_author` with rejected only.
        - delete_comment: delete `comment_id`, a comment the channel wrote.
        - add_to_playlist: add `video_id` to `playlist_id` (optional `position`, 0 = top).

        Args:
            op: what to change.
            video_id: the video (update_video, set_thumbnail, add_to_playlist).
            video: the video file reference, ≤ 100 MB (upload_video).
            title: (new) title, ≤ 100 characters (update_video, upload_video).
            description: new description, ≤ 5,000 bytes (update_video).
            tags: the full new tag list — replaces the current one (update_video).
            category_id: from youtube op=categories (update_video).
            default_language: e.g. "en", "fr" (update_video).
            privacy_status: private, unlisted or public (update_video).
            publish_at: scheduled publication, RFC 3339; needs privacy private (update_video).
            embeddable: can be embedded on other sites (update_video).
            license: youtube or creativeCommon (update_video).
            public_stats_viewable: public view counts (update_video).
            made_for_kids: self-declared made for kids (update_video).
            contains_synthetic_media: altered or synthetic content disclosure (update_video).
            dry_run: return the diff without writing (update_video).
            notify_subscribers: notify subscribers of the new video, default true —
                only matters once it is public (upload_video).
            image: the thumbnail file reference (set_thumbnail).
            comment_id: the comment (reply, delete_comment).
            comment_ids: comments to moderate (set_moderation).
            text: the reply text (reply).
            moderation_status: published, heldForReview or rejected (set_moderation).
            ban_author: also ban the authors, with rejected (set_moderation).
            playlist_id: the playlist (add_to_playlist).
            position: 0-based position in the playlist (add_to_playlist).
            account: email of the Google account to use — same choice as `_account`.
        """
        if op not in _ACT_OPS:
            raise _bad("op must be one of " + ", ".join(_ACT_OPS))
        fields = dict(title=title, description=description, tags=tags,
                      category_id=category_id, default_language=default_language,
                      privacy_status=privacy_status, publish_at=publish_at,
                      embeddable=embeddable, license=license,
                      public_stats_viewable=public_stats_viewable,
                      made_for_kids=made_for_kids,
                      contains_synthetic_media=contains_synthetic_media)
        _check_args(op, dict(video_id=video_id, dry_run=dry_run, image=image, video=video,
                             notify_subscribers=notify_subscribers,
                             comment_id=comment_id, comment_ids=comment_ids, text=text,
                             moderation_status=moderation_status, ban_author=ban_author,
                             playlist_id=playlist_id, position=position, **fields))

        if op == "update_video":
            vid = _need(video_id, "video_id", op)
            if all(v is None for v in fields.values()):
                raise _bad("op='update_video' needs at least one field to change — "
                           "nothing was done.")
            c = await _client(account)
            out = await _run(lambda: c.update_video(vid, fields, dry_run=dry_run))
            res = {"video_id": vid, "dry_run": dry_run, "changes": out["changes"]}
            if not out["changes"]:
                res["note"] = "every value is already the current one — nothing was written."
            elif "video" in out:
                res["video"] = _video(out["video"], full=True)
            return res

        if op == "upload_video":
            src = _need(video, "video", op)
            name = _need(title, "title", op)
            try:
                rf = await asyncio.to_thread(file_source.resolve, src,
                                             max_bytes=_VIDEO_MAX_BYTES)
            except file_source.FileSourceError as e:
                raise _bad(f"the video file cannot be read: {e} — nothing was uploaded "
                           f"(at most {_VIDEO_MAX_BYTES // (1024 * 1024)} MB through oto).")
            mime = _video_mime(rf.data, rf.filename, rf.mime)
            meta = {k: v for k, v in fields.items() if k != "public_stats_viewable"}
            meta.update(title=name, privacy_status=privacy_status or "private",
                        category_id=category_id or "22", made_for_kids=bool(made_for_kids))
            c = await _client(account)
            created = await _run(lambda: c.upload_video(
                rf.data, mime, notify_subscribers=notify_subscribers, **meta))
            return {"video_id": created.get("id"), "uploaded": True, "bytes": len(rf.data),
                    "video": _video(created, full=True),
                    "note": "YouTube is processing it; set the thumbnail with "
                            "op=set_thumbnail once it shows in op=videos."}

        if op == "set_thumbnail":
            vid = _need(video_id, "video_id", op)
            src = _need(image, "image", op)
            try:
                rf = await asyncio.to_thread(file_source.resolve, src,
                                             max_bytes=_THUMB_MAX_BYTES)
            except file_source.FileSourceError as e:
                raise _bad(f"the thumbnail image cannot be read: {e} — nothing was uploaded.")
            mime = _image_mime(rf.data, rf.mime)
            c = await _client(account)
            res = await _run(lambda: c.set_thumbnail(vid, rf.data, mime))
            items = res.get("items") or [{}]
            return {"video_id": vid, "thumbnail_set": True, "bytes": len(rf.data),
                    "thumbnail": _thumb({"thumbnails": items[0]}),
                    "studio_url": f"https://studio.youtube.com/video/{vid}/edit"}

        if op == "reply":
            cid, body = _need(comment_id, "comment_id", op), _need(text, "text", op)
            c = await _client(account)
            return {"reply": _comment(await _run(lambda: c.reply_to_comment(cid, body)))}

        if op == "set_moderation":
            ids = list(_need(comment_ids, "comment_ids", op))
            status = _need(moderation_status, "moderation_status", op)
            c = await _client(account)
            await _run(lambda: c.set_moderation_status(ids, status, ban_author=ban_author))
            return {"comment_ids": ids, "moderation_status": status, "ban_author": ban_author}

        if op == "delete_comment":
            cid = _need(comment_id, "comment_id", op)
            c = await _client(account)
            await _run(lambda: c.delete_comment(cid))
            return {"deleted": cid}

        # op == "add_to_playlist"
        pid, vid = _need(playlist_id, "playlist_id", op), _need(video_id, "video_id", op)
        c = await _client(account)
        item = await _run(lambda: c.add_to_playlist(pid, vid, position=position))
        return {"playlist_item_id": item.get("id"), "playlist_id": pid, "video_id": vid,
                "position": (item.get("snippet") or {}).get("position")}
