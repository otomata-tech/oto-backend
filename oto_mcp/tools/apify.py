"""Apify — rent an already-written scraper instead of writing one (apify.com).

Wraps `oto.tools.apify.client.ApifyClient` (API v2). keyed `api_key` (Bearer),
byo-only: each user/org connects THEIR account — an actor is billed by usage.

Apify is not a scraper but a **catalog of scrapers** (the *actors*): Google
Maps, LinkedIn, Instagram, Amazon, Booking, TikTok… Each actor has its own input
JSON, described on its Store page. Hence the path:

1. `apify_store_search("google maps")` → find the actor and its identifier.
2. `apify_actor(id)` → read its card (default options, memory, timeout).
3. `apify_run_sync(id, input)` → launch and fetch the results (≤ 300 s),
   or `apify_run` + `apify_run_status` + `apify_dataset_items` for a long job.

A running actor costs money: setting `max_items` / `timeout_secs` /
`max_total_charge_usd` AT LAUNCH is the only protection — afterwards, it is billed.

Calls to the client are written out in plain (`_client().run(…)`) and not dispatched
by name: that is what makes them verifiable by the version-skew probe.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..connectors import verify as connector_verify


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _upstream_message(e) -> str:
    status = e.status_code
    if status in (401, 403):
        return (f"Apify rejected the token (HTTP {status}) — check the key configured "
                "on this connector (Apify: Settings → API & Integrations).")
    if status == 402:
        return ("Apify: insufficient credits/plan (402) — top up the account, or "
                "narrow the run's scope (max_items).")
    if status == 404:
        return (f"Apify: not found (404) — check the identifier. An actor is written "
                f"`username/actor-name` (or its id), a run/dataset is an opaque id. {e.body}")
    if status == 408:
        return ("Apify: the run exceeded the 300 s of synchronous mode — relaunch with "
                "`apify_run`, then `apify_run_status` and `apify_dataset_items`.")
    if status == 429:
        return "Apify: too many requests (429) — retry in a moment."
    if status in (500, 502, 503, 504):
        return f"Apify is temporarily unavailable (HTTP {status}) — retry later."
    return f"Apify refused the request (HTTP {status}): {e.body}"


_LIMITS_URL = "https://api.apify.com/v2/users/me/limits"


def _verify(fields: dict, config: dict | None = None) -> dict:  # noqa: ARG001
    """"Test the connection" probe — covers `auth+quota`.

    `GET /v2/users/me/limits`: authenticated, free, and the only Apify call that says
    how much of the month is left — `limits.maxMonthlyUsageUsd` against
    `current.monthlyUsageUsd`. Listing actors (the former probe) authenticated just
    as well on an account that had hit its cap, and every run then failed upstream.

    The balance is in US DOLLARS of platform usage, not in credits: Apify bills
    compute and proxy, not results.
    """
    import requests

    r = requests.get(_LIMITS_URL, headers={"Authorization": f"Bearer {fields['key']}"},
                     timeout=15)
    if r.status_code in (401, 403):
        raise connector_verify.NonAutorise(
            f"Apify refused the token (HTTP {r.status_code}).")
    r.raise_for_status()
    data = (r.json() or {}).get("data") or {}
    plafond = (data.get("limits") or {}).get("maxMonthlyUsageUsd")
    consomme = (data.get("current") or {}).get("monthlyUsageUsd")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
               for v in (plafond, consomme)):
        raise RuntimeError(
            f"Apify answered without a readable monthly usage: {str(data)[:200]}")
    restant = round(plafond - consomme, 2)
    if restant <= 0:
        raise connector_verify.QuotaEpuise(
            f"The Apify token is good, but the monthly usage cap is reached "
            f"(${consomme:.2f} of ${plafond:.2f}). Raise the cap or wait for the next "
            "cycle — reconnecting would change nothing.")
    return {"quota": {"restant": restant, "unite": "usd", "limite": plafond}}


def register(mcp: FastMCP) -> None:
    from oto.tools.apify.client import ApifyClient
    from oto.tools.common.errors import UpstreamHTTPError

    connector_verify.register("apify", _verify, couvre=connector_verify.AUTH_QUOTA)

    def _client() -> ApifyClient:
        key, _ = access.resolve_api_key("apify")
        return ApifyClient(api_key=key)

    @contextmanager
    def _upstream():
        """Turn an Apify refusal into an actionable tool error."""
        try:
            yield
        except ValueError as e:
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))

    # --- find the actor -----------------------------------------------------

    @mcp.tool()
    def apify_store_search(
        search: Optional[str] = None,
        limit: int = 20,
        category: Optional[str] = None,
        sort_by: Optional[str] = None,
    ) -> dict:
        """Search Apify's public Store for an actor that already scrapes your target.

        Start here: naming a target ("google maps reviews", "linkedin company",
        "amazon products") is how you find the actor to run, along with its pricing
        and its identifier.

        Returns — `{data: {items: [{id, username, name, title, description, stats,
            pricingInfos, …}], total}}`. Run it with `username/name`.

        Args:
            search: free text describing the site or data you want.
            category: Store category (e.g. `"SOCIAL_MEDIA"`, `"ECOMMERCE"`).
            sort_by: `"relevance"` | `"popularity"` | `"newest"` | `"lastUpdate"`.
        """
        with _upstream():
            return _client().store_search(search=search, limit=limit,
                                          category=category, sort_by=sort_by)

    @mcp.tool()
    def apify_actors(limit: int = 50, offset: Optional[int] = None) -> dict:
        """List the actors of THIS account (not the public Store)."""
        with _upstream():
            return _client().actors(limit=limit, offset=offset)

    @mcp.tool()
    def apify_actor(actor_id: str) -> dict:
        """Fetch one actor's card: builds, versions and `defaultRunOptions`
        (default memory and timeout).

        Read it before a first run to size `memory_mbytes`/`timeout_secs`. The
        actor's INPUT fields are documented on its Store page, not here.

        Args:
            actor_id: `username/actor-name` (as shown in the Store) or its id.
        """
        with _upstream():
            return _client().actor(actor_id)

    # --- launch -------------------------------------------------------------

    @mcp.tool()
    def apify_run_sync(
        actor_id: str,
        run_input: Optional[dict] = None,
        max_items: Optional[int] = None,
        limit: Optional[int] = None,
        fields: Optional[list[str]] = None,
        timeout_secs: Optional[int] = None,
        memory_mbytes: Optional[int] = None,
        max_total_charge_usd: Optional[float] = None,
    ) -> Any:
        """Run an actor and return its results in one call. Waits up to 300 s.

        The normal path for a bounded scrape. Beyond 300 s Apify answers 408 — use
        `apify_run` then `apify_run_status`/`apify_dataset_items` instead.

        Returns: the LIST of dataset items.

        Args:
            actor_id: `username/actor-name` or id.
            run_input: the actor's own input JSON — its fields are specific to each
                actor (e.g. `{"searchStringsArray": ["bakery Marseille"],
                "maxCrawledPlaces": 20}` for the Google Maps scraper). See the
                actor's Store page.
            max_items: cap on BILLED items (pay-per-result actors) — set it.
            limit / fields: paginate and project the returned items (some actors
                return very wide objects).
            timeout_secs / memory_mbytes: run budget on Apify's side.
            max_total_charge_usd: hard cost ceiling for this run.
        """
        with _upstream():
            return _client().run_sync_dataset_items(
                actor_id, run_input=run_input, max_items=max_items, limit=limit,
                fields=fields, timeout_secs=timeout_secs,
                memory_mbytes=memory_mbytes,
                max_total_charge_usd=max_total_charge_usd)

    @mcp.tool()
    def apify_run(
        actor_id: str,
        run_input: Optional[dict] = None,
        max_items: Optional[int] = None,
        timeout_secs: Optional[int] = None,
        memory_mbytes: Optional[int] = None,
        max_total_charge_usd: Optional[float] = None,
        wait_for_finish: Optional[int] = None,
    ) -> dict:
        """Start an actor WITHOUT waiting for it — for scrapes longer than 300 s.

        Returns — `{data: {id, status, defaultDatasetId, …}}` — keep `id` for
            `apify_run_status` and `defaultDatasetId` for `apify_dataset_items`.

        Args:
            wait_for_finish: seconds to wait before returning (max 60) — enough to
                catch a short run without polling.
        """
        with _upstream():
            return _client().run(
                actor_id, run_input=run_input, max_items=max_items,
                timeout_secs=timeout_secs, memory_mbytes=memory_mbytes,
                max_total_charge_usd=max_total_charge_usd,
                wait_for_finish=wait_for_finish)

    @mcp.tool()
    def apify_run_status(run_id: str, wait_for_finish: Optional[int] = None) -> dict:
        """Check a run: `status` (READY, RUNNING, SUCCEEDED, FAILED, TIMED-OUT,
        ABORTED), `defaultDatasetId` (where the output is) and `usageTotalUsd`
        (what it cost so far)."""
        with _upstream():
            return _client().run_status(run_id, wait_for_finish=wait_for_finish)

    @mcp.tool()
    def apify_abort_run(run_id: str, gracefully: Optional[bool] = None) -> dict:
        """Abort a running actor — stops the billing.

        Args:
            gracefully: let the actor finish its current item and flush its results
                (a hard abort may lose what wasn't pushed yet).
        """
        with _upstream():
            return _client().abort_run(run_id, gracefully=gracefully)

    # --- read the output ----------------------------------------------------

    @mcp.tool()
    def apify_dataset_items(
        dataset_id: str,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        fields: Optional[list[str]] = None,
        omit: Optional[list[str]] = None,
        clean: Optional[bool] = None,
    ) -> Any:
        """Read the results a run produced.

        Returns: the LIST of items.

        Args:
            dataset_id: the run's `defaultDatasetId`.
            fields / omit: keep or drop keys — worth using, several actors return
                objects with dozens of fields per item.
            clean: skip empty/hidden items.
        """
        with _upstream():
            return _client().dataset_items(
                dataset_id, limit=limit, offset=offset, fields=fields,
                omit=omit, clean=clean)
