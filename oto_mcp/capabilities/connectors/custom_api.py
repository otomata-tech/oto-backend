"""What the CONNECTOR capabilities say about the org's custom connectors ("Custom API",
connector `api`) — kept out of `selection.py` and `instances.py`, which are over the
500-line ceiling (`docs/conventions.md`), so that the Custom API grows none of them.

- `repli` — the way out of a "no such connector" refusal: connect the tool through
  Custom API, with the setup link pre-filled with the searched name (never a secret).
- `projection_for_name` — `oto_connector(name=…)` for a name that is no catalog
  connector but the label (or account) of a custom connector the caller reaches (their
  own personal key, then the org's).
- `stamp_instances` — the `custom_api` card of each `api` instance in the roster
  (`/api/me/connector-instances`).

The data itself (definitions in `connector_instances.config`) is read by
`connectors/api_operations.py`, the one module allowed on that key.
"""
from __future__ import annotations

import logging
from typing import Optional

from ... import links
from ...connectors import api_operations
from .._types import ResolvedCtx

logger = logging.getLogger(__name__)


def _http_reachable(catalog: list[dict]) -> bool:
    return any(c["name"] == "api" for c in catalog)


def repli(catalog: list[dict], demande: str,
          ctx: Optional[ResolvedCtx]) -> tuple[str, dict]:
    """The way out when the tool has no card: the Custom API connector (`api`), IF the
    org can reach it — `(sentence, details)`, the sentence appended to the refusal and
    `details = {"custom_api": {connector, setup_url}}`. Its setup link carries the
    searched name, never a secret — the key is typed in the form. `("", {})` when
    `api` is not open to the org."""
    if not _http_reachable(catalog):
        return "", {}
    url = links.connector_setup_link("api", sub=ctx.sub if ctx else None,
                                     org=ctx.org_id if ctx else None, label=demande)
    where = f" at {url}" if url else " (`api(op=\"setup_link\")` gives the link)"
    return (f" There is no dedicated connector, but a tool with an API key can be "
            f"connected through the Custom API connector (`api`){where}. Send the "
            f"link to whoever holds the key: they paste it in that form — never ask "
            f"for the key in the chat."), {"custom_api": {"connector": "api",
                                                         "setup_url": url}}


def projection_for_name(ctx: ResolvedCtx, catalog: list[dict], name: str, *,
                        verbose, include) -> Optional[dict]:
    """The `connectors.me` answer when `name` designates a custom connector the caller
    reaches (their own, then the org's) — `connectors` empty, `custom_connector`
    filled, its `scope` said — or None."""
    if not _http_reachable(catalog):
        return None
    perso = api_operations.match_org_connector(ctx.org_id, ctx.sub, name)
    if perso is None:
        return None
    return {"connectors": [], "verbose": verbose, "include": include or [],
            "credentials": "computed", "readiness": "not_computed",
            "custom_connector": perso}


def stamp_instances(out: list[dict], connues: dict, cles: list) -> None:
    """Each `api` instance ("Custom API") is a custom connector the front renders as
    its OWN card: `custom_api = {label, description, logo_domain, actions_count}`, read
    from the instance's saved definition (one query, no decryption). Fail-open and
    logged, like `id`: the listing never falls for it."""
    ids = {i: connues[c]["id"] for i, c in enumerate(cles)
           if c and c[2] == "api" and c in connues}
    if not ids:
        return
    try:
        resumes = api_operations.summaries_for_instances(list(ids.values()))
    except Exception:
        logger.warning("instances: custom connector definitions unavailable "
                       "(fail-open)", exc_info=True)
        return
    for i, iid in ids.items():
        if iid in resumes:
            resume = dict(resumes[iid])
            if resume["label"] == "Custom API" and out[i].get("name"):
                resume["label"] = out[i]["name"]     # the card's display name
            out[i]["custom_api"] = resume
