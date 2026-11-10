"""Registry declaration of the `sante` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "sante", ["sante"], secret_kind="none",
    # "Santé" promised a domain; it is a directory of establishments (2026-09-02).
    label="Healthcare establishments",
    help="FINESS directory of healthcare and medico-social establishments + "
         "HAS ESSMS evaluations (open data)",
)

CATEGORY = "Public data"
PUBLISHER = "HAS / FINESS"
DESCRIPTION = (
    "French healthcare and medico-social establishments: "
    "complete FINESS directory and HAS ESSMS evaluations, with "
    "multi-criteria search."
)
LOGO_DOMAIN = "has-sante.fr"
