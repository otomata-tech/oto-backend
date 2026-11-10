"""Registry declaration of the `grain` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). Cf. `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# grain: meeting recordings, transcripts, sharing, webhooks,
# organization data. keyed api_key (Bearer + Public-Api-Version header),
# byo-only (no platform key) — Personal Access Token (per user) or
# Workspace Access Token (admin, access to all workspace data).
CONNECTOR = _c(
    "grain", ["grain"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Grain",
    help="meeting recordings, transcripts, sharing, webhooks, org",
    href="https://grain.com",
)

CATEGORY = "Calls & meetings"
PUBLISHER = "Grain"
LOGO_DOMAIN = "grain.com"

DESCRIPTION = (
    "The meetings recorded by Grain: transcripts, sharing, webhooks and "
    "organization data. Personal token or workspace token (admin access to "
    "all of the workspace's data)."
)
