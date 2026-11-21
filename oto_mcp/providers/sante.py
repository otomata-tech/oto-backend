"""Registry declaration of the `sante` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "sante", ["sante"], secret_kind="none",
    # "Santé" promised a domain while it was only a directory of establishments
    # (2026-09-02); medicines, trials, publications and conflicts of interest make
    # it one.
    label="Health data",
    help="FINESS directory of healthcare and medico-social establishments + "
         "HAS ESSMS evaluations, medicines, clinical trials, biomedical publications, "
         "declared conflicts of interest (open data)",
)

CATEGORY = "Public data"
PUBLISHER = "HAS / FINESS"
DESCRIPTION = (
    "French healthcare and medico-social establishments: "
    "complete FINESS directory and HAS ESSMS evaluations, with "
    "multi-criteria search. Also French medicines (BDPM, with safety "
    "notices and shortages), clinical trials (ClinicalTrials.gov, CTIS), "
    "biomedical publications (PubMed, Europe PMC) and conflicts of "
    "interest declared by health companies (Transparence Santé)."
)
LOGO_DOMAIN = "has-sante.fr"
