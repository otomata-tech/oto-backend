"""Registry declaration for the `linear` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# linear: issues, projects, cycles (sprints), teams, labels, comments,
# webhooks. keyed api_key (`Authorization` header WITHOUT a `Bearer` prefix —
# a Linear specificity), **byo_user + byo_org**, no platform key. A Linear API
# key is a PERSONAL key (Settings → Security & access → Personal
# API keys): it acts on behalf of its holder, in the workspaces they have
# access to — like notion or slack. It can therefore be set for oneself as well as for
# the org (24/09/2026). No platform pool: unlike a pooled vendor
# credit balance that can be shared (AI Ark, see the retired `linkedin` connector,
# #279), nothing justifies a key shared by oto.
CONNECTOR = _c(
    "linear", ["linear"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Linear",
    help="issues, projects, cycles, teams, labels, comments, webhooks",
    href="https://linear.app",
)

CATEGORY = "Product & data"
PUBLISHER = "Linear"
LOGO_DOMAIN = "linear.app"

DESCRIPTION = (
    "Linear project tracking: issues, projects, cycles (sprints), teams, "
    "labels, comments and webhooks. The Linear API key is personal: it "
    "acts on behalf of its holder. Set it for yourself, or for the org if it must "
    "serve everyone; no key shared by oto."
)
