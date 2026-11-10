"""Registry declaration of the `fireflies` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# fireflies: meeting transcripts, live meeting control, AskFred
# (AI Q&A), org (users/groups/channels/bites/analytics/audit). GraphQL (a single
# POST endpoint), keyed api_key (Bearer), byo-only (no platform key).
# Webhooks V1/V2 = dashboard-only at Fireflies, no GraphQL query/mutation
# for it — deliberately absent from this connector's MCP surface.
CONNECTOR = _c(
    "fireflies", ["fireflies"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Fireflies",
    help="meeting transcripts, live meeting, AskFred, org",
    href="https://fireflies.ai",
)

CATEGORY = "Calls & meetings"
PUBLISHER = "Fireflies.ai"
LOGO_DOMAIN = "fireflies.ai"

DESCRIPTION = (
    "The meetings recorded by Fireflies: transcripts, control of a "
    "live meeting, questions asked to AskFred (AI Q&A on the content), and "
    "organization data (users, groups, channels, analytics)."
)
