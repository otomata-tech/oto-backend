"""Registry declaration of the `bodacc` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# bodacc: the BODACC as a whole (DILA open data, `annonces-commerciales` dataset on
# OpenDataSoft) — every notice published over a period, by family, department,
# court, city or full text, and counts. NO credential, no quota of ours.
# ⚠️ Not a duplicate of `sirene`: `fr_events` / `fr_events_batch` read the notices
# of companies ALREADY KNOWN by their SIREN; `bodacc_*` finds the companies from the
# notices (every receivership in a department this week). The two stay separate
# connectors because a connector carries one credential model, and `sirene` is keyed.
CONNECTOR = _c(
    "bodacc", ["bodacc"], secret_kind="none",
    label="BODACC legal notices",
    help="every BODACC notice by period, family (receiverships, sales, creations, "
         "deregistrations, accounts filed…), department, court or full text — and "
         "counts by family, department or month (DILA open data)",
    href="https://www.bodacc.fr/pages/api-bodacc/",
)

CATEGORY = "French data"
PUBLISHER = "DILA"
DESCRIPTION = (
    "The French official bulletin of civil and commercial notices (BODACC), as "
    "open data from the DILA: search every notice published over a period by "
    "family (insolvency proceedings, business sales, creations, changes, "
    "deregistrations, filed accounts), department, court, city or full text; "
    "count them by family, department or month; read one notice in full."
)
LOGO_DOMAIN = "bodacc.fr"
