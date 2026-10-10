"""Registry declaration of the `hubspot` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# byo keyed api_key, outside the base set (opt-in, installable from the library), no
# platform key (everyone sets their own). Inert until activated in DB
# (connector_activation, deny-by-default), like foncier/sante.
CONNECTOR = _c(
    "hubspot", ["hubspot"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="HubSpot",
    help="CRM (contacts, companies, deals, tickets, notes, lists/segments, properties, pipelines)",
    href="https://app.hubspot.com",
    # `hubspot_lignes` only carries `hubspot_push_rows`: the rows of a table
    # pushed BY REFERENCE; `hubspot_pipelines` carries `hubspot_pipeline` (read-only).
    # Same namespace, hence same activation.
    modules=("hubspot", "hubspot_lignes", "hubspot_pipelines"),
)

CATEGORY = "Prospecting"
PUBLISHER = "HubSpot"
LOGO_DOMAIN = "hubspot.com"

DESCRIPTION = (
    "The HubSpot CRM: contacts, companies, deals, tickets, notes, "
    "lists and custom properties. Outside the base set, to be activated per org; no "
    "platform key, everyone sets their own."
)
