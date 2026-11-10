"""Registry declaration of the `wttj` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# wttj: Welcome to the Jungle's ATS (ex-Welcome Kit), recruiter side. Bearer token
# ISSUED by WTTJ to the account holder (no self-service
# generation), byo-only: each recruiter sets THEIR key.
CONNECTOR = _c(
    "wttj", ["wttj"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Welcome to the Jungle",
    help="Welcome to the Jungle ATS — jobs and pipeline stages, candidates, "
         "comments",
    href="https://developers.welcomekit.co",
)

CATEGORY = "HR & recruiting"
PUBLISHER = "Welcome to the Jungle"
LOGO_DOMAIN = "welcometothejungle.com"

DESCRIPTION = (
    "Recruitment tracked in Welcome to the Jungle's ATS: jobs and their "
    "pipeline stages, candidates (add, move, archive), comments "
    "and history of moves."
)
