"""Registry declaration for the `lever` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "lever", ["lever"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Lever",
    help="ATS — opportunities (candidates), postings, stages, notes",
    href="https://www.lever.co",
)

CATEGORY = "HR & recruiting"
PUBLISHER = "Lever"
LOGO_DOMAIN = "lever.co"

DESCRIPTION = (
    "Recruiting tracked in Lever (ATS): opportunities (candidates), "
    "postings (job openings), pipeline stages and notes."
)
