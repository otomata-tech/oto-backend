"""Is a credential AVAILABLE to me? — per connector, across the whole catalog.

Born from oto-backend#1112. A user's agent told them their LinkedIn was not
connected while it was (`linkedin_unipile_account op=status`:
`connected:true, alive:true`). It had not called the status tool: it had read
`state:not_selected` on the catalog row, and understood it as "not connected".
Nothing on the row said an account existed. Twice, for two users.

**Three axes, never confused** — each has its own surface, and none speaks for the other:

- **selected in the toolbox**: `connectors.selection` (`state`). Governs the
  VISIBILITY of tools, nothing else — a `not_selected` connector is still callable
  through `oto_call` (ADR 0036);
- **credential available**: THIS module. A key or account exists for the person
  at some level of the cascade (personal, team, org, tenant, platform), or by an account of another org (`elsewhere`, step 5);
- **verified alive**: never computed here. A session can be dead at the provider
  while the account stays linked; only a probe says so, and it costs a network
  round trip. `next_step` NAMES the tool that verifies it.

**Single source: `access.status_for`**, the snapshot `/api/me` already serves to the
dashboard (PRELOADED presence probe, one in-memory walk per connector, no
decryption). No cascade is recomputed here: the dashboard, `oto_connector` and
`oto_list_my_tools` therefore read the same fact. This module, and only this one, is
what the two agent surfaces call — a second derivation would reopen the contradiction
of #1112 (the tool catalog said everything was "installed" while the card said
"not selected").

⚠️ This is NOT the fitness verdict (`connectors/readiness.py`): that one also reads
the paid option, the quota and the recorded rejection, connector by connector
(~244 ms each). Here we answer a narrower question — "is there anything to
authenticate with?" — but across the WHOLE catalog, because the catalog is where the
agent was drawing its conclusion.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel

from .. import access, providers, status_hints
from . import verify as connector_verify

logger = logging.getLogger(__name__)

# The credential exists and nothing is pending: a key resolves, or (hosted channel)
# an account is linked.
CONNECTED = "connected"
# A key resolves, but one step remains before acting (link one's LinkedIn account
# on an org Unipile key, authorize oto…) — `next_step` says which.
PENDING_STEP = "pending_step"

# `over_quota` is a PLATFORM level whose day is over: the key exists (that's the axis
# here), exhaustion belongs to readiness (`readiness`, targeted read).
# `elsewhere` (#1499): only an account of ANOTHER org serves (step 5) — the list says
# so instead of showing nothing connected while the call is served.
_PALIER = {"user": "user", "group": "group", "org": "org", "tenant": "tenant",
           "platform": "platform", "over_quota": "platform", "elsewhere": "elsewhere"}

# Nature of a hosted channel's credential: an ACCOUNT of the person (their LinkedIn,
# WhatsApp session…), not the key of the provider that carries it.
HOSTED_ACCOUNT = "hosted_account"


class CredentialPresence(BaseModel):
    """The served shape of `par_connecteur` — DESCRIBES the `dict` produced below (same
    regime as `Capability.Output`), on the row of `oto_connector op=list` as on the
    group of `oto_list_my_tools`. Present ONLY when a credential exists: its absence
    means "no key or account resolves for you here" (or `secret_kind=none`, nothing to
    bring), never "not computed" — the envelope says that (`credentials`)."""
    status: Literal["connected", "pending_step"]
    # The cascade level that ANSWERS — the key, not the account: a LinkedIn linked on
    # the org's Unipile key is `level=org`, `nature=hosted_account`. `elsewhere`: an
    # account of another org serves (step 5, #1499; `status_for` names the org).
    level: Literal["user", "group", "org", "tenant", "platform", "elsewhere"]
    # api_key|basic_auth|fields|oauth|cookie|… (`secret_kind`), or `hosted_account`
    # for a hosted channel. Never a value: the NATURE, without the secret.
    nature: Optional[str] = None
    # `connected`: the step that VERIFIES it is alive (status tool, probe);
    # `pending_step`: the missing step. Rendered as is, never rephrased.
    next_step: str


def etape_de_verification(connector: str) -> str:
    """The step that VERIFIES an available credential is alive — rendered as is.

    First the step the connector DECLARES (`status_hints.register_verify_step`:
    LinkedIn has its status tool), otherwise the generic side-effect-free probe
    (`oto_instance op=verify`), otherwise the admission that none exists. Never
    silence: a "connected" row without a step gets read as "verified"."""
    declare = status_hints.verify_step(connector)
    if declare:
        return declare
    if connector_verify.supports(connector):
        return (f"Available, not yet verified alive: "
                f"`oto_instance(op='verify', connector='{connector}')` tests it with no "
                f"side effect — do this before concluding it doesn't work.")
    return ("Available, not yet verified alive: this connector has no side-effect-free "
            "probe, only a first real call can tell.")


def _nature(name: str) -> Optional[str]:
    con = providers.REGISTRY.get(name)
    if con is None:
        return None
    return HOSTED_ACCOUNT if con.hosted_channel else con.secret_kind


def par_connecteur(sub: str, *, org: Optional[int], group: Optional[int]) -> dict[str, dict]:
    """`{connector: {status, level, nature, next_step}}` for each connector with a
    credential available to `sub` in `(org, group)` — ABSENT otherwise (no key or
    account resolves, or the connector asks for none: `secret_kind=none`).

    `org`/`group` are EXPLICIT, like `readiness.diagnose`: the computation follows the
    subject, never a requester's context.

    Raises if the snapshot cannot be read: the caller must SAY so (fail-visible), not
    this module returning `{}` — an empty result would be read as "nothing is
    connected", which is exactly the false conclusion being fixed."""
    snapshot = access.status_for(sub, org=org, group=group)["providers"]
    out: dict[str, dict] = {}
    for name, entry in snapshot.items():
        palier = _PALIER.get(entry.get("mode") or "")
        if palier is None:          # `forbidden`: nothing resolves
            continue
        attente = entry.get("pending_action")
        out[name] = {
            "status": PENDING_STEP if attente else CONNECTED,
            "level": palier,
            "nature": _nature(name),
            "next_step": attente or etape_de_verification(name),
        }
    return out


# What a surface's envelope says about the computation — always, never silence.
COMPUTED = "computed"
UNAVAILABLE = "unavailable"


def lire(sub: str, *, org: Optional[int]) -> tuple[dict[str, dict], str]:
    """`(par_connecteur(...), "computed")`, or `({}, "unavailable")` if the snapshot
    cannot be read — logged, and RETURNED: each surface puts this status in its
    envelope, so a row without `credential` is not read as "nothing is connected" on
    the day the read itself failed. The entry point for both agent surfaces
    (`oto_connector`, `oto_list_my_tools`): one computation, one possible failure,
    reported the same way.

    For the CALLER (both surfaces read their own toolbox): the team is their active
    team, read INSIDE the `try` — a team-read hiccup is a computation failure like any
    other, reported the same way."""
    try:
        return par_connecteur(sub, org=org, group=access.current_group(sub)), COMPUTED
    except Exception:
        logger.warning("available credential unreadable for the catalog (fail-visible)",
                       exc_info=True)
        return {}, UNAVAILABLE
