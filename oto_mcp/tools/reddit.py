"""Reddit read-only — flux RSS publics (no auth, no app).

Reddit a fermé l'API JSON publique (`www.reddit.com/*.json` → 403). Lecture via les
flux `*.rss` (Atom), servis sans authentification. Best-effort : rate-limité serré par
IP (429 → erreur claire) et sans score/votes/num_comments/arbre de commentaires.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    from oto.tools.reddit import RedditClient

    client = RedditClient()

    @mcp.tool()
    def reddit_subreddit(
        name: str,
        sort: str = "hot",
        limit: int = 25,
        time: Optional[str] = None,
        after: Optional[str] = None,
    ) -> dict:
        """List posts from a subreddit.

        Args:
            name: Subreddit name (without /r/).
            sort: hot|new|top|rising|controversial.
            limit: Max posts (capped at 100).
            time: hour|day|week|month|year|all (only with sort=top|controversial).
            after: Pagination cursor returned by a previous call.
        """
        return client.subreddit(name, sort=sort, limit=limit, time=time, after=after)

    @mcp.tool()
    def reddit_search(
        query: str,
        subreddit: Optional[str] = None,
        sort: str = "relevance",
        time: str = "all",
        limit: int = 25,
        after: Optional[str] = None,
    ) -> dict:
        """Search Reddit posts. If `subreddit` is set, restricts to that sub.

        Args:
            query: Search query.
            subreddit: Subreddit name to restrict the search to (optional).
            sort: relevance|hot|top|new|comments.
            time: hour|day|week|month|year|all.
            limit: Max results (capped at 100).
            after: Pagination cursor.
        """
        return client.search(
            query, subreddit=subreddit, sort=sort, time=time, limit=limit, after=after
        )

    @mcp.tool()
    def reddit_search_subreddits(query: str, limit: int = 25) -> dict:
        """Discover subreddits by name/description match."""
        return client.search_subreddits(query, limit=limit)

    @mcp.tool()
    def reddit_post(
        url_or_id: str,
        comment_limit: int = 100,
        depth: int = 5,
    ) -> dict:
        """Fetch a Reddit post and its comments tree.

        Args:
            url_or_id: Full reddit URL, /r/sub/comments/... permalink, or bare post id.
            comment_limit: Max number of comments to return.
            depth: Max depth of the comment tree.
        """
        return client.post(url_or_id, comment_limit=comment_limit, depth=depth)
