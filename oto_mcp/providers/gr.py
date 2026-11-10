"""Registry declaration of the `gr` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# Greece: entity lookup via the GEMI registry (autocomplete) + VIES. Open data,
# no key. Inert until activated in the DB (deny-by-default), like foncier/sante.
CONNECTOR = _c(
    "gr", ["gr"], secret_kind="none",
    label="Greece companies",
    help="search a Greek company in the GEMI registry, check a European VAT "
         "number (VIES) — open data",
)

# "Data GR" was both the label and a single-member CATEGORY: neither
# "Greece" nor "companies" appeared, and the type filter carried a line for it
# alone. Filed (2026-09-02) where the European company registers already live —
# hithorizons, topograph.
CATEGORY = "Public data"
PUBLISHER = "GEMI / VIES"
SANS_LOGO_DE_MARQUE = True

DESCRIPTION = (
    "Check a Greek company in the GEMI registry (autocomplete) or an "
    "intra-community VAT number (VIES) — open data, no key to "
    "set up."
)
