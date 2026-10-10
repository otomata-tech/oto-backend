"""Admin capability: what is left on every PLATFORM key (ADR 0009).

The instance pays for its platform keys: when one runs dry, every org that resolves
it gets refused at once, and nothing said so beforehand. Each balance was only
learned through the 402 of a customer's run — FullEnrich at one credit and Apollo at
zero were both found by hand, on the same evening (07/10/2026).

This reads them all in ONE call, so a scheduled agent can watch them: for each
platform row of the vault, it runs the connector's own registered probe
(`connectors/verify.py`) on that key and returns the verdict, plus the balance when
the probe reads one.

## What it does NOT invent

- **The balance comes from the probe, nowhere else.** A probe that covers `auth`
  only says the key authenticates: `balance` is then null, and null reads "not
  measured", never "zero" nor "plenty". The `coverage` field says which.
- **No probe = `no_probe`**, not `ok`. The key may be fine; nothing measured it.
- **`unknown` reads "I don't know"**, same rule as `oto_admin_instance_health`.

## Cost

Probes are side-effect free, not all free of charge: `serper` makes a one-result
search (one credit) and `jev` one yes/no answer. Called once a day, that is the
whole cost of the watch.

Read-only, `PLATFORM_ADMIN`. Never returns a secret. Platform rows are excluded
from health persistence by the verify capability; this one writes nothing at all.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from pydantic import BaseModel, Field

from .. import credentials_store
from ..connectors import verify as connector_verify
from ._authz import PLATFORM_ADMIN
from ._types import Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)

#: Not a probe verdict: the connector registers no probe at all.
NO_PROBE = "no_probe"


class PlatformBalancesInput(BaseModel):
    provider: Optional[str] = Field(
        default=None,
        description="One connector only (e.g. `fullenrich`). Omitted = every platform key.")


class PlatformKeyBalance(BaseModel):
    provider: str
    label: str
    # `ok` | `no_quota` | `unauthorized` | `unknown` (probe verdicts) | `no_probe`.
    verdict: str
    # What the probe measured: `auth` (the key authenticates, NOTHING about the
    # balance), `auth+quota`, `auth+scopes`, or null when there is no probe.
    coverage: Optional[str] = None
    # `{"restant": <number>, "unite": "credits"|"usd", "limite"?: <number>}` as the
    # probe returned it. ⚠️ null = NOT MEASURED, never zero.
    balance: Optional[dict] = None
    error: Optional[str] = None
    next_step: Optional[str] = None
    elapsed_ms: int = 0


class PlatformBalances(BaseModel):
    checked_at: float
    keys: list[PlatformKeyBalance]


async def _probe_one(provider: str, label: str) -> dict:
    # Every key carries the FULL shape: an absent `balance` would be read by a
    # caller however it likes, a null one says "not measured".
    base = {"provider": provider, "label": label,
            "coverage": connector_verify.couverture(provider), "balance": None,
            "error": None, "next_step": None, "elapsed_ms": 0}
    probe = connector_verify.probe_for(provider)
    if probe is None:
        return {**base, "verdict": NO_PROBE,
                "next_step": ("no probe for this connector: watch its 402s in "
                              "`oto_admin_monitoring op=calls errors=true` instead.")}
    t0 = time.monotonic()
    try:
        # Vault read off the loop (`docs/event-loop-perf.md`): no SQL in the loop.
        row = await asyncio.to_thread(
            credentials_store.get_credential_with_meta,
            credentials_store.PLATFORM, label, provider, "")
        if not row:
            raise RuntimeError("the platform row no longer resolves (removed, or undecryptable "
                               "— see `oto_admin_vault_health`).")
        fields = credentials_store.unpack_secret(provider, row["secret"])
        mesures = await connector_verify.executer(
            probe, fields, credentials_store.public_meta(row.get("meta")),
            instance=(credentials_store.PLATFORM, label, ""))
    except Exception as e:  # noqa: BLE001 — a failure IS the result, per key
        verdict = connector_verify.classer(e)
        logger.info("platform balance %s/%s → %s", provider, label, verdict)
        return {**base, "verdict": verdict, "error": str(e)[:500],
                "next_step": connector_verify.CONDUITE.get(verdict),
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}
    return {**base, "verdict": connector_verify.OK, "balance": mesures.get("quota"),
            "elapsed_ms": int((time.monotonic() - t0) * 1000)}


async def _platform_balances(ctx: ResolvedCtx, inp: PlatformBalancesInput) -> dict:
    rows = await asyncio.to_thread(credentials_store.list_platform_credentials,
                                   inp.provider)
    # Concurrent: each probe is bounded (`connector_verify._BORNE_S`), so the whole
    # sweep takes as long as the slowest provider, not their sum.
    keys = await asyncio.gather(*(_probe_one(r["provider"], r["label"]) for r in rows))
    return {"checked_at": time.time(), "keys": list(keys)}


CAPABILITIES += [
    Capability(
        key="admin.platform_balances", handler=_platform_balances,
        Input=PlatformBalancesInput, Output=PlatformBalances,
        authz=PLATFORM_ADMIN,
        description=(
            "[platform admin] What is left on every PLATFORM key (the keys the "
            "instance pays for): runs each connector's own side-effect-free probe on "
            "its platform key and returns, per key, `verdict` (ok | no_quota | "
            "unauthorized | unknown | no_probe), `coverage` and `balance` "
            "(`{restant, unite, limite?}`). ⚠️ `balance: null` means NOT MEASURED — "
            "a probe that covers `auth` only proves the key authenticates — never "
            "zero. For a connector without a balance, watch its 402s in "
            "`oto_admin_monitoring`. Optional `provider` narrows to one connector. "
            "Read-only; serper and jev probes cost one call each."),
        mcp="oto_admin_platform_balances",
        rest=RestBinding("GET", "/api/admin/platform-keys/balances"),
    ),
]
