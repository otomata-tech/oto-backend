"""Registry declaration of the `tally` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# tally: online forms — forms, questions, blocks, responses,
# analytics, workspaces, folders, organization members, webhooks
# (38 operations, full coverage of the public API). keyed api_key
# (Bearer `tly-…`), **byo-only and byo-only by nature**: a Tally key is tied
# to ONE user, inherits their rights (no fine-grained scope exists on Tally's side)
# and stops working if they leave the organization — a shared platform
# key would therefore be a named account in disguise, not a service key.
CONNECTOR = _c(
    "tally", ["tally"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Tally",
    help="forms: responses, questions, blocks, analytics, workspaces, webhooks",
    href="https://tally.so",
)

CATEGORY = "Marketing & SEO"
PUBLISHER = "Tally"
LOGO_DOMAIN = "tally.so"

DESCRIPTION = (
    "The online forms created with Tally: responses, questions, blocks, "
    "analytics, workspaces, folders, organization members and "
    "webhooks. A Tally key is personal — it inherits the rights of the "
    "person who created it, and stops working if they leave "
    "the organization."
)
