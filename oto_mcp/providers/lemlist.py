"""Registry declaration for the `lemlist` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "lemlist", ["lemlist"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key",
    label="Lemlist", help="cold outreach, signals and website visitors",
    href="https://app.lemlist.com",
    # Three modules for ONE connector, for readability rather than scope:
    # `lemlist` holds the campaign and its leads, `lemlist_crm` everything else
    # (CRM, inbox, unsubscribes, signals, settings), `lemlist_lignes` the single
    # action that pushes a table's rows BY REFERENCE. The namespace stays
    # `lemlist` everywhere — it is the namespace, not the file name, that the
    # gate reads (`namespace_of`).
    modules=("lemlist", "lemlist_crm", "lemlist_lignes"),
)

CATEGORY = "Outreach"
PUBLISHER = "lemlist"
LOGO_DOMAIN = "lemlist.com"

DESCRIPTION = (
    "Lemlist cold outreach campaigns: create and run a campaign, "
    "manage its leads, plus the rest of the account (native CRM, inbox, "
    "unsubscribes, signals including the companies visiting your website, "
    "settings) in a separate module."
)
