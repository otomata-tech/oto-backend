"""Unipile — hosted LinkedIn & WhatsApp (search / scrape / messaging).

⚠️ **One module, SEVEN connectors** since the 2026-08-28 split: `unipile` (the
account, which carries the key) and its six channels, of which `linkedin_unipile` is
served here. The other five have their own `tools/<channel>.py`, which calls the
shared messaging factory from here. See `docs/unipile.md` §The split.

The key is resolved per call **under the CHANNEL's connector**
(`unipile_client` → `access.resolve_credential(<channel>)`), not under `unipile`: this
is what makes the ACL and the activation OF THAT CHANNEL bite, since the gate reads the
name it is given. The key itself stays the account's — the delegation
(`Connector.credential_of`) normalizes within the cascade. The dsn (API v2: gateway
`api.unipile.com`) comes from the BYO credential's config; the platform key takes the
oto-core client default (api.unipile.com), which reads no env.

Why alongside the `linkedin` browser connector: the session lives at Unipile
(real Chrome + residential proxy), which sidesteps the TLS fingerprint and the
local browser's session isolation (issue #5) — at the price of a paid SaaS.
"""
from __future__ import annotations

import logging
import time
import unicodedata
from typing import Literal, Optional
from urllib.parse import unquote, urlsplit

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INTERNAL_ERROR, INVALID_PARAMS

from .. import access, db, providers, session_org, status_hints
from ..connectors import flow as connector_flow
from ..connectors import verify as connector_verify
from . import unipile_messagerie
from .lecture import ACTE, LECTURE

logger = logging.getLogger(__name__)

# The feed (LinkedIn home) is served LIVE (oto#156): one Voyager page per call,
# sorted by date in memory, nothing copied into the datastore. The old `linkedin-feed`
# mirror resynced by REPLACING its rows — any annotation placed between two syncs was
# lost without a trace. Decision of 05/10: no more mirror, no more annotations;
# pagination is LinkedIn's (`cursor`).
_FEED_SORT_ORDER = "MEMBER_SETTING"  # honors the sort chosen on the LinkedIn home

# TRIAGE view of the feed — the default of `op="feed"` (signal #384). Measured on 40 real posts:
# the raw post costs ~1,650 characters (66 KB for a page of 40, beyond the cap of an
# MCP result — the harness falls back to a file and the agent has to re-sort with jq before
# starting its work). The text alone weighs 60%, the rest is redundancy
# (`urn` == the tail of `post_url`) and columns that are useless for triage.
# `fields=["*"]` / `text_max_chars=None` return the raw post. What changes is the default
# READING — ADR 0047 §Amendment of 11/08: the lazy path must be the right one.
_FEED_DEFAULT_FIELDS = (
    "urn", "post_url",                              # address the post + cite it
    "author_name", "author_headline",               # who is speaking (the guide sorts on it)
    "posted_at",                                    # freshness
    "text",                                         # what it is about (truncated)
    "content_type", "content_title",                # ...and what the post is MADE of
    "reactions_count", "comments_count",            # traction
    "is_repost", "original_author_name",            # repost ⟹ react on the original
    "original_text", "original_content_type",       # ...and the substance IS in the original
    "feed_reason",                                  # why it is in your feed
)
# Left out of the default: `posted_relative` (derivable from `posted_at`),
# `surfaced_by`/`comment_authors` (empty on 40/40 of the measured posts).
_FEED_ADDRESSING = ("urn",)         # never projected out of the result: without it we can
                                    # no longer open the post nor deduplicate it

# DEFAULT excerpt length for any long text returned in a LIST by this connector —
# feed (#384) as well as a member's posts/comments (#281). A single number for the whole
# `linkedin_*` family: the agent learns it once. A post's header is enough to
# triage it (that is what the #384 agent had kept by hand with jq); the cut is
# MARKED (`text_truncated`) and `text_max_chars=None` returns the full text.
_TEXT_EXCERPT_CHARS = 600
# ALL free-text fields of an item, not just `text`: since oto-core
# v1.80.0 a repost also carries `original_text` (the actual substance, when `text`
# only contains the resharer's remark). Capping only `text` would let the second
# through whole and void the cap on exactly the posts with the most
# to read. Each cut is marked under its own name (`original_text_truncated`).
_TEXTUAL_FIELDS = ("text", "original_text")

# --- Upstream LinkedIn rate-limit discipline (Unipile). EMPIRICAL: Unipile's 429 is a
# LAYERED rate-limit ("We only allow 1 / 10 / 100 requests") whose `Retry in N`
# FOLLOWS THE ACCOUNT'S RECENT CADENCE — it is not a constant. Two measurements, not to be
# confused: 2026-07-21, moderate bursts → 3-38s (455 calls/h of which 187 OK, 429 recovered
# in ~40s); 2026-08-07, AFTER a pilot that chained calls → ~55 min then ~53 min on a single
# isolated call (#361). So no hard cap of 100/12h, but no promised "few seconds" either:
# the delay to announce is THE ONE Unipile returns, never an average. We
# FOLLOW Unipile's signal: on a 429 we arm a cooldown = ITS OWN `retry_after`
# (parsed by oto-core, seconds included), capped, and we refuse the sub's scrapes until then
# — a micro-backoff that self-paces the burst without hammering (hammering degrades into timeouts
# then triggers a checkpoint/disconnects the account). + company-profile cache (most
# constrained route, ~100/window) = 0 upstream calls, 0 quota. PROCESS-LOCAL guards (single loop).
_CHAT_LIST_MAX = 25      # max page of `linkedin_unipile op=list` (#873, LinkedIn)
_ENGAGEMENT_DEFAULT = 100  # people returned by `linkedin_unipile_post op=engagement`
_ENGAGEMENT_MAX = 500      #  ... and its upper bound (oto#177)
_ENGAGEMENT_BUDGET_S = 20  # max duration of the page loop (never a call that freezes)
_RATE_LIMIT_UNTIL: dict[str, float] = {}   # sub -> epoch when the cooldown ends
_COMPANY_CACHE: dict[tuple, tuple] = {}     # (sub, ident_lower) -> (epoch, result)
_COMPANY_TTL = 6 * 3600                      # company profiles ~static → 6h
_COMPANY_CACHE_MAX = 3000                    # memory bound (coarse purge beyond it)
_RL_DEFAULT_SECS = 30    # 429 without a readable delay → short backoff (bursts = a few s)
_RL_MAX_SECS = 3600      # cap: a "Retry in 12 hours" (rare/misleading) does not lock
                         #  the day — at worst we re-probe after 1h (self-correcting)


def _fmt_wait(secs: float) -> str:
    s = max(1, int(secs) + 1)
    return f"~{s}s" if s < 90 else f"~{s // 60 + 1} min"


def _rate_limited(wait_secs: float, detail: str) -> McpError:
    """The named refusal `unipile_rate_limited`: Unipile has hit the limit of this LinkedIn
    account. `retryable: true` with the delay to wait (the one Unipile asked for,
    `Retry-After` header or body) in `data.retry_after_seconds` (oto#177)."""
    secs = max(1, int(wait_secs) + 1)
    return McpError(ErrorData(code=INVALID_PARAMS, message=(
        f"Refused `unipile_rate_limited`: {detail} Retry in {_fmt_wait(wait_secs)} and "
        "SLOW DOWN the pace of linkedin_* calls rather than chaining them in bursts "
        "(that is what triggers the throttle, then degrades and disconnects the account); if "
        "the wait is long, move on to something else and come back, rather than probing in "
        "a loop."),
        data={"code": "unipile_rate_limited", "retryable": True,
              "retry_after_seconds": secs}))


def _rate_limit_guard(sub: str) -> None:
    """Refuse a scrape during the ongoing 429 cooldown (without hitting Unipile) — the duration
    is THE ONE Unipile asked for. Avoids hammering during the backoff."""
    until = _RATE_LIMIT_UNTIL.get(sub, 0.0)
    now = time.time()
    if until > now:
        raise _rate_limited(until - now, (
            "Unipile is rate-limiting this LinkedIn account (delay requested by Unipile: from "
            "a few seconds after a light burst to ~1h when the recent cadence was "
            "sustained — the displayed delay is authoritative, not an average)."))


def _note_rate_limited(sub: str, err) -> None:
    """Arm the cooldown = the `retry_after` returned by Unipile — a few seconds after
    a light burst, up to ~1h when the recent cadence was sustained (#361) — capped
    at `_RL_MAX_SECS` (a rare/misleading "12 hours" does not block the day); short default
    if the body has no readable delay."""
    secs = min(getattr(err, "retry_after", None) or _RL_DEFAULT_SECS, _RL_MAX_SECS)
    _RATE_LIMIT_UNTIL[sub] = time.time() + secs


def _actor_key() -> str:
    """ACTOR key for LOCAL accounting — rate-limit cooldown, company cache.
    Never an authorization: what it indexes is a cadence, not a right.

    The `sub` when there is one. On a published project MCP endpoint (ADR 0032) there is
    none, and requiring a `sub` here refused reads that the project does
    allow — that is the heart of #276. The project is the legitimate carrier: it operates
    only one LinkedIn account, hence a single cadence to hold."""
    from .. import subdomain_project
    anon = subdomain_project.current_anon_context()
    if anon is not None:
        return f"project:{anon.project_id}"
    return access.current_user_sub_or_raise()


def _scrape(sub: str, fn):
    """Scrape LinkedIn under rate-limit discipline: refusal during an ongoing cooldown,
    and on an upstream 429 we arm the cooldown (= Unipile's delay) + an actionable "slow down" error."""
    _rate_limit_guard(sub)
    from oto.tools.unipile.client import UnipileRateLimited
    try:
        return fn()
    except UnipileRateLimited as e:
        _note_rate_limited(sub, e)
        raise _rate_limited(_RATE_LIMIT_UNTIL[sub] - time.time(), (
            f"Unipile is rate-limiting this LinkedIn account ({e}). Company profiles already seen "
            "are served from the cache — no need to re-read them."))


# STRUCTURED filters of `linkedin_unipile_search` (≠ keywords): these are the ones
# upstream may fail to apply without saying so (#536).
_FACETTES_RECHERCHE = ("company", "location", "industry", "skills",
                       "network_distance", "advanced_keywords")


def _alertes_recherche(items, total, cursor, facettes, page_suivante):
    """What the page does NOT say about itself (#536: three silent amputations on the
    same target, no error, no indicator — an honest agent concludes "empty pool"
    or "population swept")."""
    out = []
    if isinstance(total, int) and len(items) < total:
        if cursor:
            out.append(
                f"PARTIAL page: {len(items)} results served out of {total} — there are "
                "more pages, call again with `cursor` before concluding anything "
                "about the population.")
        else:
            out.append(
                f"⚠️ {total - len(items)} results out of {total} are UNREACHABLE: "
                f"upstream returns only {len(items)} and gives NO cursor "
                "(LinkedIn product cap — measured at 25 out of 86 in "
                "api='sales_navigator'). This is NOT a complete sweep: conclude "
                "nothing about the unseen profiles; narrow the search (finer facet, "
                "split by location/title, other `api=`) to make "
                "the population fit under the cap.")
    if total is None and items and not cursor:
        # The ordinary tier (`classic`) carries NO total (#91, feedback 753):
        # the numeric admission above then cannot fire, and this is
        # precisely the case where the page may be a cap without a cursor. Staying silent
        # here let a single page be read as a swept population.
        out.append(
            f"{len(items)} result(s) and NO total announced by upstream (tier "
            "without a counter, typically api='classic'): the absence of `cursor` does "
            "NOT prove the population is swept — this tier does not paginate. Conclude "
            "nothing about the unseen profiles; narrow the search or go through "
            "a premium product (guide `linkedin-search`).")
    if facettes and not items:
        out.append(
            f"0 results WITH facet(s) {', '.join(facettes)}: a facet may NOT be "
            "applied by upstream, with no error or indicator (measured: same target "
            "and same employer facet → 0 in api='sales_navigator', 10 in "
            "api='classic'). A zero here does not prove an empty pool — cross-check with "
            "another `api=` or with keywords before reporting it.")
    if page_suivante and facettes:
        out.append(
            f"CURSOR-ONLY pagination: the filters passed again with `cursor` "
            f"({', '.join(facettes)}) are NOT re-applied — only the cursor carries the "
            "upstream query, and page 2 sometimes LOSES the employer filter (measured in "
            "api='classic': unrelated profiles). Check the employer of each item "
            "on this page before using it.")
    return out


def _slim_search(res, *, facettes=(), page_suivante=False):
    """Slim down a search response (feedback #335, token cost ÷~2 in bulk):
    - de-duplicates `data`/`items` and `next_cursor`/`cursor` — oto-core `_norm` returns
      BOTH (same list) for downstream stability; the agent only needs `items`/`cursor`;
    - removes the image URLs from each result (`*picture_url*`: photo + large + background) =
      dead weight in search (the agent does not render images; a specific profile → linkedin_unipile_profile).
    Touches NOTHING else (all business fields stay).

    ...and SAYS what the page amputates (#536): `returned`/`truncated` as soon as
    `items < total_count`, plus plain-language `warnings` when the result does not
    read at face value (cap without cursor, zero on a facet, page
    obtained by cursor, page with neither total nor cursor). The envelope grows ONLY
    if there is something to admit — a complete search stays as light
    as before. ⚠️ `total_count` is PASS-THROUGH only: absent upstream (ordinary
    tier), it is absent here, and `returned`/`truncated` with it (#91)."""
    if not isinstance(res, dict):
        return res
    items = res.get("items")
    if items is None:
        items = res.get("data") or []
    for it in items:
        if isinstance(it, dict):
            for k in [k for k in it if "picture_url" in k.lower()]:
                it.pop(k, None)
    cursor = res.get("cursor") or res.get("next_cursor")
    total = res.get("total_count")
    out = {"items": items, "cursor": cursor}
    if total is not None:
        out["total_count"] = total
    if isinstance(total, int) and len(items) < total:
        out["returned"] = len(items)
        out["truncated"] = True
    alertes = _alertes_recherche(items, total, cursor, tuple(facettes), page_suivante)
    if alertes:
        out["warnings"] = alertes
    return out


def _canonical_li_identifier(identifier: str, genre: str = "in") -> str:
    """Canonicalize a LinkedIn `public_identifier` (vanity slug): LinkedIn
    ALWAYS generates it in ASCII (transliterates accents at creation, e.g.
    `renée-lefèvre` → `renee-lefevre`). An accented slug typed by the agent
    makes the Unipile API return a MISLEADING 403 "Insufficient permissions"
    (#180) → we strip the diacritics before the call. No-op on an already ASCII slug
    or an opaque provider_id (`ACoAA…`, no accent) — idempotent.

    A full profile URL (`https://www.linkedin.com/<genre>/<slug>/?…`) gets the SAME
    misleading 403 (#1353): it is brought back to its slug (`genre` = `in` for a
    member, `company` for a company). Any other LinkedIn URL (Sales Navigator, a post,
    the other genre) is refused by name rather than sent. A bare slug is
    percent-decoded too (#1447): `chlo%C3%A9-dupr%C3%A9` → `chloe-dupre`."""
    ident = identifier.strip()
    if "linkedin.com" in ident.lower() or ident.lower().startswith(("http:", "https:")):
        parts = urlsplit(ident if "://" in ident else "https://" + ident)
        segments = [unquote(x) for x in parts.path.split("/") if x]
        if (not parts.hostname or not parts.hostname.lower().endswith("linkedin.com")
                or len(segments) < 2 or segments[0].lower() != genre):
            quoi = "a member" if genre == "in" else "a company"
            raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                f"`identifier` is a URL that is not {quoi}'s LinkedIn page "
                f"(`linkedin.com/{genre}/<slug>`): {identifier!r}. Pass the public "
                "slug (the segment after "
                f"`/{genre}/`) or the URN (`ACoAA…`); nothing was sent.")))
        ident = segments[1]
    else:
        # A bare slug copied from an address bar arrives percent-encoded
        # (`chlo%C3%A9-dupr%C3%A9`): decoded like a URL segment (#1447).
        ident = unquote(ident)
    return "".join(
        c for c in unicodedata.normalize("NFKD", ident)
        if not unicodedata.combining(c)
    )


def _slim(payload, fields: Optional[list[str]] = None,
          text_max_chars: Optional[int] = None,
          *, keep_always: tuple[str, ...] = ("id", "social_id")):
    """Slim down an Unipile list envelope — field projection + text truncation.

    Why (signal #281): `limit=10` on a member's posts returns 55 to 75 KB — image URLs
    in triplicate, urns, share tokens — for a need that is almost always
    "sweep X's latest posts and see whether one fits". The payload fell back to a
    file on every call, so a second tool (jq) was needed to sort. With a
    projection and a text excerpt, triage fits in ONE light call.

    Touches ONLY the items: the envelope (`cursor`, `total_count`) is preserved, otherwise
    pagination would break. `fields` always keeps enough to ADDRESS the item afterwards
    (`keep_always` — `id`/`social_id` for a raw Unipile item, `urn` for a feed
    post): projecting until the result is unusable would be
    worse than returning everything."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return payload
    if not fields and not text_max_chars:
        return payload
    keep = set(fields or ()) | set(keep_always) if fields else None

    def _one(it):
        if not isinstance(it, dict):
            return it
        out = {k: v for k, v in it.items() if k in keep} if keep else dict(it)
        for champ in _TEXTUAL_FIELDS:
            v = out.get(champ)
            if text_max_chars and isinstance(v, str) and len(v) > text_max_chars:
                out[champ] = v[:text_max_chars] + "…"
                out[f"{champ}_truncated"] = True
        return out

    payload = dict(payload)
    payload["items"] = [_one(i) for i in payload["items"]]
    if "data" in payload and isinstance(payload.get("data"), list):
        payload["data"] = payload["items"]   # `_norm` aliases both: keep consistent
    return payload


def _page_de_messagerie(page, fields: Optional[list[str]],
                        text_max_chars: Optional[int]):
    """A messaging page (threads or messages) slimmed on request (#1353).

    A daily sweep of replies needs, per thread, its id, name, date, last sender and an
    excerpt: the full `last_message.text` made a page of 20 threads weigh ~100,000
    characters. Opt-in — nothing changes without `fields`/`text_max_chars`:
    - `fields`: kept keys; a dotted one (`last_message.sender_id`) keeps that sub-key
      of the object. `id` always stays (to chain `op="read"`). A key no item carries
      is refused, naming the keys present — never silently dropped;
    - `text_max_chars`: excerpt of `text` and `last_message.text`, the cut marked
      (`text_truncated`).
    The page then comes as ONE list (`items`) and ONE cursor (`cursor`): oto-core serves
    both twice (`data`, `next_cursor`), which doubled the weight."""
    if not fields and text_max_chars is None:
        return page
    if text_max_chars is not None and text_max_chars <= 0:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=(
            "`text_max_chars` must be > 0 (omit it for the full text).")))
    if fields is not None and not fields:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=(
            "`fields` is an empty list: omit it for every field, or name the keys.")))
    if not isinstance(page, dict):
        return page
    items = page.get("items")
    if not isinstance(items, list):
        items = page.get("data") if isinstance(page.get("data"), list) else []
    out = {k: v for k, v in page.items() if k not in ("data", "next_cursor", "items")}
    if "cursor" not in out and "next_cursor" in page:
        out["cursor"] = page["next_cursor"]

    def _tronque(obj: dict) -> dict:
        v = obj.get("text")
        if text_max_chars and isinstance(v, str) and len(v) > text_max_chars:
            obj = {**obj, "text": v[:text_max_chars] + "…", "text_truncated": True}
        return obj

    def _un(it):
        if not isinstance(it, dict):
            return it
        it = _tronque(it)
        if isinstance(it.get("last_message"), dict):
            it = {**it, "last_message": _tronque(it["last_message"])}
        if not fields:
            return it
        garde: dict = {"id": it["id"]} if "id" in it else {}
        for f in fields:
            tete, _, queue = f.partition(".")
            if tete not in it:
                continue
            if not queue:
                garde[tete] = it[tete]
            elif isinstance(it[tete], dict) and queue in it[tete]:
                sous = garde.get(tete) if isinstance(garde.get(tete), dict) else {}
                garde[tete] = {**sous, queue: it[tete][queue]}
        return garde

    if fields and items:
        def _porte(it, f):
            tete, _, queue = f.partition(".")
            if not isinstance(it, dict) or tete not in it:
                return False
            return not queue or (isinstance(it[tete], dict) and queue in it[tete])
        inconnues = [f for f in fields if not any(_porte(it, f) for it in items)]
        if inconnues:
            presentes = sorted({k for it in items if isinstance(it, dict) for k in it})
            raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                f"`fields`: key(s) absent from every item of the page: {inconnues}. "
                f"Keys present: {presentes} (a sub-key is named `parent.key`).")))
    out["items"] = [_un(i) for i in items]
    return out


def _page_de_relations(page, fields: Optional[list] = None):
    """A page of 1st-degree connections, without the duplicated envelope, projected if requested.

    oto-core `_norm` returns the list TWICE (`data` and `items`, same content) and the
    cursor twice (`next_cursor` and `cursor`). The projection only touched
    `items`: `data` went out INTACT next to a projected `items` — nothing was
    slimmed, and a misnamed key returned an array of empty objects that read
    as data loss (#91, feedback 731). So we serve ONE list (`items`)
    and ONE cursor (`cursor`), projected together.

    `member_id` is always kept: it is the deduplication key that the
    description prescribes, projecting until it is lost would make the export unusable.
    A requested field that NO item on the page carries is refused, naming the
    keys present — never silently dropped."""
    if not isinstance(page, dict):
        return page
    out = {k: v for k, v in page.items() if k not in ("data", "next_cursor")}
    items = page.get("items")
    if not isinstance(items, list):
        items = page.get("data") if isinstance(page.get("data"), list) else []
    if "cursor" not in out and "next_cursor" in page:
        out["cursor"] = page["next_cursor"]
    if fields:
        presentes = {k for it in items if isinstance(it, dict) for k in it}
        inconnues = [f for f in fields if f not in presentes]
        if items and inconnues:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=(f"`fields`: key(s) absent from all connections on the "
                         f"page: {inconnues}. Keys present: {sorted(presentes)}.")))
        garder = set(fields) | {"member_id"}
        items = [{k: v for k, v in it.items() if k in garder}
                 for it in items if isinstance(it, dict)]
    out["items"] = items
    return out


def _shape_feed(payload: dict, fields: Optional[list[str]],
                text_max_chars: Optional[int]) -> dict:
    """Fit the feed page to the size of a tool result (signal #384).

    Three regimes, and the result ALWAYS SAYS which one applies:
    - `fields` omitted → the triage view `_FEED_DEFAULT_FIELDS`;
    - `fields=["*"]` → all the post's fields (path to the raw post);
    - `fields=[…]` → exactly those fields (the `urn` always stays, an unknown field
      is flagged without blocking).

    The `projection` block is set only if something was trimmed: it names what is
    missing and how to get it, so that a default that summarizes never becomes a
    default that hides."""
    items = payload["items"]
    present = {k for it in items if isinstance(it, dict) for k in it}

    if fields is not None and not fields:
        # Neither "everything", nor "nothing", nor the triage view: ambiguous request. We refuse
        # instead of choosing in the caller's place (a swallowed `fields=[]` would return
        # SILENTLY more than the default, the opposite of the intent).
        raise McpError(ErrorData(code=INVALID_PARAMS, message=(
            "`fields` is an empty list: omit it for the triage view, pass the "
            "wanted fields, or `['*']` for all the post's fields.")))
    if text_max_chars is not None and text_max_chars <= 0:
        # Same trap: 0 is falsy in Python, hence "no limit" — the opposite
        # of what whoever writes `text_max_chars=0` is asking for.
        raise McpError(ErrorData(code=INVALID_PARAMS, message=(
            "`text_max_chars` must be > 0 (or `None` for the full text) — "
            "`0` does not mean \"no text\".")))

    if fields is None:
        keep: Optional[list[str]] = list(_FEED_DEFAULT_FIELDS)
    elif "*" in fields:
        keep = None
    else:
        keep = list(fields)

    out = _slim(payload, keep, text_max_chars, keep_always=_FEED_ADDRESSING)

    rendered = {k for it in out["items"] if isinstance(it, dict) for k in it}
    omitted = sorted(present - rendered)
    if omitted or text_max_chars:
        out["projection"] = {
            "omitted_fields": omitted,
            "text_max_chars": text_max_chars,
            "hint": "triage view. All fields: fields=['*'] — full text: "
                    "text_max_chars=None — a whole post: op='get' (post_id=<urn>).",
        }
    if fields and keep is not None:
        unknown = [f for f in fields if f not in present]
        if unknown and items:
            out["warning"] = (
                "`fields` entries unknown in the feed posts: "
                f"{', '.join(unknown)} — check the spelling (absent from the result)")
    return out


def _feed(client, limit: Optional[int], cursor: Optional[str],
          fields: Optional[list[str]], text_max_chars: Optional[int]) -> dict:
    """A page of the feed, read LIVE from Unipile (oto#156) — nothing is written.

    One call = one Voyager request, under the same rate-limit discipline as any
    LinkedIn read (`_scrape`). The page is sorted by publication date in
    memory; the next one is requested with the returned `cursor`, never by a page number
    (LinkedIn paginates by token). An unreadable envelope RAISES: returning it as an
    empty page would read as "nothing new" where upstream changed shape."""
    count = 20 if limit is None else limit
    if count < 1:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=(
            f"op='feed': `limit` must be ≥ 1 (got {count}).")))
    page = _scrape(_actor_key(), lambda: client.get_feed(
        count=count, cursor=cursor, sort_order=_FEED_SORT_ORDER))
    if "_raw" in page:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=(
            "The LinkedIn feed returned an unexpected structure (no `elements`): "
            "nothing is readable on this page. Try again later; if it persists, "
            "report it (`feedback`).")))
    items = sorted(page.get("items") or [],
                   key=lambda it: it.get("posted_at") or "", reverse=True)
    return _shape_feed({"items": items, "cursor": page.get("cursor"),
                        "count": len(items)}, fields, text_max_chars)


# Unipile channels: front key → DB provider. Single source of the channel list
# (consumed by status_for; mirrored on the front side in ConnectorHostedWidget).
# X (TWITTER) and Messenger (MESSENGER) removed on 2026-09-15: the Unipile v2 API does not
# serve them (absent from createAuthLink), their connection could not succeed.
UNIPILE_CHANNELS = {
    "linkedin": "LINKEDIN", "whatsapp": "WHATSAPP", "telegram": "TELEGRAM",
    "instagram": "INSTAGRAM",
}


def _channels_from(accts_by_provider: dict) -> dict:
    """Build the channels dict from the accounts indexed by DB provider."""
    def _ch(provider: str) -> dict:
        a = accts_by_provider.get(provider)
        return {
            "connected": a is not None,
            "account_id": a["account_id"] if a else None,
            "account_name": a.get("account_name") if a else None,
            "connected_at": str(a["connected_at"]) if a else None,
        }
    return {front: _ch(prov) for front, prov in UNIPILE_CHANNELS.items()}


def status_for(sub: str, *, org=access._UNSET, group=access._UNSET) -> dict:
    """Per-user Unipile state: connected channels + unlocked option + key mode.
    SINGLE SOURCE consumed by `/api/me/unipile` (user-facing). BYO (own
    user/group/org key) ⇒ option open (the user manages their own instance). Otherwise the
    hosted-messaging option must have been granted to the org by an admin (comp).
    Explicit `org`/`group` = a THIRD PARTY's state against their own context, without the
    requester's view-as/session context (anti-leak, see access._UNSET).
    Member scope (ADR 0033 B4): `channels` = the channels linked to THIS org (the binding
    is a per-org act — explicit model, end of the silent fallback #221).
    `elsewhere` = the PROPOSAL: channels not linked here for which the sub has a live
    platform seat in another org (same shared key ⟹ adoptable at connect)."""
    o = access.current_org(sub) if org is access._UNSET else org
    mode = access.credential_mode_for(sub, "unipile", org=org, group=group)
    byo = mode in access.BYO_MODES
    subscribed = access.option_open(sub, "unipile", org=org, group=group)  # single source (byo OR option)
    all_accts = db.list_unipile_accounts(sub)
    accts = {a["provider"]: a for a in all_accts if a.get("org_id") == o}
    # Adoption possible? (platform mode = same key everywhere; the option gates the connect)
    elsewhere: dict = {}
    if mode == "platform" and subscribed:
        for a in sorted((x for x in all_accts
                         if x.get("platform_seat") and x.get("org_id") != o),
                        key=lambda x: str(x.get("connected_at") or ""), reverse=True):
            if a["provider"] not in accts:
                elsewhere.setdefault(a["provider"], {
                    "account_id": a["account_id"],
                    "account_name": a.get("account_name"),
                    "org_id": a.get("org_id"),
                })
    front_by_provider = {prov: front for front, prov in UNIPILE_CHANNELS.items()}
    return {
        "subscribed": subscribed,   # option unlocked (BYO or admin comp) — gates "connect"
        "mode": mode,  # user|group|org|platform|over_quota|forbidden (origin of the key)
        "byo": byo,
        "channels": _channels_from(accts),
        # per front channel: the sub's account connected ELSEWHERE, adoptable here in one click
        # (the Connect button adopts on the backend side — the UI can announce it).
        "elsewhere": {front_by_provider[p]: v for p, v in elsewhere.items()
                      if p in front_by_provider},
    }


def account_status(provider: str = "LINKEDIN",
                   account_id_hint: "str | None" = None) -> dict:
    """"Is my {provider} account connected, and is its session alive?"

    Born from signal **#452** (org 2, 14/08/2026). The NAME `linkedin_unipile_account`
    promises the account's state; the tool only served the premium slate (Recruiter /
    Sales Navigator contracts). An agent that came to check "is my LinkedIn
    connected?" invented `op='status'`, got an `invalid_arguments` (call
    248959, args `{op:'status'}`) and concluded "not connected" — while the channel
    was, and a user reported "it doesn't work".

    Two constraints, both drawn from this failure mode:

    - **It ANSWERS, it does not raise.** With no linked account, `unipile_client()` raises
      an McpError: building the status on it would have replaced a false negative with an
      error, i.e. changed nothing. Here, "not connected" is an ANSWER.
    - **It resolves like a real call.** `resolve_operated_account_id` is
      exactly what `unipile_client()` goes through (pin `_account=`, granted account #55,
      the org's own account) — so "status says connected" implies
      "a call will find an account". Any other reading would recreate the card that
      reassures while the calls fail.

    `connected` ≠ `alive`: an account stays LINKED in the database while its session is
    dead (checkpoint, rotated cookie — #236), and that is precisely the state where a green
    card misleads the most. `alive=None` = probe unavailable, not "dead".

    **It LINKS, like `GET /api/me/unipile`.** There is no webhook any more (#581): a
    freshly connected account is only attached by `reconcile_pending`, under the
    sub that requested the link. The REST side does it on every status read; the
    agent side did it NOWHERE — an onboarding started by
    `unipile_connect_start` could therefore only succeed if the person reopened their
    card in a dashboard within the hour (experienced: org 270, 2026-09-03/14). No-op
    without a pending (no network call), never fatal. When nothing was linked, the
    established reason surfaces in `binding` — `no_candidate` used to be silent everywhere.

    `account_id_hint` = the `account_id` that the flow's return page carries in
    its address, relayed by the agent: it is the PROOF that the agent side lacks
    (oto#247). Without it, the reconciliation refuses to choose between several accounts
    connected in the same window on the shared key (`ambiguous_candidates`).
    """
    from .. import unipile_connect
    from ..connectors import identities as connector_identities
    from ..connectors import readiness as connector_readiness

    sub = access.current_user_sub_or_raise()
    org = access.current_org(sub)
    front = {p: f for f, p in UNIPILE_CHANNELS.items()}.get(provider, provider.lower())

    binding = None
    try:
        binding = unipile_connect.reconcile_pending(sub, account_id=account_id_hint)
    except Exception:  # noqa: BLE001 — opportunistic reconciliation, never blocking
        logger.warning("unipile account_status: best-effort reconcile failed",
                       exc_info=True)

    try:
        account_id = connector_identities.resolve_operated_account_id(sub, provider)
        pointer_error = None
    except ValueError as e:
        # Orphaned "operated identity" pointer (revoked grant, account disconnected by
        # its owner): a real call RAISES here. The status REPORTS it instead —
        # it is exactly the kind of state one comes to ask it about.
        account_id, pointer_error = None, str(e)

    label = None
    if account_id:
        label = next((a.get("account_name") for a in db.list_unipile_accounts(sub)
                      if a.get("account_id") == account_id), None)
        if label is None:
            granted = db.granted_accounts_for(sub, provider) or {}
            g = granted.get(account_id)
            label = (g or {}).get("account_name") if isinstance(g, dict) else None

    alive = None
    if account_id:
        try:
            alive = bool(unipile_client(provider).account_alive(account_id))
        except Exception:
            # Probe unavailable ≠ dead session: we return `alive=None` and say so,
            # rather than announcing an outage we did not observe.
            logger.warning("unipile liveness probe unavailable (%s)", provider,
                           exc_info=True)

    out = {"connected": account_id is not None, "account_id": account_id,
           "account_name": label, "channel": provider, "alive": alive}

    if account_id is None and isinstance(binding, dict) and not binding.get("bound"):
        # The reason for THIS channel: a failed WhatsApp request is not reported on the
        # LinkedIn status. Reasons without a channel (key, unreachable provider) apply
        # to all; `no_pending` is not a failure.
        motif = next((m for m in binding.get("pendings") or []
                      if (m.get("provider") or "").upper() == provider), None)
        if motif is None and binding.get("reason") in ("no_credential",
                                                         "provider_unreachable"):
            motif = binding
        if motif is not None:
            out["binding"] = {"reason": motif.get("reason"),
                              "detail": motif.get("detail")}
    if account_id is None:
        # The missing step comes from the SHARED seam (option closed? no key? just
        # a channel to link?) — the same answer as the connector card, not a
        # second version that would diverge.
        # Diagnose the CHANNEL, not the account: since the split, the activation, ACL
        # and selection that can block THIS channel are ITS OWN. The "key"
        # layer, for its part, goes up to the carrier account — `readiness.diagnose` does it
        # itself (`credential_provider`), so the message names the right card.
        canal_con = providers.connector_for_hosted_channel(provider)
        nom = canal_con.name if canal_con else "unipile"
        # #1342: an own account linked in another org is named, with its gesture —
        # never « connect your account » for an account that exists.
        ailleurs = ([] if pointer_error
                    else connector_identities.comptes_dans_une_autre_org(sub, provider))
        if ailleurs:
            out["elsewhere"] = ailleurs[0]
            out["next_step"] = ailleurs[0]["next_step"]
            return out
        diag = connector_readiness.diagnose(
            sub, nom, org=org, group=access.current_group(sub))
        out["next_step"] = pointer_error or (
            diag.next_step if diag is not None
            else connector_readiness.no_identity_step(sub, nom, "account"))
    elif alive is False:
        out["next_step"] = (
            f"The {front} account is linked, but its session is DEAD on the "
            f"provider side (checkpoint, changed password, revoked cookie): every "
            f"call will fail. Reconnect it via `unipile_connect_start`.")
    return out


def _status_pending_action(sub: str, org, group, entry: dict):
    """`status_hints` hook (generic seam, batch 2): the key resolves and the option is
    open, but NO channel is linked → the missing step is "Connect a
    channel". The hosted-account specificity stays HERE, not in the common model."""
    if entry.get("mode") == "forbidden":
        return None   # no key → the "to connect"/"option" verdicts are enough
    st = status_for(sub, org=org, group=group)
    if not st["subscribed"]:
        return None   # option closed → the front already renders "option required"
    if any(ch["connected"] for ch in st["channels"].values()):
        return None
    return "Connect a channel"


status_hints.register("unipile", _status_pending_action)

# The step that VERIFIES an available LinkedIn (oto-backend#1112): the catalogue says
# "an account is linked", never "its session is alive" — it is `op=status` that knows.
# Two agents said "your LinkedIn is not connected" without having called it.
status_hints.register_verify_step(
    "linkedin_unipile",
    "Available, not yet verified alive: `linkedin_unipile_account(op='status')` "
    "returns `connected` and `alive` (session alive at the provider) — to be called "
    "BEFORE saying that LinkedIn is not connected.")


def _channel_pending_action(canal: str, libelle: str):
    """`status_hints` hook for ONE channel card (split of 2026-08-28).

    The account key resolves and the option is open, but THIS channel is not linked →
    the missing step is "connect your X account". Before the split, the hook was
    set on `unipile` and said "Connect a channel" as long as NONE of the six
    was: it went silent as soon as the first was connected, so someone who had
    LinkedIn was never told they still had WhatsApp to plug in. One
    card per channel makes the question askable channel by channel — and the answer useful.

    (Closed over `canal` by a factory rather than by a loop closure: six
    hooks closing over the iteration variable would all say the last one.)"""

    def hook(sub: str, org, group, entry: dict):
        if entry.get("mode") == "forbidden":
            return None   # no key → "to connect"/"option" are already enough
        st = status_for(sub, org=org, group=group)
        if not st["subscribed"]:
            return None   # option closed → the front already renders "option required"
        if st["channels"].get(canal, {}).get("connected"):
            return None
        return f"Connect your {libelle} account"

    return hook


# One hook per channel card. `unipile` has none left: its card sets a KEY, it has
# no button to connect anything — a "Connect a channel" there would be
# an instruction with no action.
for _con in providers.REGISTRY.values():
    if _con.hosted_channel:
        status_hints.register(_con.name,
                              _channel_pending_action(_con.hosted_channel.lower(),
                                                      _con.label))
del _con


def admin_status_by_org(sub: str, orgs: list) -> list:
    """Messaging state **per org** for the admin sheet (a user can be in N orgs;
    the option is PER ORG). `orgs` = `org_store.list_orgs_for_user(sub)`.
    For each org: option/mode computed AGAINST THAT org + the channels attached to it
    (`unipile_accounts.org_id`). Accounts attached to an org outside their list fall
    into an "(outside their orgs)" block."""
    accts = db.list_unipile_accounts(sub)
    out = []
    for o in orgs:
        oid = o["org_id"]
        mode = access.credential_mode_for(sub, "unipile", org=oid)
        byo = mode in access.BYO_MODES
        by = {a["provider"]: a for a in accts if a.get("org_id") == oid}
        out.append({
            "org_id": oid, "org_name": o.get("name"), "is_active": bool(o.get("is_active")),
            "subscribed": access.option_open(sub, "unipile", org=oid),  # single source
            "mode": mode, "byo": byo,
            "channels": _channels_from(by),
            # Through the seam, never through the sources. `org_comp` = the org's declared
            # right (any source). `user_comp` = the person's account mark,
            # shown for the record — it opens nothing. `subscribed` also counts a
            # right row set on the person (`has_option`, ADR 0070 §7).
            "option_source": {
                "user_comp": access.user_has_option(sub, "unipile"),
                "org_comp": access.org_has(oid, "unipile"),
            },
        })
    member = {o["org_id"] for o in orgs}
    orphans = {a["provider"]: a for a in accts if a.get("org_id") not in member}
    if orphans:
        out.append({
            "org_id": None, "org_name": "(outside their orgs)", "is_active": False,
            "subscribed": None, "mode": None, "byo": None,
            "channels": _channels_from(orphans), "option_source": None,
        })
    return out


def _project_operated_account(anon, provider: str) -> str:
    """Account to operate on a PUBLISHED MCP endpoint (ADR 0032) — no `sub`.

    The secret of a published endpoint authenticates the PROJECT, not a person: the
    per-member resolution (`resolve_operated_account_id`) has nothing to hook onto and
    raised "Unauthenticated". As a result, a project shared with a third party lost
    LinkedIn — half the contacts of an enrichment mission — and the only
    alternative was to hand over a NOMINAL `oto_` token, which carries the entire
    organization (356 tools, `email_send`, `data_delete_datastore`): indefensible before the
    compliance of a client under a processing contract.

    The missing information already existed: the project DECLARES its connector identities
    (`project_links.identity_ref`). We use it — that is what makes the project secret a
    truly restricted token: a project's perimeter, under the identities it declares.

    Two guards, both necessary:
    - **membership** — the identity must be a LIVE account of the org that owns the
      project. The shared Unipile key addresses the platform's whole subscription: without this
      cross-check, a project link naming any `acc_…` would make a public endpoint
      act under another tenant's LinkedIn.
    - **channel** — the `provider` filter replaces here the rule "several bindings ⇒
      ambiguous, we give up": a project that declares LinkedIn AND WhatsApp under the same
      `unipile` connector says nothing ambiguous, it declares two channels.

    Never a fallback: neither on another account of the org, nor on the first of the subscription.
    A message sent under the wrong identity is irreversible, and the share's recipient
    has no way to notice."""
    # ⚠️ TWO link names to read since the split of 2026-08-28. A link written
    # BEFORE names the `unipile` connector (there was only one, and the channel
    # filter below was enough to lift the ambiguity); a link written SINCE, from
    # the channel's card, names the channel. Reading only one of the two breaks half
    # the projects — the old or the new ones depending on the name chosen — and breaks it
    # silently, since the absence of a link is rendered as "this project declares no
    # account". The links are deliberately NOT migrated: they carry an
    # identity chosen by a person, and both names remain true.
    canal_con = providers.connector_for_hosted_channel(provider)
    noms_de_lien = ["unipile"] + ([canal_con.name] if canal_con else [])
    declared = [a for nom in noms_de_lien
                for a in access.project_declared_identities(nom, anon.project_id)]
    if not declared:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"This shared project declares no {provider.title()} account. Its "
                     "owner must link the connector WITH an identity "
                     f"(`oto_project op=link target_type=connecteur "
                     f"target_ref={noms_de_lien[-1]} identity_ref=<account_id>`) so "
                     "that the endpoint can act.")))
    # Deduplicated (stable order): a project that declares the SAME identity under the
    # two link names declares ONE account, not two — without this the
    # "several accounts ⟹ I don't guess" safeguard would fire on a
    # perfectly unambiguous project, simply because it was linked after the split.
    joignables = db.org_unipile_account_ids(anon.org_id, provider)
    usable = list(dict.fromkeys(a for a in declared if a in joignables))
    if not usable:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"The {provider.title()} account declared by this project is not (or "
                     "no longer) a connected account of the owning organization — "
                     "disconnected, or linked to another organization. No fallback to "
                     "another account: the owner must restore the link.")))
    if len(usable) > 1:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"This project declares {len(usable)} {provider.title()} accounts "
                     f"({', '.join(sorted(usable))}). A published endpoint has no one to "
                     "ask which one: the owner must keep only one for "
                     "this channel.")))
    # `_account=` stays readable on this surface (the axis does not ask for a `sub`). We do NOT
    # silently ignore it — swallowing a context token makes the call act under a different
    # identity than the one requested, the failure mode that the `_` prefix fixed
    # (#250): we accept it if it restates the project's identity, we refuse otherwise. The
    # recipient of a share does not choose under which account they operate.
    pin = session_org.current_call_account()
    if pin and pin != usable[0]:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=("`_account=` is not admissible on a published project endpoint: "
                     "the identity comes from the project, not from the caller. Remove the token.")))
    return usable[0]


def _refus_identite(e: ValueError):
    """The protocol error of an identity that cannot be operated. A loan held back by
    its lender's pause (#898) carries its code there (`lender_suspended`), so that an
    agent can tell it from a revocation without reading the sentence."""
    code = getattr(e, "code", None)
    return ErrorData(code=INVALID_PARAMS, message=str(e),
                     **({"data": {"code": code, "retryable": False}} if code else {}))


def unipile_client(provider: str = "LINKEDIN"):
    """The user's Unipile client for a channel (LINKEDIN, WHATSAPP, …).

    Shared key (org) + per-user account_id PER CHANNEL: each acts as
    THEMSELVES under the common Unipile subscription. NO fallback: without a connected
    account_id for this channel, the oto-core client would fall back to the subscription's
    1st account → **cross-user impersonation** (security audit 2026-06-18). We require the
    per-user credential, otherwise an actionable McpError. Reused by tools/whatsapp.py.

    ONLY exception (#55): an account GRANTED by its owner
    (`connector_account_grants`, revalidated on EVERY call — immediate revocation),
    resolved by `connector_identities.resolve_operated_account_id`. Limitation: if
    the owner is on a DIFFERENT Unipile key than the grantee (personal BYO ≠ shared
    key), the Unipile API will answer 404 on the account_id — error surfaced as
    is (the resolved key is independent of the account).
    """
    from oto.tools.unipile import make_unipile_client
    from .. import subdomain_project
    from ..connectors import identities as connector_identities
    # Resolution under the CHANNEL's connector (split of 2026-08-28), not under
    # `unipile`: this is what routes the call through THAT CHANNEL's gates (the
    # gates apply to the name the resolution receives). The KEY stays the account's: the delegation
    # (`Connector.credential_of`) brings it back to `unipile` in the cascade.
    # Channel outside the registry ⟹ we fall back to the carrier (previous behavior).
    canal_con = providers.connector_for_hosted_channel(provider)
    rc = access.resolve_credential(canal_con.name if canal_con else "unipile",
                                   want="auto")
    anon = subdomain_project.current_anon_context()
    if anon is not None:
        return make_unipile_client(
            api_key=rc.key, dsn=(None if rc.is_platform else rc.config.get("dsn")),
            account_id=_project_operated_account(anon, provider), provider=provider)
    sub = access.current_user_sub_or_raise()
    try:
        account_id = connector_identities.resolve_operated_account_id(sub, provider)
    except ValueError as e:  # operated pointer revoked/disconnected → explicit error
        raise McpError(_refus_identite(e))
    # Project pin (#57): if the active project pins an unipile account, it takes precedence over the
    # per-channel default — BUT only if it belongs to THIS user IN THIS org
    # (anti-impersonation + member scope ADR 0033) OR is granted to them by its owner
    # (#55, live grant re-checked at this call), AND to the requested channel. Otherwise default (fail-soft).
    org = access.current_org(sub)
    # Same name duality as on the anonymous path (see `_project_operated_account`):
    # the channel first — a link set from ITS card is the most specific —, the
    # account then for the links from before the split.
    canal_con = providers.connector_for_hosted_channel(provider)
    pinned = ((access.project_pinned_identity(canal_con.name) if canal_con else None)
              or access.project_pinned_identity("unipile"))
    if pinned and (
        any(a.get("account_id") == pinned and a.get("provider") == provider
            and a.get("org_id") == org
            for a in db.list_unipile_accounts(sub))
        or pinned in db.granted_accounts_for(sub, provider)
    ):
        account_id = pinned
    elif pinned:
        # The project pins an account LENT by a paused account (#898): neither the
        # fallback to the default (we would act under a different identity than the one the
        # project declares), nor a mute refusal — the named refusal.
        try:
            connector_identities.refuser_si_preteur_en_pause(sub, provider, pinned)
        except ValueError as e:
            raise McpError(_refus_identite(e))
    if not account_id:
        # #1342 (option C of 27/10/2026): the caller's OWN account of this channel is
        # linked in another of their orgs and adoptable here → say so, with the gesture.
        # « Connect your account » sent people to a new pairing (a second session at the
        # provider) for an account that already exists.
        ailleurs = connector_identities.comptes_dans_une_autre_org(sub, provider)
        if ailleurs:
            raise McpError(ErrorData(
                code=INVALID_PARAMS, message=ailleurs[0]["next_step"],
                data={"code": connector_identities.CODE_AUTRE_ORG, "retryable": False}))
        # The page comes from the account's PRODUCT: a client of a third-party tenant sent
        # to us creates a second account there, and nothing links any more (see
        # `unipile_connect.connections_page`). Without a declared page, no address —
        # the agent step, for its part, exists everywhere.
        from .. import unipile_connect
        page = unipile_connect.connections_page(sub, org)
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"Connect your {provider.title()} account"
                    + (f" at {page}" if page else "")
                    + " (or via `unipile_connect_start`) before using these tools."))
    # DSN taken from the resolved credential's config (default api.unipile.com on the
    # oto-core side). Platform key → client default (it reads no env).
    dsn = None if rc.is_platform else rc.config.get("dsn")
    # `provider` = the CHANNEL of the operated account. It is not only documentation: Unipile v2
    # messaging has two endpoint shapes (per inbox for LinkedIn, flat
    # for the other five channels) and upstream answers 501 to the wrong one. Without it,
    # oto-core assumes LinkedIn and pays a 501 round trip before recovering.
    return make_unipile_client(api_key=rc.key, account_id=account_id, dsn=dsn,
                               provider=provider)


def _verify(fields: dict, config: dict | None = None) -> None:
    """Unipile connection probe (#133): `list_accounts()` on the resolved key.

    Tests the auth AND the content in one go — an account-agnostic endpoint so no
    need for an account_id. We distinguish three cases:
    - key absent → actionable message (should not happen: `_fields_for`
      resolves the credential upstream, but we keep the safeguard);
    - dead / refused key → `UnipileError` (401/4xx) left to bubble up as is
      (its message = the probe's error feedback);
    - valid key but NO connected account → distinct from a broken listing, we raise
      a message that points to the dashboard's hosted-auth."""
    from oto.tools.unipile import make_unipile_client

    # ⚠️ The field derived from `secret_kind="api_key"` is named `key` (see
    # providers.secret_fields) — reading `api_key` here made the probe BLIND
    # (systemic "key absent" whatever the vault, experienced 2026-07-08:
    # wrongly diagnosed as a missing platform key).
    api_key = fields.get("key")
    if not api_key:
        raise ValueError("Unipile API key missing.")
    cfg = config or {}
    # dsn paired with the key (default api.unipile.com on the oto-core side).
    client = make_unipile_client(api_key=api_key, dsn=cfg.get("dsn"))
    accounts = client.list_accounts()
    if not accounts:
        raise ValueError(
            "valid Unipile key but no connected account — connect an account "
            "via the dashboard's hosted-auth (unipile_connect_start).")


# --- a connector's TWO tools: `<ns>` reads, `<ns>_act` acts (17/10/2026, 21/10/2026) ----
# An MCP client (claude.ai, ChatGPT) authorizes TOOLS, never operations: "always allow"
# on a tool that reads must never allow a message to a third party. So every messaging
# connector serves a PAIR (`tools/lecture.py`, Gmail's shape): `<ns>` reads (`LECTURE`),
# `<ns>_act` acts — sends, reacts, changes a thread (`ACTE`, the tool the client asks to
# confirm). The old names (`whatsapp_chat`, `whatsapp_send`, `linkedin_unipile_chat`,
# `linkedin_unipile_send`…) refuse, naming the call to make
# (`outils_retires.RENOMMAGES`).
#
# LinkedIn has ONE acting tool, `linkedin_unipile_act` (21/10/2026): messages, posts,
# invitations and the acts on a member all live there, each under its own named op
# (`react_message` is not `react_post`). The readers — `linkedin_unipile` (messaging),
# `_post`, `_network`, `_profile`, `_search`… — only read; an old acting op called on
# them is refused, naming its new call (`outils_retires.OPS_DEPLACEES`).


def register_messaging_tools(mcp: FastMCP, channel: str) -> None:
    """Register the Unipile messaging PAIR of a channel, resolved on the user's
    <channel> account (no-fallback): `{c}(op=list|read)` READS, `{c}_act(op=send)` SENDS
    — two tools, because a client authorizes a tool, not an op (`tools/lecture.py`).
    Unipile messaging (`/chats`) is channel-agnostic → a single code for all channels.
    Called by tools/{whatsapp,telegram,instagram}.py.

    The channel IS the name (that is what makes it findable by the agent); the verbs
    are `op` — same shape as `linkedin_unipile` / `linkedin_unipile_act`, the same
    capability on the same connector (ADR 0047 §Amendment)."""
    cl = channel.lower()
    prov = channel.upper()
    acteur = f"{cl}_act"

    def _bad(msg: str) -> McpError:
        return McpError(ErrorData(code=INVALID_PARAMS, message=msg))

    @mcp.tool(name=cl, annotations=LECTURE, description=(
        f"Reads {channel} messaging via Unipile: 1-to-1 AND group conversations. "
        f"Sending is `{acteur}`.\n\n"
        "`op`:\n"
        "- **\"list\"** (default): the conversations, newest first, in `items` "
        f"(`limit` 1-{unipile_messagerie.LISTE_MAX}). A `cursor` is returned only when "
        "Unipile paginates the channel (WhatsApp does not: a full page carries "
        "`truncated`). Group threads are listed alongside the 1-to-1 ones (`is_group`, "
        "named by the group's `name`) and read the same way. Each 1-to-1 thread is "
        "enriched with the counterpart's name (`attendee_name`); `with_names=False` "
        "turns this enrichment off (one API call fewer).\n"
        "- **\"read\"**: the messages of a thread, 1-to-1 or group (`chat_id` from "
        "op=\"list\"). A message Unipile passed on without text nor attachment carries "
        "`content_unavailable`. Each attachment carries a `source`: pass it to "
        "`oto_import` (target=\"project_file\") to file it, or to any tool taking a "
        "`source` — the server fetches the file, never through the conversation."))
    def _lire(op: Literal["list", "read"] = "list",
              chat_id: Optional[str] = None,
              limit: Optional[int] = None,
              cursor: Optional[str] = None,
              with_names: bool = True) -> dict:
        if op == "list":
            refus = unipile_messagerie.borne_de_liste(limit)
            if refus:
                raise _bad(refus)
            page = limit if limit is not None else 20
            return unipile_messagerie.page_de_fils(
                unipile_client(prov).list_chats(limit=page, cursor=cursor,
                                                with_attendee_names=with_names), page)
        if op == "read":
            if chat_id is None:
                raise _bad("op='read' requires chat_id")
            return unipile_messagerie.references_des_pieces(
                unipile_messagerie.page_de_messages(unipile_client(prov).list_messages(
                    chat_id, limit=limit if limit is not None else 30)), cl)
        raise _bad(f"op must be 'list' or 'read' — sending is `{acteur}`.")

    @mcp.tool(name=acteur, annotations=ACTE, description=(
        f"⚠️ SENDS a {channel} message via Unipile, from the person's own account — it "
        f"leaves at once, there is no draft. Reading is `{cl}`.\n\n"
        "`op`:\n"
        "- **\"send\"** (default): sends `text`. `chat_id` → replies in an existing "
        f"thread, 1-to-1 or group (from `{cl}(op=\"list\")`); otherwise `recipient_id` → "
        "opens a new thread with that contact."))
    def _agir(text: str,
              op: Literal["send"] = "send",
              chat_id: Optional[str] = None,
              recipient_id: Optional[str] = None) -> dict:
        # Refused BEFORE any credential resolution: a send without a text or a
        # destination never reaches the client.
        if not text:
            raise _bad(f"{acteur} op='send' requires text")
        if chat_id is None and recipient_id is None:
            raise _bad(f"{acteur} op='send' requires chat_id (reply) or recipient_id "
                       "(new thread)")
        return unipile_client(prov).send_message(text, chat_id=chat_id,
                                                 attendee_id=recipient_id)


def register(mcp: FastMCP) -> None:

    connector_verify.register("unipile", _verify)

    @mcp.tool()
    async def unipile_connect_start(channel: str = "linkedin",
                                    force: bool = False,
                                    premium: Optional[str] = None) -> dict:
        """Start the connection of a hosted messaging account (LinkedIn by
        default) and return an Unipile auth **`url`** to pass on to the user.

        The user opens the URL, logs in to their account (login/2FA/captcha —
        everything happens in this hosted page), then returns to the connections
        page of THEIR product. ⚠️ There is NO webhook (#581): the account is
        LINKED by reconciliation, under your identity, within the hour following this link.
        For LinkedIn, it is `linkedin_unipile_account(op="status")` that triggers it
        — call it when the person says they are done (`binding` says why nothing
        was linked). For the other channels, the linking happens when the person
        reopens their connections page. This is THE messaging onboarding entry point
        from the agent (feedback #131).

        A messaging account is PER-PERSON: if it is already connected in
        another of your orgs, it follows you here (no need to reconnect) and this call
        refuses by default to avoid a duplicate. Pass `force=True` only to
        connect a REALLY different account.

        ⚠️ **LinkedIn premium**: by default only the `classic` product is connected.
        If the person has a **Recruiter** or **Sales Navigator** seat and wants to use it
        (`linkedin_unipile_search(api="recruiter"/"sales_navigator")`,
        `linkedin_unipile_account(op="contracts")`…),
        you MUST request it HERE via `premium` — otherwise these APIs answer 403 "out of
        your scope". The two are **exclusive**. To ADD a product to an ALREADY
        connected account (classic only today), rerun with `premium=` — and
        `force=True` if the anti-duplicate guard blocks: the existing seat is
        **reconnected** (product attached, NO duplicate). If Recruiter still answers
        403 after that, it is on the platform Unipile subscription side (Recruiter API to
        activate), not the connection.

        Args:
            channel: channel to connect — linkedin (default), whatsapp, telegram,
                instagram.
            force: connect despite an account already linked to this channel elsewhere (#172).
            premium: LinkedIn premium product to activate — "recruiter" or
                "sales_navigator" (exclusive, only one per account). Only request it
                if the person really has the matching LinkedIn seat. Also adds
                the cookie connection to the wizard (recommended for these products).
        """
        from .. import unipile_connect

        sub = access.current_user_sub_or_raise()
        try:
            out = await unipile_connect.hosted_auth_url(sub, channel, force=force,
                                                        premium=premium)
        except unipile_connect.ConnectRefused as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=e.message))
        if out.get("adopted"):
            # Per-org binding: the account already connected elsewhere (same shared key)
            # has just been linked to the current org — no link to open.
            out["instructions"] = (
                f"{out.get('channel', channel)} account already connected elsewhere: it has just "
                "been activated for this org — nothing else to do, the tools are "
                "usable immediately.")
            return out
        ch = out.get("channel", channel)
        out["instructions"] = (
            f"Pass `url` to the user: they open the link (valid for 1 h) and connect "
            f"their {ch} account all the way through. No webhook links the account: "
            + ("when they are done, call linkedin_unipile_account(op='status') — "
               "that is what LINKS it; `binding` says why if nothing was. "
               "Ask them for the address of the page they landed on: if it carries "
               "`account_id=…`, pass it (`account_id=`) — without this proof, the "
               "linking is refused when several connections happened at the same "
               "time on the shared key."
               if str(ch).lower() == "linkedin" else
               "the linking happens when they reopen their connections page, within the hour."))
        return out

    # ---- dispatch helpers (`op=` pattern, ADR 0047) ----------------------

    def _bad(msg: str) -> McpError:
        return McpError(ErrorData(code=INVALID_PARAMS, message=msg))

    def _need(value, name: str, op: str):
        """Mandatory argument for THIS op — actionable error, never a fallback."""
        if value is None:
            raise _bad(f"op='{op}' requires {name}")
        return value

    # ---- search ----------------------------------------------------------

    @mcp.tool()
    def linkedin_unipile_search(
        keywords: Optional[str] = None,
        category: str = "people",
        company: Optional[list[str]] = None,
        location: Optional[list[str]] = None,
        industry: Optional[dict] = None,
        network_distance: Optional[list[int]] = None,
        advanced_keywords: Optional[dict] = None,
        skills: Optional[list] = None,
        url: Optional[str] = None,
        api: str = "classic",
        cursor: Optional[str] = None,
    ) -> dict:
        """LinkedIn search via Unipile.

        ⚠️ **Recruiter / Sales Navigator faceted search** (skills,
        industry, location, employer): first read the guide
        `oto_guide(op=read, slug="linkedin-search")`. Four traps that SKEW results
        silently: (1) a facet requires a **resolved ID** — pass the term through
        `linkedin_unipile_facets` and give the chosen `id`; a raw term does NOT filter;
        (2) the `url=` mode is **capped at 25 without pagination** — prefer the structured one
        to control the filters, not for volume: it is the PRODUCT (`api=`) that
        decides on pagination (`classic` does not paginate, whatever the mode);
        (3) a facet may be **NOT applied** by the chosen product, without an error
        (measured: same employer → 0 in `sales_navigator`, 10 in `classic`) — a **0 on a
        faceted search does not prove an empty pool**, cross-check;
        (4) pagination is **CURSOR-ONLY**: the filters passed alongside the `cursor`
        are not re-applied, and page 2 **sometimes loses the employer filter** —
        check the employer of the items of a paginated page.

        **The return**: always `items` + `cursor`. `total_count` **only when
        upstream announces one** — the ordinary tier (`api="classic"`) carries
        none: no population count by this route. When it is there,
        `returned` + `truncated: true` say that the page returns less; `truncated`
        **without** `cursor` = the rest is UNREACHABLE (product cap, e.g. 25 out of 86).
        Without a total, neither `returned` nor `truncated`: the absence of `cursor` then does
        NOT prove a complete sweep, and a `warnings` says so. Read `warnings` before
        reporting "empty pool" or "population swept".

        ⚠️ **Cadence**: LinkedIn rate-limits per account. Chaining dozens
        of calls in a burst triggers a `429`, then DEGRADES and ends up DISCONNECTING
        the account. The requested backoff FOLLOWS your recent cadence: a few seconds
        after a light burst, up to ~1h after a sustained chain — read
        the returned delay, do not assume it is short. Space out your calls; on a
        `429`, respect THAT delay and SLOW DOWN — do not insist.
        For volume, delegate the pagination to a sub-agent (guide `bulk-load`).

        `company`/`location`/`industry` accept NAMES (automatically resolved
        into LinkedIn facets) or numeric facet ids. ⚠️ The LinkedIn company
        page is NOT a valid employer facet id for people search —
        pass the name and let the client resolve.

        ⚠️ **COMPANY fields missing from the result** (size, description, industry):
        enrich them per DISTINCT company, never per profile. Deduplicate the
        employers of your results (far fewer than the people), then
        `linkedin_unipile_profile(op="company", identifier=<name or slug>)` for each —
        the profile is CACHED 6h, a re-looked-up company does not consume quota again. For
        French companies, `fr_search` gives the headcount (= the size) FOR FREE,
        with no LinkedIn quota. Do NOT query each person one by one for these company
        fields (that is the "844 calls" trap: counting per profile what is
        done per employer).

        Args:
            keywords: Keywords (name, job title…).
            category: "people" or "companies".
            company: Employer(s) — names or facet ids.
            location: Location(s) — names or facet ids.
            industry: industry filter — dict `{include?: [...], exclude?: [...]}` (names or ids).
                ⚠️ `exclude` is NOT supported by `api="classic"` (raises an error):
                LinkedIn classic only accepts a list of industries to INCLUDE. To
                exclude an industry, use `api="sales_navigator"` or `"recruiter"`.
            network_distance: degree of connection — `[1]`=1st degree (your 1st-degree connections),
                `[2]`=2nd, `[3]`=3rd+. Combinable (`[1, 2]`) → targets "my 1st-degree in [city]".
            advanced_keywords: people targeting — dict `{first_name?, last_name?, title?,
                company?, school?}`.
            skills: skills filter (Recruiter / Sales Nav) — list of names OR
                facet ids (resolve first via `linkedin_unipile_facets(
                facet_type="SKILL", …)` and pass the chosen `id`). Also accepts a dict
                `{include?, exclude?}` (exclusion = `priority DOESNT_HAVE`).
            url: LinkedIn search URL pasted from the browser (classic / Sales
                Navigator). If provided, the other structured filters are ignored;
                pass the URL product's `api=`. ⚠️ **Recruiter-from-URL is
                currently unreliable on the Unipile side** (the endpoint hangs → timeout,
                even with a fresh searchContextId): for Recruiter, prefer the
                STRUCTURED search below (`api="recruiter"` + keywords/facets),
                not the URL.
            api: "classic" | "sales_navigator" | "recruiter" (advanced filters depending on
                the connected account's LinkedIn subscription). Recruiter/Sales Nav require
                the premium seat activated at connect (otherwise 403 "out of scope").
            cursor: Pagination cursor returned by a previous call.
        """
        sub = _actor_key()
        poses = dict(company=company, location=location, industry=industry,
                     skills=skills, network_distance=network_distance,
                     advanced_keywords=advanced_keywords)
        facettes = tuple(n for n in _FACETTES_RECHERCHE if poses.get(n))
        return _slim_search(_scrape(sub, lambda: unipile_client().search(
            keywords=keywords, category=category, company=company, location=location,
            industry=industry, network_distance=network_distance,
            advanced_keywords=advanced_keywords, skills=skills, url=url, api=api,
            cursor=cursor,
        )), facettes=facettes, page_suivante=bool(cursor))

    @mcp.tool()
    def linkedin_unipile_facets(facet_type: str, keywords: str, limit: int = 25) -> dict:
        """Resolve a LinkedIn filter NAME into `{id, name}` candidates to pass to
        `linkedin_unipile_search`. To be used BEFORE a structured search as soon as a
        criterion is not a simple keyword (skill, industry, location,
        employer…).

        Choosing the right candidate is YOUR job: a single input often returns
        several facets ("Microsoft Excel" → Excel, Microsoft Office, …) —
        read the `name`s and keep the relevant `id`. Then pass it to
        `linkedin_unipile_search` (`location`/`company`/`industry` already accept
        ids; the other facets are coming — see guide `linkedin-search`).

        Returns `{facet_type, candidates: [{id, name}]}`. Resolution INDEPENDENT of the
        product/contract (works even outside Recruiter/Sales Nav).

        Args:
            facet_type: facet type, UPPERCASE. Confirmed: `SKILL`, `LOCATION`,
                `INDUSTRY`, `COMPANY`. Others exist (try `TITLE`, `SCHOOL`,
                `FUNCTION`, `SENIORITY`, `LANGUAGE`…) — an invalid type raises
                an `Expected kind 'StringEnum'` error.
            keywords: the label to resolve (e.g. "Microsoft Excel", "Paris").
            limit: max number of candidates (default 25).
        """
        cands = unipile_client().resolve_facet(str(facet_type).upper(), keywords, limit=limit)
        return {"facet_type": str(facet_type).upper(), "candidates": cands}

    # ---- members & companies: read a profile, their activity, act on it ---

    @mcp.tool(annotations=LECTURE)
    def linkedin_unipile_profile(
        op: Literal["person", "company", "me", "posts", "comments", "reactions",
                    "followers", "following"] = "person",
        identifier: Optional[str] = None,
        sections: str = "*",
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
        fields: Optional[list[str]] = None,
        text_max_chars: Optional[int] = _TEXT_EXCERPT_CHARS,
    ) -> dict:
        """A LinkedIn member or company: read their profile and their activity. Acting
        on a member (endorsing a skill, a Sales Navigator / Recruiter action) is
        `linkedin_unipile_act` (op="endorse" / "update_member").

        `identifier` = **public slug** (`marie-dupont`; a full `linkedin.com/in/…` or
        `/company/…` URL is brought back to it) or **URN** (`ACoAA…`). NOT the
        numeric `member_id` from `linkedin_unipile_search`: the v2 API rejects it
        (`400 Invalid User ID`) — go through the slug, which these ops resolve for you.

        `op`:
        - **"person"** (default): full profile (dated career, schools, network).
          ⚠️ LinkedIn may throttle a section (often `experience`): the response
          then carries `throttled_sections=[…]` with the section empty despite a
          `*_total_count` > 0. This is an UPSTREAM rate-limit, not an absence of data:
          retry later (minutes), reduce concurrency (≤8 in parallel), and
          in a batch handle these targets in a deferred catch-up pass.
          Also CHECK that the returned `public_identifier`/id == the requested one before
          writing (reject + retry otherwise).
        - **"company"**: company profile. Cached 6h per account (~static
          profiles) — the same company looked up again does not consume the upstream quota
          (~100 profiles/12h per account).
        - **"me"**: profile of the connected account itself (the "me" under which the
          other ops act). No `identifier`.
        - **"posts"** / **"comments"**: what a member publishes / comments — to
          spot a post to comment on/like, or what a prospect engages with.
          Each item's text is served as an EXCERPT (600 characters, cut marked
          `text_truncated: true`): the raw is 55-75 KB for 10 posts, and triage is done on
          the header. `text_max_chars=None` returns the full text, `op="get"` of
          `linkedin_unipile_post` a specific post. To slim further, `fields`
          (e.g. `["text","posted_at","social_id"]`) keeps only those fields — `id`/
          `social_id` are always kept so you can chain.
        - **"reactions"**: posts a member has liked.
        - **"followers"** / **"following"**: followers of the connected account, or of a
          member via `identifier`. Paginated.

        Args:
            op: person (default) | company | me | posts | comments | reactions |
                followers | following.
            identifier: public slug or URN of the member / company. Required
                except op="me"; optional for followers/following (default = you).
            sections: op="person" — sections to include ("*" = all), comma
                separated: experience, education, languages, skills,
                certifications, volunteer_experience, projects, recommendations,
                interests, recruiting_activity (Recruiter only). `<name>_preview`
                = its first entries only (lighter, less throttling), `*_preview`
                = every section that way. Any other name is refused, naming
                these. The About text is NOT a section: it always comes back,
                as `bio`.
            cursor: pagination (posts, comments, reactions, followers, following).
            limit: page size.
            fields: op="posts"/"comments" — field projection (slims heavily).
            text_max_chars: op="posts"/"comments" — text length of each item
                (default 600; `None` = full text).
        """
        sub = _actor_key()

        if op == "me":
            return unipile_client().get_own_profile()

        if op == "person":
            ident = _canonical_li_identifier(_need(identifier, "identifier", op))
            return _scrape(sub, lambda: unipile_client().get_profile(ident, sections=sections))

        if op == "company":
            ident = _canonical_li_identifier(_need(identifier, "identifier", op),
                                             "company")
            key = (sub, ident.lower())
            hit = _COMPANY_CACHE.get(key)
            if hit and time.time() - hit[0] < _COMPANY_TTL:
                return hit[1]
            res = _scrape(sub, lambda: unipile_client().get_company(ident))
            if len(_COMPANY_CACHE) >= _COMPANY_CACHE_MAX:
                _COMPANY_CACHE.clear()
            _COMPANY_CACHE[key] = (time.time(), res)
            return res

        if op == "posts":
            return _slim(unipile_client().list_member_posts(
                _need(identifier, "identifier", op), cursor=cursor, limit=limit),
                fields, text_max_chars)

        if op == "comments":
            return _slim(unipile_client().list_member_comments(
                _need(identifier, "identifier", op), cursor=cursor, limit=limit),
                fields, text_max_chars)

        if op == "reactions":
            return unipile_client().list_member_reactions(
                _need(identifier, "identifier", op), cursor=cursor, limit=limit)

        if op == "followers":
            return unipile_client().list_followers(user_id=identifier, cursor=cursor,
                                                   limit=limit)

        if op == "following":
            return unipile_client().list_following(user_id=identifier, cursor=cursor,
                                                    limit=limit)

        raise _bad("op must be 'person', 'company', 'me', 'posts', 'comments', "
                   "'reactions', 'followers' or 'following' — acting on a member is "
                   "`linkedin_unipile_act`.")

    # ---- messaging -------------------------------------------------------

    @mcp.tool(name="linkedin_unipile", annotations=LECTURE)
    def linkedin_unipile(
        op: Literal["list", "read", "attendees", "contacts"] = "list",
        chat_id: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
        with_names: bool = True,
        fields: Optional[list[str]] = None,
        text_max_chars: Optional[int] = None,
    ) -> dict:
        """Reads LinkedIn messaging (DM) via Unipile. Sending, reacting and changing a
        thread — like every act on LinkedIn — is `linkedin_unipile_act`.

        `op`:
        - **"list"** (default): the conversations, paginated (`limit` + `cursor`) —
          25 at most per page (LinkedIn's limit): beyond that, paginate by `cursor`.
          Each 1-to-1 thread is enriched with `attendee_name`/`attendee_headline`/
          `attendee_profile_url` (resolved in batch — the raw `name` of 1-to-1 threads
          is null and `attendee_provider_id` is opaque). `with_names=False` turns off
          this enrichment (raw payload, one API call fewer).
          ⚠️ **The enrichment may not happen, and the response SAYS so**: an
          `attendee_names` field then appears, with its `status` and its reason. An
          absent `attendee_name` on a thread therefore does NOT mean "no
          counterpart" — read `attendee_names` before concluding, and fall back
          on `name` or `last_message.sender`.
          ⚠️ **Do NOT use `last_message.is_sender` to know whether YOU wrote
          last.** Observed as `false` on all of an account's threads on
          03/09/2026 — including the 22 whose last message came from the account
          itself, exactly like the threads where the counterpart had replied. The
          field distinguishes nothing: relying on it leads to concluding a reply on every
          thread, or the opposite. Compare `last_message.sender_id` (or
          `last_message.sender.display_name`) with the account's identity.
        - **"read"**: the messages of a thread (`chat_id`). Each attachment carries a
          `source`: pass it to `oto_import` (target="project_file") to file it, or to
          any tool taking a `source` — the server fetches the file.
        - **"attendees"**: participants of a thread (`chat_id`).
        - **"contacts"**: your messaging contact book (counterparts). Paginated.

        A page of threads with their full last message is heavy (~100,000 characters
        for 20): for a sweep, slim it with `fields` (e.g. `["id", "name",
        "timestamp", "last_message.sender_id", "last_message.text"]`) and
        `text_max_chars` (e.g. 200) — op="list" and op="read".

        Args:
            op: list (default) | read | attendees | contacts.
            chat_id: thread id (read, attendees).
            limit: page size (list, read, contacts).
            cursor: pagination (list, contacts).
            with_names: op="list" — enrichment of counterpart names.
            fields: op="list"/"read" — keys kept per item (`parent.key` for a sub-key;
                `id` always kept); a key absent from the whole page is refused.
                Omitted = every field.
            text_max_chars: op="list"/"read" — excerpt length of `text` and
                `last_message.text`, the cut marked `text_truncated`. Omitted = full.
        """
        if (fields is not None or text_max_chars is not None) and op not in ("list", "read"):
            raise _bad(f"`fields`/`text_max_chars` apply to op='list'/'read', not op='{op}'.")
        client = unipile_client()

        if op == "list":
            # #873 — beyond 25, LinkedIn makes Unipile answer "400 Invalid
            # querystring", without naming the limit (measured on 11/09/2026: 25 passes,
            # 26 is refused). Refused here by naming it, before any call.
            if limit is not None and not 1 <= limit <= _CHAT_LIST_MAX:
                raise _bad(f"op='list': `limit` ranges from 1 to {_CHAT_LIST_MAX} "
                           "(LinkedIn's limit) — paginate with `cursor`.")
            return _page_de_messagerie(
                client.list_chats(limit=limit if limit is not None else 20,
                                  cursor=cursor, with_attendee_names=with_names),
                fields, text_max_chars)

        if op == "read":
            return _page_de_messagerie(unipile_messagerie.references_des_pieces(
                client.list_messages(_need(chat_id, "chat_id", op),
                                     limit=limit if limit is not None else 30),
                "linkedin"), fields, text_max_chars)

        if op == "attendees":
            return client.list_chat_attendees(_need(chat_id, "chat_id", op))

        if op == "contacts":
            return client.list_attendees(cursor=cursor, limit=limit)

        raise _bad("op must be 'list', 'read', 'attendees' or 'contacts' — sending, "
                   "reacting and changing a thread is `linkedin_unipile_act`.")

    @mcp.tool(name="linkedin_unipile_act", annotations=ACTE)
    def linkedin_unipile_act(
        op: Literal["send", "update", "react_message", "publish_post", "comment_post",
                    "react_post", "invite", "accept_invitation", "decline_invitation",
                    "cancel_invitation", "endorse", "update_member"] = "send",
        text: Optional[str] = None,
        chat_id: Optional[str] = None,
        recipient_id: Optional[str] = None,
        message_id: Optional[str] = None,
        reaction: Optional[str] = None,
        action: Optional[str] = None,
        value: Optional[bool | str] = None,
        post_id: Optional[str] = None,
        provider_id: Optional[str] = None,
        message: Optional[str] = None,
        invitation_id: Optional[str] = None,
        shared_secret: Optional[str] = None,
        identifier: Optional[str] = None,
        skill_endorsement_id: Optional[int] = None,
        api: Optional[str] = None,
        hiring_project_id: Optional[str] = None,
        stage: Optional[str] = None,
        list_id: Optional[str] = None,
    ) -> dict:
        """⚠️ ACTS on LinkedIn from the person's own account — the other members see it,
        at once, with no draft: a message, a post, a comment, a reaction, an invitation,
        an endorsement. Reading is `linkedin_unipile` (messaging), `linkedin_unipile_post`,
        `linkedin_unipile_network`, `linkedin_unipile_profile`.

        `op` — messaging (DM):
        - **"send"** (default): sends `text`. `chat_id` → replies in an existing thread;
          otherwise `recipient_id` (the recipient's provider id) → opens a new thread.
        - **"react_message"**: reacts to a MESSAGE with a native emoji (`reaction`, e.g.
          '👍') — the counterpart sees it. `message_id` = id of a message from
          `linkedin_unipile(op="read")`; `chat_id` is **required on the v2 API**,
          ignored in v1.
        - **"update"**: changes a thread's state — `action` ∈ setReadStatus |
          setMuteStatus | setArchiveStatus | setPinnedStatus | setLabel | getInviteLink;
          `value` = boolean for the statuses, string for setLabel, omitted for
          getInviteLink. ⚠️ setReadStatus can show the counterpart that you read them.

        `op` — posts, published publicly in the person's name:
        - **"publish_post"**: publishes a post (`text`).
        - **"comment_post"**: comments on a post (`post_id`, `text`).
        - **"react_post"**: reacts to a POST (`post_id`) — `value`: LIKE (default) |
          PRAISE | EMPATHY | INTEREST | APPRECIATION | ENTERTAINMENT.
          `post_id` = social_id (`urn:li:…`) from `linkedin_unipile_post` or
          `linkedin_unipile_profile(op="posts")`.

        `op` — invitations:
        - **"invite"**: sends a connection request. `provider_id` = the `provider_id` of
          a `linkedin_unipile_search` / `linkedin_unipile_profile` result; `message` =
          note ≤300 characters.
        - **"accept_invitation"** / **"decline_invitation"**: answers a RECEIVED
          invitation — `invitation_id` AND `shared_secret` from the SAME item of
          `linkedin_unipile_network(op="invitations", direction="received")`.
        - **"cancel_invitation"**: withdraws a SENT invitation (`invitation_id` of a
          `direction="sent"` item).

        `op` — a member (`identifier` = public slug or URN):
        - **"endorse"**: endorses a skill (`skill_endorsement_id` = `endorsement_id` of
          a skill from `linkedin_unipile_profile(op="person")`).
        - **"update_member"**: premium action — `api` = 'sales_navigator' (`action`
          'saveLead', optional `list_id`) or 'recruiter' (`action`
          'addCandidateToPipeline' | 'addApplicantToPipeline' |
          'changeCandidatePipeline' | 'rejectApplicant', `hiring_project_id`, `stage`
          'UNCONTACTED' | 'CONTACTED' | 'REPLIED').

        Args:
            op: see above (default "send").
            text: send / publish_post / comment_post — the content.
            chat_id: thread id (send-in-a-thread, update, react_message v2).
            recipient_id: op="send" — recipient's provider id (new thread).
            message_id: op="react_message" — id of the targeted message.
            reaction: op="react_message" — the emoji.
            action: op="update" — the thread action; op="update_member" — the premium
                action.
            value: op="update" — value of the thread action; op="react_post" — the
                reaction type (default LIKE).
            post_id: comment_post / react_post — the post's social_id.
            provider_id: op="invite" — recipient's LinkedIn provider id.
            message: op="invite" — accompanying note (≤300 characters).
            invitation_id: accept_invitation / decline_invitation / cancel_invitation.
            shared_secret: accept_invitation / decline_invitation — LinkedIn token of
                the same item (required).
            identifier: endorse / update_member — the member.
            skill_endorsement_id: op="endorse" — id of the skill to endorse.
            api: op="update_member" — 'sales_navigator' or 'recruiter'.
            hiring_project_id: op="update_member" — recruiter pipeline actions.
            stage: op="update_member" — recruiter pipeline stage.
            list_id: op="update_member" — target Sales Navigator list (saveLead).
        """
        # Every refusal below comes BEFORE the client is built: an incomplete act never
        # reaches LinkedIn.
        if op == "send":
            if chat_id is None and recipient_id is None:
                raise _bad("op='send' requires chat_id (reply) or recipient_id "
                           "(new thread)")
            txt = _need(text, "text", op)
            return unipile_client().send_message(txt, chat_id=chat_id,
                                                 attendee_id=recipient_id)

        if op == "update":
            cid, act = _need(chat_id, "chat_id", op), _need(action, "action", op)
            return unipile_client().patch_chat(cid, act, value=value)

        if op == "react_message":
            mid = _need(message_id, "message_id", op)
            rea = _need(reaction, "reaction", op)
            client = unipile_client()
            # Pass `chat_id` only if provided: keeps compat if oto-core is
            # still at a version whose `react_message` lacks this kwarg (v2-only).
            if chat_id is not None:
                return client.react_message(mid, rea, chat_id=chat_id)
            return client.react_message(mid, rea)

        if op == "publish_post":
            txt = _need(text, "text", op)
            return unipile_client().create_post(txt)

        if op == "comment_post":
            pid, txt = _need(post_id, "post_id", op), _need(text, "text", op)
            return unipile_client().comment_post(pid, txt)

        if op == "react_post":
            pid = _need(post_id, "post_id", op)
            if value is not None and not isinstance(value, str):
                raise _bad("op='react_post': `value` is a reaction type (LIKE, PRAISE…)")
            return unipile_client().react_post(pid, value=value or "LIKE")

        if op == "invite":
            pid = _need(provider_id, "provider_id", op)
            return unipile_client().send_invitation(pid, message=message)

        if op in ("accept_invitation", "decline_invitation"):
            iid = _need(invitation_id, "invitation_id", op)
            secret = _need(shared_secret, "shared_secret", op)
            return unipile_client().handle_invitation(
                iid, secret, "accept" if op == "accept_invitation" else "decline")

        if op == "cancel_invitation":
            iid = _need(invitation_id, "invitation_id", op)
            return unipile_client().cancel_invitation(iid)

        if op == "endorse":
            ident = _need(identifier, "identifier", op)
            skill = _need(skill_endorsement_id, "skill_endorsement_id", op)
            return unipile_client().endorse_profile(ident, skill)

        if op == "update_member":
            ident, quel = _need(identifier, "identifier", op), _need(api, "api", op)
            geste = _need(action, "action", op)
            return unipile_client().member_action(
                ident, quel, geste,
                hiring_project_id=hiring_project_id, stage=stage, list_id=list_id)

        raise _bad("op must be 'send', 'update', 'react_message', 'publish_post', "
                   "'comment_post', 'react_post', 'invite', 'accept_invitation', "
                   "'decline_invitation', 'cancel_invitation', 'endorse' or "
                   "'update_member'")

    # ---- posts -----------------------------------------------------------

    def _engagement(client, kind: str, post_id: str, comment_id: Optional[str],
                    offset: int, want: int) -> dict:
        """Follow the engagement pages (by `offset`, Unipile's only pagination here)
        up to `want` people, an empty page, or the time budget (oto#177). Each
        page goes through `_scrape`: a 429 arms the cooldown. On a 429 AFTER the first
        page, we return what has already been read rather than lose it, saying where to resume."""
        fetch = client.list_reactions if kind == "reactions" else client.list_comments
        sub = _actor_key()
        items: list = []
        seen: set = set()
        off = max(0, offset)
        next_offset: Optional[int] = None
        stopped = "end"
        retry_after = None
        deadline = time.monotonic() + _ENGAGEMENT_BUDGET_S
        while True:
            if time.monotonic() > deadline:
                stopped, next_offset = "time_budget", off
                break
            try:
                page = _scrape(sub, lambda o=off: fetch(post_id, offset=o or None,
                                                        comment_id=comment_id))
            except McpError as e:
                data = getattr(getattr(e, "error", None), "data", None) or {}
                if items and data.get("code") == "unipile_rate_limited":
                    stopped, next_offset = "rate_limited", off
                    retry_after = data.get("retry_after_seconds")
                    break
                raise
            data = page.get("items") if isinstance(page, dict) else None
            if not isinstance(data, list) or not data:
                break  # empty page: end of list (Unipile contract)
            added = 0
            for i, it in enumerate(data):
                key = (it.get("id") or it.get("author_id") or repr(it)) \
                    if isinstance(it, dict) else repr(it)
                if key in seen:
                    continue
                seen.add(key)
                items.append(it)
                added += 1
                if len(items) >= want:
                    next_offset = off + i + 1
                    break
            if len(items) >= want:
                stopped = "limit"
                break
            if not added:
                stopped = "upstream_repeats"  # upstream was re-serving an already-read page
                break
            off += len(data)
        out = {"kind": kind, "post_id": post_id, "comment_id": comment_id,
               "items": items, "count": len(items), "offset": max(0, offset),
               "truncated": stopped in ("limit", "time_budget", "rate_limited"),
               "next_offset": next_offset if stopped != "end" else None,
               "stopped": stopped}
        if stopped == "limit":
            out["note"] = (f"Stopped at `limit`={want}: there may be more — "
                           f"call again with `offset`={next_offset} for the rest.")
        elif stopped == "time_budget":
            out["note"] = (f"Stopped at the time budget ({_ENGAGEMENT_BUDGET_S}s): PARTIAL "
                           f"list — call again with `offset`={next_offset}.")
        elif stopped == "rate_limited":
            out["retry_after_seconds"] = retry_after
            out["note"] = ("Stopped on the Unipile limit (`unipile_rate_limited`): PARTIAL "
                           f"list — wait ~{retry_after}s then call again with "
                           f"`offset`={next_offset}.")
        elif stopped == "upstream_repeats":
            out["note"] = ("Pagination stopped: upstream re-served people already "
                           "read, it no longer advances. Nothing else to ask for.")
        return out

    @mcp.tool(annotations=LECTURE)
    def linkedin_unipile_post(
        op: Literal["feed", "get", "engagement"] = "feed",
        post_id: Optional[str] = None,
        kind: Literal["comments", "reactions"] = "comments",
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
        offset: int = 0,
        comment_id: Optional[str] = None,
        fields: Optional[list[str]] = None,
        text_max_chars: Optional[int] = _TEXT_EXCERPT_CHARS,
    ) -> dict:
        """LinkedIn posts: your home feed, a post, its engagement. Publishing, commenting
        and reacting is `linkedin_unipile_act` (op="publish_post" / "comment_post" /
        "react_post").

        `op`:
        - **"feed"** (default): your LinkedIn home, read LIVE — one page per call,
          sorted by publication date (most recent first), nothing stored.
          Sponsored/promo inserts are excluded. The choice of posts follows your
          LinkedIn home setting. Next page: pass the returned `cursor` again
          (`None` = end of the stream). Returns `{items, cursor, count}`.
          **Served as a TRIAGE VIEW**: each post returns enough to rank it (author +
          headline, date, traction, link, `urn`) and its text cut at 600 characters
          (`text_truncated: true` marks the cut) — a page of 40 raw posts exceeds
          the size of a tool result, and sorting a feed is done on the header.
          Nothing is lost: `fields=["*"]` returns all the post's fields,
          `text_max_chars=None` the full text, and a whole post is read with
          `op="get"` (`post_id=<urn>`).
        - **"get"**: a post — `post_id` = social_id (`urn:li:…`) from a
          `linkedin_unipile_profile(op="posts")` result.
        - **"engagement"**: who reacted/commented — `kind`='comments' or 'reactions'.
          Follows the pages up to `limit` people (default 100, max 500); returns
          `{items, count, truncated, next_offset, stopped}` — `truncated: true` says that
          the list is PARTIAL, resume with `offset=next_offset`. `comment_id` targets
          a comment's replies (comments) or reactions (reactions). An Unipile
          429 is the `unipile_rate_limited` refusal (delay in
          `retry_after_seconds`); after a first page, it returns the partial result.

        Args:
            op: feed (default) | get | engagement.
            post_id: post's social_id (get, engagement).
            kind: op="engagement" — 'comments' (default) or 'reactions'.
            limit: op="feed" — posts requested for this page (default 20);
                op="engagement" — people to return at most (default 100, max 500).
            cursor: op="feed" — the next page: the `cursor` from a previous call
                (omitted = the first page).
            offset: op="engagement" — where to resume (the `next_offset` of a previous
                call; 0 = start).
            comment_id: op="engagement" — a comment of the post: its replies
                (kind='comments') or its reactions (kind='reactions').
            fields: op="feed" — projection: the requested fields, plus the `urn`
                always kept to address the post. Omitted = the triage view;
                `["*"]` = all the post's fields.
            text_max_chars: op="feed" — text length of each post (default 600;
                `None` = full text).
        """
        if op == "feed":
            return _feed(unipile_client(), limit, cursor, fields, text_max_chars)

        client = unipile_client()

        if op == "get":
            pid = _need(post_id, "post_id", op)
            return _scrape(_actor_key(), lambda: client.get_post(pid))

        if op == "engagement":
            want = _ENGAGEMENT_DEFAULT if limit is None else limit
            if not 1 <= want <= _ENGAGEMENT_MAX:
                raise _bad(f"op='engagement': limit between 1 and {_ENGAGEMENT_MAX} "
                           f"(got {want}).")
            return _engagement(client, kind, _need(post_id, "post_id", op),
                               comment_id, offset, want)

        raise _bad("op must be 'feed', 'get' or 'engagement' — publishing is "
                   "`linkedin_unipile_act`.")

    # ---- network: connections & invitations -------------------------------

    @mcp.tool(annotations=LECTURE)
    def linkedin_unipile_network(
        op: Literal["relations", "invitations"] = "relations",
        direction: Literal["received", "sent"] = "received",
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
        fields: Optional[list] = None,
    ) -> dict:
        """Your LinkedIn network: 1st-degree connections and invitations. Inviting,
        answering or withdrawing an invitation is `linkedin_unipile_act`.

        `op`:
        - **"relations"** (default): your 1st-degree connections — to target/export your
          direct network. Paginated (`cursor`). Returns `{items, cursor, …}` — a single
          list. `fields` = PROJECTION: keeps only those fields on each item
          (e.g. `["name","headline","public_identifier","created_at"]`), plus
          `member_id` always kept — reduces an export's payload. A key
          that no connection on the page carries is REFUSED, with the keys
          present.
          ⚠️ Pagination is NOT reliable for an EXHAUSTIVE export: the `cursor` encodes a
          volatile offset (duplicates in the offset space, overestimated total) and a
          `limit=100` page returns 90-100 items, not 100. To load a whole network:
          deduplicate by `member_id` (NEVER the offset), keep ≤6 pages in parallel,
          ~20 s between two salvos (measured: 8 in parallel → `429 "We only allow
          10 requests"` on 3 of them; beyond that, cascading 502s), prove
          exhaustion with 2 staggered passes.
          ⚠️ These six precautions are ALL there is to know: they live here,
          there is no page to go read. The `bulk-load` guide deals with something else
          — delegating a big load to a sub-agent — and says nothing about the
          pagination traps above. ⚠️ An incomplete load does NOT RAISE,
          it returns fewer people: a process that reads the absence as "not a
          connection" will then act on a wrong answer.
        - **"invitations"**: the connection invitations. `direction`='received'
          (received, to accept) or 'sent' (sent, pending). Paginated — `limit`
          (default 50, MAX 100: beyond that upstream returns "Invalid querystring";
          and with no limit the whole backlog exceeds the token limit).
          For the next page, pass the `cursor` RETURNED by the previous call
          — and only that: a hand-made cursor is refused. No more returned `cursor`
          = end of the backlog (a SHORT page is not the end), unless
          `pagination_note` is present: it then says why pagination
          stopped there, and there is nothing more to ask for. `direction` is
          repeated on each page, it cannot be deduced from the cursor.

        Args:
            op: relations (default) | invitations.
            direction: op="invitations" — 'received' (default) or 'sent'.
            cursor: pagination (relations, invitations) — always the one
                returned by the previous call, never built by hand.
            limit: page size.
            fields: op="relations" — field projection (`member_id` always
                kept; key absent from the whole page = refusal).
        """
        client = unipile_client()

        if op == "relations":
            return _page_de_relations(
                client.list_relations(cursor=cursor, limit=limit), fields)

        if op == "invitations":
            return client.list_invitations(direction,
                                           limit=limit if limit is not None else 50,
                                           cursor=cursor)

        raise _bad("op must be 'relations' or 'invitations' — acting on an invitation "
                   "is `linkedin_unipile_act`.")

    # ---- account: premium slate (Recruiter / Sales Navigator) -------------

    @mcp.tool()
    def linkedin_unipile_account(
        op: Literal["status", "contracts", "select", "inmail_balance"] = "contracts",
        contract_id: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> dict:
        """The connected LinkedIn account: its STATE (op="status"), and its premium
        Recruiter / Sales Navigator slate (the three other ops).

        - **"status"** — "is my LinkedIn connected?": `connected`,
          `account_id`, `account_name`, and `alive` (the session may be DEAD while
          the account stays linked — checkpoint, rotated cookie). Always answers;
          `connected:false` carries `next_step`, the missing step. **This is the op to
          use to verify a messaging onboarding** (#452: an agent had invented
          it, got an `invalid_arguments` and wrongly concluded
          that the channel was not connected). Right after a connection flow,
          pass `account_id` = the `account_id=…` value from the return page's
          address: this is what proves WHICH account to link; without it, the linking is
          refused (`binding.reason = "ambiguous_candidates"`) if several
          connections happened at the same time on the shared key.
        - **"contracts"** (default): the available premium contracts — the `id` to
          pass to op="select".
        - **"select"**: activates a contract for the premium calls that follow.
        - **"inmail_balance"**: InMail credit balance (premium messages).

        The three premium ops require the matching subscription ON the connected
        account and the premium seat activated at connect
        (`unipile_connect_start(premium=…)`) — otherwise the premium APIs answer
        403 "out of your scope". `op="status"`, for its part, requires nothing.

        Args:
            op: status | contracts (default) | select | inmail_balance.
            contract_id: op="select" — id returned by op="contracts".
            account_id: op="status" — the `account_id` read from the address of the
                connection flow's return page (proof of the account to link).
        """
        # BEFORE `unipile_client()`: it RAISES when no account is linked, which
        # is exactly the state that `status` must be able to report (#452).
        if op == "status":
            return account_status("LINKEDIN",
                                  account_id_hint=(account_id or "").strip() or None)
        if account_id is not None:
            raise _bad("`account_id` only applies to op='status' (the proof of the account "
                       "to link on return from a connection flow)")

        client = unipile_client()

        if op == "contracts":
            return client.list_contracts()
        if op == "select":
            return client.select_contract(_need(contract_id, "contract_id", op))
        if op == "inmail_balance":
            return client.inmail_balance()

        raise _bad("op must be 'status', 'contracts', 'select' or 'inmail_balance'")

    # ---- Recruiter: job postings & candidates (reads) ---------------------

    @mcp.tool()
    def linkedin_unipile_job(
        op: Literal["postings", "posting", "applicants", "applicant",
                    "projects"] = "postings",
        job_id: Optional[str] = None,
        applicant_id: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """Job postings and candidates of the LinkedIn Recruiter account (reads).

        `op`:
        - **"postings"** (default): the recruiter account's job postings. Paginated.
        - **"posting"**: detail of a posting (`job_id` from op="postings").
        - **"applicants"**: candidates of a posting. Paginated.
        - **"applicant"**: detail of a candidate (`applicant_id` from op="applicants").
        - **"projects"**: hiring projects. The
          `hiring_project_id` feeds `linkedin_unipile_profile(op="action")`
          (pipeline). Paginated.

        Args:
            op: postings (default) | posting | applicants | applicant | projects.
            job_id: posting id (posting, applicants, applicant).
            applicant_id: op="applicant" — candidate id.
            cursor: pagination.
            limit: page size.
        """
        client = unipile_client()

        if op == "postings":
            return client.list_job_postings(cursor=cursor, limit=limit)
        if op == "posting":
            return client.get_job_posting(_need(job_id, "job_id", op))
        if op == "applicants":
            return client.list_job_applicants(_need(job_id, "job_id", op),
                                              cursor=cursor, limit=limit)
        if op == "applicant":
            return client.get_job_applicant(_need(job_id, "job_id", op),
                                            _need(applicant_id, "applicant_id", op))
        if op == "projects":
            return client.list_hiring_projects(cursor=cursor, limit=limit)

        raise _bad("op must be 'postings', 'posting', 'applicants', 'applicant' "
                   "or 'projects'")


# --- The "connect" step, declared HERE and not in the auth module -------------
#
# ⚠️ A flow is declared at IMPORT of its module. `unipile_connect` is imported
# only INSIDE the handlers (lazy import): declaring it there amounted to never
# declaring it at boot — the production catalogue did not see it, while the
# tests did because their fixture imported the convenience module.
# Third time this week that a test bench diverges from the real setup; the
# rule that comes out of it is simple: **a declaration lives in a module that boot
# loads**, and `tools/unipile.py` is one (the connector is in the registry).
connector_flow.declare(
    "unipile",
    start=lambda ctx, values: _start_hosted_flow(ctx, values),
    label="Connect a messaging account",
    params=(connector_flow.FlowParam(
        name="channel", label="Channel to connect", default="linkedin",
        options=(("linkedin", "LinkedIn"), ("whatsapp", "WhatsApp"),
                 ("telegram", "Telegram"), ("instagram", "Instagram"))),),
)

# ⚠️ **One flow per CHANNEL, without a channel parameter** (split of 2026-08-28). Before, a
# single `unipile` flow carried a `channel` to pick from a list: the card
# asked "which one?" because it represented all six. Now each channel
# has its card, hence its flow, and the channel is DERIVED from the connector
# (`Connector.hosted_channel`) instead of being typed in. The step loses nothing and gains
# a guard: you can no longer start a WhatsApp connection from the
# Telegram card. The front has nothing to change — it renders `connect.params`, which is
# simply empty here.
#
# `unipile` itself has NO flow any more: it is the provider account, its card sets
# a key. The `unipile_connect_start(channel=…)` tool stays multi-channel (it
# belongs to no capability — see the `unipile` namespace).
#
# ⚠️ This sentence is FALSE with respect to the declaration above, and staying that way is deliberate
# (2026-08-29). The multi-channel flow of `unipile` is PRODUCTION code that the
# split was supposed to leave intact — `test_le_compte_garde_son_code_de_production` holds
# it. What was supposed to change was on the SCREEN side: the front rendered this list of
# six on all SEVEN cards, so the WhatsApp card offered to connect LinkedIn.
# Fixed there (third-party front v1.17.0, `hostedChannelOf`), by reading
# `auth.hosted_channel` — the account's card keeps its six, each channel card
# has only its own.
for _con in providers.REGISTRY.values():
    if not _con.hosted_channel:
        continue
    connector_flow.declare(
        _con.name,
        # `_ch` captured by value (argument default): a closure over `_con`
        # would make the six flows identical, all on the last channel of the loop.
        start=(lambda ctx, values, _ch=_con.hosted_channel.lower():
               _start_hosted_flow(ctx, {**values, "channel": _ch})),
        label=f"Connect my {_con.label} account",
    )
del _con


async def _start_hosted_flow(ctx, values: dict):
    """Delegates to the shared REST+MCP body, lazily imported (it pulls the provider's
    client). The two outcomes of the flow — link to open, or account adopted — are
    handled there: adoption becomes a typed refusal, not a mangled contract."""
    from .. import unipile_connect
    return await unipile_connect._start_flow(ctx, values)
