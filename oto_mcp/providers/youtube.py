"""Registry declaration of the `youtube` connector — YouTube (Studio), on the Google account.

Single home of its entry: `providers/__init__.py` AGGREGATES it. The shape common
to the Google services lives at the account carrier (`providers/google.service`) —
here, what distinguishes THIS one (ninth service, 2026-11-04).
"""
from __future__ import annotations

from .google import service

# YouTube: the person authorises THIS service on their Google account, from this card
# (scopes `youtube.force-ssl`, `youtube.readonly`, `yt-analytics.readonly`). The
# channel is the one picked at consent — a Google account can own several brand
# channels, Google asks which one. Reads and writes act as that channel's owner; the
# Data API quota is the OAuth app's (per Google Cloud project), shared by every user.
CONNECTOR = service(
    "youtube",
    label="YouTube",
    help="your YouTube channel, as in YouTube Studio — videos (private and scheduled "
         "included), comments and moderation, playlists, Analytics; edit video details "
         "and thumbnails, reply to comments; granted on your Google account",
    href="https://studio.youtube.com",
)

CATEGORY = "Marketing"
LOGO_DOMAIN = "youtube.com"
DESCRIPTION = (
    "YouTube Studio, on your Google account: list your channel's videos with their "
    "privacy, schedule and stats, read comments and the moderation queue, playlists, "
    "and YouTube Analytics reports (views, watch time, subscribers, traffic sources). "
    "Upload a new video (private by default, up to 100 MB), edit a video's title, "
    "description, tags, privacy or schedule (with a dry-run diff), set its custom "
    "thumbnail, reply to and moderate comments, add videos to playlists."
)
