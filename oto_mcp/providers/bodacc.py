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
# notices (every receivership in a department this week). A connector of its own for
# ONE reason — ACTIVATION: `sirene` is on by default for everyone, while sweeping the
# whole bulletin is a capability an org opts into (`default_active=False`). Not a
# credential matter: `sirene` already carries ~20 keyless tools. The FR open-data
# convention (ADR 0028, via the FOD service) stays open for later: the client lives in
# the public lib so the switch is a routing change, not a rewrite.
CONNECTOR = _c(
    "bodacc", ["bodacc"], secret_kind="none",
    label="BODACC legal notices",
    help="every BODACC notice by period, family (receiverships, sales, creations, "
         "deregistrations, accounts filed…), department, court or full text — and "
         "counts by family, department or month (DILA open data)",
    href="https://www.bodacc.fr/pages/api-bodacc/",
)

CATEGORY = "Public data"
PUBLISHER = "DILA"
DESCRIPTION = (
    "The French official bulletin of civil and commercial notices (BODACC), as "
    "open data from the DILA: search every notice published over a period by "
    "family (insolvency proceedings, business sales, creations, changes, "
    "deregistrations, filed accounts), department, court, city or full text; "
    "count them by family, department or month; read one notice in full."
)
LOGO_DOMAIN = "bodacc.fr"
