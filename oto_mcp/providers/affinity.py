"""Registry declaration of the `affinity` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# affinity: relationship CRM (persons, companies, opportunities, lists, field
# values, notes, interactions). One API key, created PER USER in Affinity
# (Settings → Manage Apps), that ACTS AS that user: writes are attributed to
# them and their sharing rules decide what the key sees. Hence byo_user first;
# byo_org makes every member act as the person who created the key.
#
# Strict BYOK (no platform mode): it is the client's own CRM. API access needs
# an Affinity Scale, Advanced or Enterprise plan.
CONNECTOR = _c(
    "affinity", ["affinity"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key",
    label="Affinity",
    help="relationship CRM: people, companies, deals, lists, notes, interactions",
    href="https://www.affinity.co",
)

CATEGORY = "Prospection"
PUBLISHER = "Affinity"
LOGO_DOMAIN = "affinity.co"

DESCRIPTION = (
    "Affinity, the relationship CRM: search people and companies, read and edit "
    "lists and their columns (dropdowns by name), create deals, write notes, see "
    "who in your team knows whom and the emails and meetings exchanged. Actions "
    "run as the Affinity user who created the API key; API access needs a Scale, "
    "Advanced or Enterprise plan."
)
