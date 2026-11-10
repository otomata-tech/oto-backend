"""Registry declaration for the `reddit` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# reddit: reads posts/subreddits/comments WITH metrics (score,
# num_comments, upvote_ratio, pagination, nested tree) via the redditapis.com
# REST gateway. The official Reddit API is closed to self-serve
# (Responsible Builder Policy, late 2025) and anonymous JSON is blocked (403
# on datacenter IPs) → the old RSS connector (no metrics) is replaced.
# Shared platform key (Otomata pays for usage) + daily quota to bound the
# cost; BYO possible (the org sets its own redditapis key).
#
# ⚠️ THE PUBLISHER IS THE GATEWAY, NOT REDDIT (fixed on 2026-09-02). The listing
# used to announce "Reddit" as the publisher, with the reddit.com logo: we
# were attributing to Reddit a service Reddit does not provide — the call goes to
# api.redditapis.com, a reseller —, and the dependency on that intermediary
# appeared nowhere before installation. The NAME of the connector and of
# its tools does not change (callers depend on it): what changes is what
# the listing SAYS.
CONNECTOR = _c(
    "reddit", ["reddit"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    secret_kind="api_key", default_quota=100, platform_key_open=True,
    label="Reddit",
    help="posts, subreddits & comments with votes/metrics — via the "
         "third-party gateway redditapis.com, not Reddit's API",
    href="https://redditapis.com",
)

CATEGORY = "Web search & scraping"
PUBLISHER = "redditapis.com (third-party gateway)"
# Not Reddit's logo: the service rendered is not theirs. And redditapis.com
# is not a brand the user would recognize — monogram on the UI side.
SANS_LOGO_DE_MARQUE = True

DESCRIPTION = (
    "Read Reddit posts, subreddits and comments with their metrics "
    "(score, comment count, upvote ratio), through a third-party "
    "gateway (redditapis.com) — Reddit's official API is closed to self-"
    "serve. Shared platform key with a daily quota, or a key from your own "
    "gateway account."
)
