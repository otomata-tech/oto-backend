"""Registry declaration of the `ashby` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "ashby", ["ashby"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Ashby",
    help="ATS — candidates, jobs, applications, notes",
    href="https://www.ashbyhq.com",
)

CATEGORY = "HR & recruiting"
PUBLISHER = "Ashby"
LOGO_DOMAIN = "ashbyhq.com"

DESCRIPTION = (
    "Recruiting tracked in Ashby (ATS): candidates, job postings, "
    "applications and notes."
)
