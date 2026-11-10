"""Registry declaration of the `teamtailor` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "teamtailor", ["teamtailor"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Teamtailor",
    help="ATS — candidates, jobs, applications (JSON:API)",
    href="https://www.teamtailor.com",
)

CATEGORY = "HR & recruiting"
PUBLISHER = "Teamtailor"
LOGO_DOMAIN = "teamtailor.com"

DESCRIPTION = (
    "Recruiting tracked in Teamtailor (ATS): candidates, job postings and "
    "applications."
)
