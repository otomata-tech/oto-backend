"""Registry declaration of the `frenchtech` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "frenchtech", ["frenchtech"], secret_kind="none",
    label="French Tech", help="ecosystem directory of a French Tech capital (startups/organizations/service providers) + events, calls for projects, funding + French Tech Central (open data, default Aix-Marseille)",
)

CATEGORY = "Public data"
PUBLISHER = "La French Tech (open data)"
DESCRIPTION = (
    "The ecosystem of a French Tech capital (default Aix-Marseille): "
    "directory of startups, organizations and service providers, events, "
    "calls for projects, funding and French Tech Central."
)
LOGO_DOMAIN = "lafrenchtech.com"
