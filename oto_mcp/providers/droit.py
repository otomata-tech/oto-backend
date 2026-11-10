"""Registry declaration of the `droit` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# droit: case law (juris_*) + consolidated codes (loi_*) + collective
# agreements (ccn_*) + checks before an act (compliance, read-only), served by the FOD service (fod/juris, fod/loi, fod/ccn). Extracted
# from `sirene`/`fr` (it was not INSEE data: DILA/Justice/Légifrance). Open
# data, no key. 3 namespaces → 1 card "Info légale FR".
CONNECTOR = _c(
    "droit", ["juris", "loi", "ccn", "compliance"], secret_kind="none",
    label="Info légale FR",
    help="case law, consolidated codes, collective agreements (open data DILA/Légifrance)",
    href="https://www.legifrance.gouv.fr", modules=("droit",),
)

CATEGORY = "Public data"
PUBLISHER = "Légifrance / DILA"
DESCRIPTION = (
    "French legal information: case law (Cour de "
    "cassation, Conseil d'État, Conseil constitutionnel, ECHR/CJEU), "
    "versioned consolidated codes (text in force at a given date) and "
    "sector-level collective agreements (KALI). Sources: "
    "DILA/Légifrance."
)
LOGO_DOMAIN = "legifrance.gouv.fr"
