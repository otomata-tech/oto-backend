"""Registry declaration of the `sirene` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# `fr` (live APIs SIRENE/Recherche Entreprises/INPI/BODACC/BOAMP) + `fr_groupe`
# (capital chain: legal-entity officers from the RNE, #337) + `fr_stock`
# (SIRENE parquet stock, former `sirene_stock` connector, merged 2026-06-22:
# same FR companies domain, fr_stock_* namespace → namespace_of="fr").
# default_quota=0 (unlimited): FR company data open to everyone, without
# credits. Most fr_* are open-data/parquet (no key); only
# fr_siret/fr_avis_sirene/fr_headquarters touch the shared INSEE key —
# unmetered. The only remaining cap = the INSEE rate limit (30 req/min) on
# the shared key, passed through as-is (429) without oto throttling.
CONNECTOR = _c(
    "sirene", ["fr"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    secret_kind="api_key", default_quota=0, platform_key_open=True,
    label="FR companies & public procurement",
    help="identity, officers, financial statements, BODACC, BOAMP tenders, aids and "
         "subsidies, company agreements, Egapro, SIRENE stock",
    href="https://api.insee.fr", modules=("fr", "fr_stock", "fr_groupe"),
)

CATEGORY = "Public data"
# The publisher is NOT INSEE: of the 25 tools, public procurement (BOAMP),
# aids, company agreements and Egapro come from other administrations.
# The label and publisher said "INSEE SIRENE" until 2026-09-02 — a
# label under which nobody looks for a tender.
PUBLISHER = "INSEE, INPI, DILA and FR open data"
DESCRIPTION = (
    "Unified French company data: multi-criteria "
    "search, aggregated profile (identity + INPI financial statements + BODACC "
    "events), officers, BOAMP public procurement, public "
    "aids, company agreements and the Egapro index. Includes the "
    "full SIRENE stock (~43 M "
    "establishments) for batch: head offices, establishments, "
    "NAF/municipality search."
)
LOGO_DOMAIN = "insee.fr"
