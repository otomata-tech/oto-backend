"""Registry declaration of the `pennylane` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "pennylane", ["pennylane"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key",
    # The general ledger, quotes, supplier invoices, the bank, reference data written,
    # exports and the other records are domains of their own:
    # their own module, same key and same namespace (`pennylane_*`, the activation
    # gate reads the 1st token).
    modules=("pennylane", "pennylane_ledger", "pennylane_devis", "pennylane_achats",
             "pennylane_banque", "pennylane_referentiel", "pennylane_exports",
             "pennylane_documents"),
    label="Pennylane", help="accounting", href="https://app.pennylane.com",
)

CATEGORY = "Finance"
PUBLISHER = "Pennylane"
LOGO_DOMAIN = "pennylane.com"

DESCRIPTION = (
    "The company's accounting in Pennylane: quotes, invoices and their payments, "
    "customers and contacts, suppliers, bank accounts and their balance, bank "
    "transactions, trial balance, reference data (analytics, journals, chart of "
    "accounts), exports (FEC, general ledger), subscriptions and the general "
    "ledger — read entries, post one, letter lines with each other. To be "
    "distinguished from `pennylaneged`, which gives access to the document store (GED) via "
    "a browser session rather than an API key. Permissions depend on the key "
    "set, not on the connector: each action requires its own scope."
)
