"""Which steps, in which order, on which key: the call's `providers`, else the card's
`steps` setting, else the default table — and each step's key mode, read once per
carrier BEFORE anything is spent."""
from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Optional

from ..mcp_errors import McpError
from .etape import Step
from .valeurs import _bad

logger = logging.getLogger(__name__)

#: Key modes that mean "the caller's OWN key": theirs, their team's, their org's. NOT
#: `access.views.BYO_MODES`, which counts `tenant` too: from an org's side the tenant's
#: key is a SHARED key — the instance's operator pays it, and on the primary tenant it is
#: rebilled like the platform's (`credits.rang_refacture`). A step that the shared key must
#: not serve (`Step.own_key_only`) never runs on it, and the default order puts it behind.
#: `elsewhere` (step 5, #1499): an account of another org — a key of its own, which the
#: operator does not pay.
_OWN_KEY = ("user", "group", "org", "elsewhere")
#: …and those that mean "no usable key": the step is skipped without a call.
_NO_KEY = ("forbidden", "over_quota")


def _parse_steps(raw) -> list[str]:
    items = raw if isinstance(raw, list) else re.split(r"[,;\s]+", str(raw or ""))
    return [str(i).strip().lower() for i in items if str(i).strip()]


def _carrier(tool: str) -> Optional[str]:
    """The vault carrier of the connector serving `tool` (`linkedin_aiark_person` →
    `aiark`, `fr_get` → `sirene`, `linkedin_unipile_profile` → `unipile`)."""
    from .. import providers
    from ..tool_visibility import namespace_of
    con = providers.connector_for_namespace(namespace_of(tool))
    return providers.credential_provider(con.name) if con is not None else None


def _configured_steps(ns: str) -> Optional[list[str]]:
    """The `steps` setting of the waterfall's card for the caller (member override >
    team > org > tenant), or None. SYNC — it reads the vault: off the loop."""
    from .. import access
    try:
        rc = access.resolve_credential(ns, want="byo", emit_on_failure=False)
    except McpError:
        return None  # nothing set anywhere: the default order
    return _parse_steps((rc.fields or {}).get("steps")) or None


def _key_modes(sub: Optional[str], tools: list[str]) -> dict[str, str]:
    """`tool → key mode` for each step, BEFORE anything is spent. SYNC (database)."""
    from .. import access, providers
    out: dict[str, str] = {}
    by_carrier: dict[str, str] = {}
    for tool in tools:
        carrier = _carrier(tool)
        if carrier is None or carrier not in providers.CREDENTIAL_PROVIDERS:
            out[tool] = "open"
            continue
        if carrier not in by_carrier:
            if not sub:
                by_carrier[carrier] = "unknown"
            else:
                try:
                    by_carrier[carrier] = access.credential_mode_for(sub, carrier)
                except Exception:  # noqa: BLE001 — a probe: the step's own call will judge
                    logger.warning("waterfall: key mode of %s unreadable", carrier,
                                   exc_info=True)
                    by_carrier[carrier] = "unknown"
        out[tool] = by_carrier[carrier]
    return out


def _opened(step: Step) -> Step:
    """`step`, its `own_key_only` lifted for this call when the instance opens it on its
    shared key (`Step.shared_serves`). SYNC (settings): off the loop, with the key modes."""
    if step.own_key_only and step.shared_serves is not None and step.shared_serves():
        return replace(step, own_key_only=False)
    return step


def _plan(ns: str, steps: tuple[Step, ...], providers_arg: Optional[list[str]],
          sub: Optional[str]) -> tuple[list[Step], list[str], str, dict[str, str]]:
    """(ordered steps, ignored setting keys, where the order came from, key modes)."""
    by_key = {s.key: s for s in steps}
    ignored: list[str] = []
    if providers_arg is not None:
        keys = _parse_steps(providers_arg)
        if not keys:
            raise _bad("`providers` is empty — omit it to use your configured or the "
                       "default order.")
        unknown = [p for p in keys if p not in by_key]
        if unknown:
            raise _bad(f"Unknown provider(s) {unknown} — accepted here: {list(by_key)}.")
        source = "call"
    else:
        configured = _configured_steps(ns) or []
        ignored = [p for p in configured if p not in by_key]
        keys = [p for p in configured if p in by_key]
        source = "setting" if keys else "default"
        if not keys:
            keys = list(by_key)
    ordered = [_opened(by_key[k]) for k in dict.fromkeys(keys)]
    modes = _key_modes(sub, [s.tool for s in ordered])
    if source == "default":
        # Free steps first (open data, the caller's own session), then a quota-only
        # search on the caller's OWN key (lemlist's database: nothing billed, on a quota
        # that is theirs), then their other own keys, then shared keys — the sort is
        # stable: each group keeps the table's order. A WRITTEN order is not re-sorted.
        ordered.sort(key=lambda s: (0 if s.free
                                    else 1 if s.quota_only and modes.get(s.tool) in _OWN_KEY
                                    else 2 if modes.get(s.tool) in _OWN_KEY
                                    else 3))
    return ordered, ignored, source, modes


def own_key_missing(step: Step, mode: str) -> bool:
    """A step that only the caller's own key may serve, on any other key — or on a key
    mode that could not be read (`unknown`: no caller, or the probe failed). Only a key
    PROVEN to be the caller's lets it run: an unread mode is not a proof."""
    return step.own_key_only and mode not in _OWN_KEY
