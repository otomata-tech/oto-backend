"""Registry declaration of the `folk` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# folk: born AFTER the vault — no legacy users.folk_api_key column,
# the connector_credentials vault is canonical. byo-only (no platform
# key); shared team account = credential of the Otomata org.
CONNECTOR = _c(
    "folk", ["folk"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key",
    # No `cardinality`: `api_key` derives it as multi. Only the STATIC
    # announcement of the axis stays curated (N named keys of the same member, historical use).
    account_axis_static=True,
    label="Folk", help="CRM — contacts, companies, deals & custom objects",
    href="https://app.folk.app",
)

CATEGORY = "CRM"
PUBLISHER = "Folk"
LOGO_DOMAIN = "folk.app"

# ⚠️ The listing used to announce "coexists with the `folkmcp` connector (official MCP by
# OAuth)" until 2026-09-09: `folkmcp` left with the MCP federation (ADR
# 0069), so that sentence promised the user a connector they would find
# nowhere. A listing is SERVED TEXT — it gets fixed together with the code it
# describes, not on the next pass.
DESCRIPTION = (
    "The Folk CRM: contacts, companies, deals and custom objects, via API key."
)
