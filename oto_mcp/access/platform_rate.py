"""Per-org FAIRNESS on a shared platform key (F-051).

A platform key is ONE account at its provider, shared by every org it serves, with ONE
rate limit there. Nothing bounded how fast a single org could draw on it: the daily
quota (`quotas.quota_for`) is per person and per day, often lifted
(`platform_unmetered`), and says nothing about a burst. One org looping on a shared
key could exhaust the provider's rate limit — or its credit — for every other org.

This module caps the calls ONE org makes on ONE shared key per fixed window, counted
in the database (`db/platform_key_rate.py`) because the server runs as several
processes that share the database and nothing else.

- **What is counted**: one per platform-key resolution of a tool execution
  (`check_usage=True`), not `units` — the limit protects the provider's request
  rate; volume is the quota's job. Debited at RESOLUTION, like `tenant_budget`
  (a call that then fails at the provider still counts; there is no single
  post-call seam to count on success).
- **Who is counted**: the org whose keys the call consumes
  (`scope.org_d_appartenance`), every member together; a call without an org counts
  for the person (`user:<sub>`). The anonymous endpoint counts for the project's org.
- **Rollout**: `OTO_PLATFORM_KEY_ORG_RATE` = `enforce` | `report` (DEFAULT: logs
  each window an org crosses the limit, refuses nothing) | `off` (no counting at
  all).
- **Limits**: `OTO_PLATFORM_KEY_ORG_RATE_LIMIT` calls (default 120) per
  `OTO_PLATFORM_KEY_ORG_RATE_WINDOW_S` seconds (default 60);
  `OTO_PLATFORM_KEY_ORG_RATE_OVERRIDES` = `serper=600,aiark=30` per key carrier,
  `0` = no limit for that connector.
- **An unreadable setting refuses to start** (`verifier_reglages`, called at boot): a
  misspelt mode or a limit that is not a whole number is a configuration error, never
  read as the default in silence.

The counter failing never refuses a call: it is logged, and the call goes through.

**Who would be cut** (#1232): every call past the limit, in `report` as in `enforce`,
is added in memory to the daily top of hashed carriers (`limite_refus_top`), written
off the request's thread.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional

from mcp.types import ErrorData, INVALID_PARAMS

from .. import limite_refus_top, providers
from ..db import platform_key_rate as db_rate
from ..mcp_errors import McpError

logger = logging.getLogger(__name__)

#: The named code of the refusal — tested by callers, never the sentence.
CODE = "platform_key_org_rate_limited"

_ENV_MODE = "OTO_PLATFORM_KEY_ORG_RATE"
_ENV_LIMIT = "OTO_PLATFORM_KEY_ORG_RATE_LIMIT"
_ENV_WINDOW = "OTO_PLATFORM_KEY_ORG_RATE_WINDOW_S"
_ENV_OVERRIDES = "OTO_PLATFORM_KEY_ORG_RATE_OVERRIDES"
_MODES = ("enforce", "report", "off")
DEFAULT_LIMIT = 120
DEFAULT_WINDOW_S = 60


def mode() -> str:
    """Read on each call (a flip bites at the next restart, without code). Absent =
    `report`, the default: measure before biting. Unknown value: raises."""
    # Names LITERAL: the inventory (`env_inventory.py`) finds its reads by the AST.
    lu = os.environ.get("OTO_PLATFORM_KEY_ORG_RATE", "report")
    m = lu.strip().lower()
    if m not in _MODES:
        raise ValueError(f"{_ENV_MODE}={lu!r}: unreadable value, expected one of "
                         f"{', '.join(_MODES)}")
    return m


def _entier(raw: Optional[str], defaut: int, nom: str) -> int:
    """`raw` as a whole number ≥ 0; absent or empty = `defaut`; anything else raises."""
    if raw is None or not raw.strip():
        return defaut
    try:
        n = int(raw.strip())
    except ValueError:
        n = -1
    if n < 0:
        raise ValueError(f"{nom}={raw!r}: unreadable value, expected a whole number "
                         f"(0 or more)")
    return n


def window_s() -> int:
    lu = os.environ.get("OTO_PLATFORM_KEY_ORG_RATE_WINDOW_S")
    n = _entier(lu, DEFAULT_WINDOW_S, _ENV_WINDOW)
    if n < 1:
        raise ValueError(f"{_ENV_WINDOW}={lu!r}: a window lasts at least one second")
    return n


def _overrides() -> dict[str, int]:
    """`OTO_PLATFORM_KEY_ORG_RATE_OVERRIDES` = `serper=600,aiark=30`; a pair without
    `=`, without a name, or without a whole number raises."""
    lu = os.environ.get("OTO_PLATFORM_KEY_ORG_RATE_OVERRIDES") or ""
    limites: dict[str, int] = {}
    for paire in lu.split(","):
        if not paire.strip():
            continue
        nom, egal, valeur = paire.partition("=")
        if not egal or not nom.strip() or not valeur.strip():
            raise ValueError(f"{_ENV_OVERRIDES}={lu!r}: `{paire.strip()}` is not "
                             f"`connector=limit`")
        limites[nom.strip().lower()] = _entier(valeur, DEFAULT_LIMIT, _ENV_OVERRIDES)
    return limites


def _limite_commune() -> int:
    return _entier(os.environ.get("OTO_PLATFORM_KEY_ORG_RATE_LIMIT"), DEFAULT_LIMIT,
                   _ENV_LIMIT)


def limit_for(carrier: str) -> int:
    """Calls per window for one org on `carrier`'s shared key; `0` = no limit."""
    limites = _overrides()
    if carrier in limites:
        return limites[carrier]
    return _limite_commune()


def verifier_reglages() -> None:
    """The startup guard: every `OTO_PLATFORM_KEY_ORG_RATE*` setting is read once, and
    an unreadable one refuses to start, naming it — never a silent default."""
    mode()
    window_s()
    _overrides()
    _limite_commune()


def _porteur(sub: Optional[str], org: Optional[int]) -> Optional[str]:
    if org is not None:
        return f"org:{org}"
    return f"user:{sub}" if sub else None


def refusal(carrier: str, used: int, limit: int, window: int, hint: str = "") -> McpError:
    return McpError(ErrorData(
        code=INVALID_PARAMS,
        message=(f"Too many calls on oto's shared `{carrier}` key from this org: {used} "
                 f"in the current {window}-second window, limit {limit} per org. The key "
                 f"is shared by every org; retry in a minute, or set your own "
                 f"`{carrier}` key{hint} to lift the limit."),
        data={"code": CODE, "connector": carrier, "used": used, "limit": limit,
              "window_s": window}))


def enforce(provider: str, sub: Optional[str], org: Optional[int], *,
            hint: Callable[[], str] = lambda: "") -> None:
    """Count this platform-key call for its org, and refuse it (in `enforce`) past the
    org's limit for the window. `hint` = where to set one's own key, built only when
    refusing."""
    m = mode()
    if m == "off":
        return
    carrier = providers.credential_provider(provider)
    limit = limit_for(carrier)
    qui = _porteur(sub, org)
    if not limit or qui is None:
        return
    window = window_s()
    try:
        used = db_rate.compter(qui, carrier, window)
    except Exception:
        # Fail-open, loudly: a broken counter must not take every platform key down.
        logger.exception("platform-key rate: counter failed for %s on %s — call served",
                         qui, carrier)
        return
    if used <= limit:
        return
    # QUI serait refusé (#1232) : le palmarès des porteurs hachés, par jour.
    limite_refus_top.noter_plateforme(carrier, qui, m, time.time())
    if used == limit + 1:   # once per window and per org, not once per call
        logger.warning("platform-key rate (%s): %s crossed %d calls/%ds on `%s`",
                       m, qui, limit, window, carrier)
    if m == "enforce":
        raise refusal(carrier, used, limit, window, hint())
