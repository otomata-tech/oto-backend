"""Registry declaration of the `attio` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# attio: outside the core set (2026-06-11) — the official Attio MCP is better for
# now. Code kept (tools/attio.py) for possible custom implementations;
# installable from the library.
CONNECTOR = _c(
    "attio", ["attio"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", default_quota=200,
    label="Attio", help="CRM", href="https://app.attio.com",
)

CATEGORY = "CRM"
PUBLISHER = "Attio"
LOGO_DOMAIN = "attio.com"

DESCRIPTION = (
    "A lightweight, customizable CRM: list, create and update "
    "records (people, companies, deals) according to the schema specific to "
    "your Attio workspace. Outside the core set since June 2026 — Attio's official MCP "
    "is now more complete; this connector remains available for custom "
    "implementations."
)
