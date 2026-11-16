"""Registry declaration of the `openrouter` key carrier.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# ⚠️ `kind="credential"`: NO tool. This object only carries the key the platform uses ON
# BEHALF of the org — a scheduled agent on an open model of the `openrouter` family
# (`runner_models`) consumes it, no tool exposes it. Distinct from the `jev` key, which is
# an OpenRouter key too but serves ONE tool: a key carrier is the only thing the worker
# may pull at claim (`runner_jobs._cle_de_modele` guards on the type).
#
# `byo_org`: the ORGANIZATION's key — it pays for the turns, and a scheduled agent
# outlives its author. `platform`: a SHARED key (the instance's) serves an org that
# deposited none and that it is GRANTED to (`platform_key_open=False`: the grant is the
# right), each model call journaled at the cost OpenRouter reported for it, unpriced
# (`tours_au_cout`), for the commerce to bill back. Every grant carries a daily quota in
# micro-dollars of that cost (`grant_quota_unit`): the default, 0, would be unlimited on
# our key. Each call is checked against it before it is sent, and its cost debited once
# reported (`tours_au_cout`).
CONNECTOR = _c(
    "openrouter", [], kind="credential", auth_modes={"byo_org", "platform"}, keyed=True,
    default_quota=0, platform_key_open=False,
    grant_quota_unit="micro-dollars (US$ 0.000001) of model cost",
    # ⚠️ SINGLE-account, declared: a run goes on ONE key — two deposits for the same org
    # would be two invoices for the same work, with no criterion to choose between them.
    cardinality="mono",
    # ONE secret, as `mistral`: the screens deposit a single-field key as `api_key`.
    secret_kind="api_key", label="OpenRouter",
    help="OpenRouter key for the open models (DeepSeek, MiniMax) — used by the "
         "organization's scheduled agents, never by a tool. Without one, a platform key "
         "granted to the organization is used and billed.",
    href="https://openrouter.ai/settings/keys",
)

CATEGORY = "AI & automation"
PUBLISHER = "OpenRouter"
LOGO_DOMAIN = "openrouter.ai"
