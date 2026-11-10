"""Registry declaration of the `bls` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# bls: wages and employment by occupation in the United States (OEWS survey of the
# Bureau of Labor Statistics), through the public API v2 — open data, NO credential.
# ⚠️ Without a registration key the API caps at 25 requests per DAY, counted on
# the calling address: the cap is therefore shared by the whole platform. The
# free key (500/day) is set on the operator side, as the env variable `BLS_API_KEY` that the
# server reads and passes to the client (`tools/bls.py`) — it is not a user
# credential, hence `secret_kind="none"` and no cascade.
CONNECTOR = _c(
    "bls", ["bls"], secret_kind="none",
    label="US wages (BLS)",
    help="wages and employment by occupation in the United States — P10 to P90 distribution by "
         "SOC code, national, by State or by metropolitan area (BLS OEWS open data)",
    href="https://www.bls.gov/oes/",
)

CATEGORY = "Public data"
PUBLISHER = "U.S. Bureau of Labor Statistics"
DESCRIPTION = (
    "Wages and employment by occupation in the United States, from the Bureau "
    "of Labor Statistics open data (OEWS survey): annual mean, median and P10 to "
    "P90 percentiles for a SOC code, national, by State or by metropolitan "
    "area. Latest published year only."
)
LOGO_DOMAIN = "bls.gov"
