"""Reddit read-only — posts, subreddits & comments with engagement metrics.

Backed by the **redditapis.com** REST proxy (bearer token) : score, num_comments,
upvote_ratio, real publication date, working `after` pagination, and the native
nested comment tree. Reddit's official Data API is closed to self-serve
registration (Responsible Builder Policy, late 2025) and the anonymous JSON is
IP-blocked — this proxy is the working path. Key resolved per call : member/org
key first, else the shared platform key (metered by daily quota).
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access
from ..connectors import verify as connector_verify


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion » — otomata-tech/oto#69. Couvre `auth` SEUL.

    `GET /api/reddit/search/communities` (déjà dans le client —
    `search_subreddits`), une recherche générique (`q="a"`, `limit=1`) qui ne
    dépend d'aucune cible existante : la passerelle redditapis.com n'expose ni
    `/me` ni solde. Comme `hunter`/`folk`, cette sonde ne compte PAS dans le
    quota plateforme (`record_platform_usage` n'est appelé que par les tools
    réels, jamais par `_verify` — même précédent).

    **Authentifié ≠ utilisable** (classe oto#69) : `RedditClient._get` lève déjà
    sur un refus (`RuntimeError` si le corps porte `error` en 200, sinon
    `raise_for_status`) — rien de plus fin à distinguer, une clé redditapis
    porte le périmètre entier de son quota.
    """
    from oto.tools.reddit import RedditClient

    RedditClient(api_key=fields["key"]).search_subreddits("a", limit=1)


def register(mcp: FastMCP) -> None:
    from oto.tools.reddit import RedditClient

    connector_verify.register("reddit", _verify)

    def _client() -> tuple[RedditClient, bool]:
        key, is_platform = access.resolve_api_key("reddit")
        return RedditClient(api_key=key), is_platform

    @mcp.tool()
    def reddit_subreddit(
        name: str,
        sort: str = "hot",
        limit: int = 25,
        time: Optional[str] = None,
        after: Optional[str] = None,
    ) -> dict:
        """List posts from a subreddit, with votes and comment counts.

        Returns items with score, num_comments, upvote_ratio, created (ISO), and
        a top-level `after` cursor (null when there is no further page).

        Args:
            name: Subreddit name (without /r/).
            sort: hot|new|top|rising|controversial.
            limit: Max posts (capped at 100).
            time: hour|day|week|month|year|all (only with sort=top|controversial).
            after: Pagination cursor from a previous call's `after`.
        """
        client, is_platform = _client()
        result = client.subreddit(name, sort=sort, limit=limit, time=time, after=after)
        if is_platform:
            access.record_platform_usage("reddit")
        return result

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
            after: Pagination cursor from a previous call's `after`.
        """
        client, is_platform = _client()
        result = client.search(
            query, subreddit=subreddit, sort=sort, time=time, limit=limit, after=after
        )
        if is_platform:
            access.record_platform_usage("reddit")
        return result

    @mcp.tool()
    def reddit_search_subreddits(query: str, limit: int = 25) -> dict:
        """Discover subreddits by name/description match, with subscriber counts.

        Returns items with name, title, description, and `subscribers` (to filter
        out low-signal communities).
        """
        client, is_platform = _client()
        result = client.search_subreddits(query, limit=limit)
        if is_platform:
            access.record_platform_usage("reddit")
        return result

    @mcp.tool()
    def reddit_post(
        url_or_id: str,
        comment_limit: int = 100,
        depth: int = 5,
    ) -> dict:
        """Fetch a Reddit post and its nested comment tree.

        Comments come back nested (each with its `replies`), with score and author.

        Args:
            url_or_id: Full reddit URL, /r/sub/comments/... permalink, or bare post id.
            comment_limit: Max number of comments to return.
            depth: Max depth of the reply tree walked (0 = top-level comments only).
        """
        client, is_platform = _client()
        result = client.post(url_or_id, comment_limit=comment_limit, depth=depth)
        if is_platform:
            access.record_platform_usage("reddit")
        return result
