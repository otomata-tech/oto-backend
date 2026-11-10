"""Registry declaration of the `mistral` key carrier.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# ⚠️ `kind="credential"`: NO tool. This object only carries the key
# that the platform uses ON BEHALF of the org — a scheduled agent
# consumes it, no tool exposes it. An ordinary connector with empty namespaces
# would present itself as a connector without having its effects; the distinct
# type lets the screen say what it is.
#
# `byo_org` only: the key is the ORGANIZATION's, not a person's —
# it is what pays for the turns, and a scheduled agent outlives its author.
CONNECTOR = _c(
    "mistral", [], kind="credential", auth_modes={"byo_org"}, keyed=True,
    # ⚠️ SINGLE-account, declared: the derivation would return `multi` (api_key), and
    # the screen would offer to set a second key that nothing could choose between.
    # A run goes on ONE key — two deposits for the same org would be two
    # invoices for the same work, and the worker has no criterion to decide.
    cardinality="mono",
    secret_kind="api_key", label="Mistral AI",
    help="Mistral model key — used by the organization's scheduled agents, never by a tool",
    href="https://console.mistral.ai/api-keys",
)

CATEGORY = "AI & automation"
PUBLISHER = "Mistral AI"
LOGO_DOMAIN = "mistral.ai"
