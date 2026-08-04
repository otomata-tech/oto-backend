"""Check CRM — thin connector over Julien's "enrichment" job-change-check API.

Wraps `oto.tools.checkcrm.CheckCrmClient` (https://enrichment-two.vercel.app/v1).
Key resolved per call via `access.resolve_api_key("checkcrm")` — byo-only (no
platform key). Named `checkcrm` (one token) rather than `check_crm` so
`namespace_of` (first underscore-separated token) resolves correctly; the
user-facing label in the registry is still "Check CRM".

Fire-and-forget: `checkcrm_send_contacts` triggers an async job-change check on
enrichment's side and returns only `{checkId, contactCount, skippedCount}` —
results are pushed by enrichment to a per-network webhook it owns, not
retrievable through this connector. See docs/sf-api.md on the enrichment repo.
"""
from __future__ import annotations

import time
from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access

# Same idiom as oto_mcp/tools/folk.py's _bulk_run/_bulk_fatal (no shared module
# exists yet to import this from — Folk is the only other precedent, and its
# helpers are private to that file, so this is duplicated rather than reaching
# into another connector's internals).
#
# Cap derived from the REST tool-invoke path's hard timeout (`api_routes.py`,
# `asyncio.wait_for(_invoke(), timeout=45)`): at ~0.15s courtesy delay between
# calls plus enrichment's own per-item latency, this aims for a comfortable
# margin under 45s rather than cutting it close.
_BULK_MAX_ITEMS = 50
_BULK_DELAY_S = 0.15


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _bulk_fatal(exc: Exception) -> bool:
    """Auth/connection errors abort the whole batch (repeating the same error N
    times helps no one). Everything else (one rejected item, a 422 from
    enrichment...) stays a per-item error that doesn't interrupt the batch."""
    from oto.tools.common.errors import UpstreamHTTPError
    import requests

    if isinstance(exc, UpstreamHTTPError):
        return exc.status_code in (401, 403)
    return isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))


def _bulk_run(items: list, fn) -> list[tuple[int, bool, object]]:
    """Runs `fn(item)` for each item, with a small courtesy delay between calls.
    Returns a list of `(index, ok, value_or_error_message)` — the caller builds
    its own receipt shape from that."""
    if len(items) > _BULK_MAX_ITEMS:
        raise _bad(
            f"Too many items ({len(items)}) — max {_BULK_MAX_ITEMS} per call, "
            f"split into multiple calls."
        )
    results: list[tuple[int, bool, object]] = []
    for i, item in enumerate(items):
        try:
            results.append((i, True, fn(item)))
        except Exception as e:
            if _bulk_fatal(e):
                raise
            results.append((i, False, str(e)))
        if i < len(items) - 1:
            time.sleep(_BULK_DELAY_S)
    return results


def register(mcp: FastMCP) -> None:
    from oto.tools.checkcrm.client import CheckCrmClient

    def _client() -> CheckCrmClient:
        key, _ = access.resolve_api_key("checkcrm")
        return CheckCrmClient(api_key=key)

    @mcp.tool()
    def checkcrm_send_contacts(
        account_id: str,
        contacts: list[dict],
        account_linkedin_url: Optional[str] = None,
    ) -> dict:
        """Send a batch of contacts to enrichment for an async LinkedIn job-change
        check, grouped under one account/company.

        `account_id` is your own identifier for the account/company these contacts
        belong to (echoed back nowhere on this call, but scopes the account-level
        grouping). `contacts` items: `id` (required — your own contact identifier),
        `linkedinUrl` (required for a contact to actually be checked; contacts
        without one are skipped and counted in `skippedCount`), plus optional
        `firstName`/`lastName`/`name`/`title`/`email`. `account_linkedin_url` sets
        the expected employer for job-change matching (omit for "no expected
        company" — the check still runs, just without a same/changed verdict).

        Returns `{checkId, contactCount, skippedCount}` immediately. This does NOT
        return check results — enrichment pushes those asynchronously to a
        per-network webhook it owns; there is no way to retrieve them through this
        tool.
        """
        return _client().send_contacts(account_id, contacts, account_linkedin_url)

    @mcp.tool()
    def checkcrm_add_subsidiary(
        company_linkedin_url: str,
        subsidiary_linkedin_url: Optional[str] = None,
        subsidiary_linkedin_urls: Optional[list[str]] = None,
        subsidiary_name: Optional[str] = None,
    ) -> dict:
        """Add a subsidiary brand under a parent company, for enrichment's
        job-change matcher (a contact found at a known subsidiary reads as "same
        company" as the parent).

        Pass exactly one of `subsidiary_linkedin_url` (single) or
        `subsidiary_linkedin_urls` (batch, looped server-side — enrichment has no
        native batch endpoint for this; max 50 per call). Idempotent either way:
        an already-existing (company, subsidiary) pair is reported as a
        duplicate instead of erroring.

        `subsidiary_linkedin_url`/`subsidiary_linkedin_urls` MAY be numeric LinkedIn
        company IDs — accepted and resolved to their vanity form in the background
        (the single-item response includes `resolving: true` when that happens; a
        following `checkcrm_list_subsidiaries` call may not reflect it
        immediately — no separate notification is sent). `company_linkedin_url`
        (the parent) must already be the vanity-slug form.

        Single mode returns the direct API response
        (`{subsidiary, duplicate, reclassified?, resolving?, resolvingNote?}`).
        Batch mode returns a receipt `{total, succeeded, failed: [{index, error}]}`
        — a bad/revoked API key aborts the whole batch immediately rather than
        failing item by item. `subsidiary_name` only applies to single mode (a
        shared display name across a batch of different subsidiaries wouldn't be
        meaningful).
        """
        if (subsidiary_linkedin_url is None) == (subsidiary_linkedin_urls is None):
            raise _bad(
                "Pass exactly one of subsidiary_linkedin_url (single) or "
                "subsidiary_linkedin_urls (batch), not both or neither."
            )
        if subsidiary_linkedin_urls is not None and subsidiary_name:
            raise _bad(
                "subsidiary_name only applies to a single subsidiary_linkedin_url, "
                "not a batch — drop subsidiary_name or switch to subsidiary_linkedin_url."
            )
        client = _client()
        if subsidiary_linkedin_url is not None:
            return client.add_subsidiary(company_linkedin_url, subsidiary_linkedin_url, subsidiary_name)
        results = _bulk_run(
            subsidiary_linkedin_urls,
            lambda url: client.add_subsidiary(company_linkedin_url, url),
        )
        failed = [{"index": i, "error": val} for i, ok, val in results if not ok]
        return {
            "total": len(subsidiary_linkedin_urls),
            "succeeded": len(subsidiary_linkedin_urls) - len(failed),
            "failed": failed,
        }

    @mcp.tool()
    def checkcrm_list_subsidiaries(
        slug: str = None,
        name: str = None,
    ) -> dict:
        """List parent companies with their subsidiary brands, optionally filtered.

        Sans argument : tous les parents de ce réseau qui ont au moins une filiale,
        chacun avec sa liste imbriquée. `slug` = un slug LinkedIn nu (`"acme-corp"`,
        une URL de société complète marche aussi) ou un id LinkedIn numérique ;
        `name` = sous-chaîne insensible à la casse. Les deux ensemble sont OR-és, pas
        cumulés. Le slug/nom d'une FILIALE remonte son parent avec toute sa fratrie —
        jamais une filiale seule. Aucun résultat = `{"companies": []}`, pas une erreur
        (≠ l'ancienne forme, qui exigeait le parent et rendait un 404).

        Une filiale ajoutée à l'instant via un id LinkedIn numérique peut encore
        apparaître sous cette forme numérique, ou avoir déjà fusionné avec une entrée
        existante — cf. la docstring de `checkcrm_add_subsidiary` sur la résolution
        asynchrone.
        """
        return _client().list_subsidiaries(slug=slug, name=name)
