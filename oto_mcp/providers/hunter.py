"""Registry declaration of the `hunter` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "hunter", ["hunter"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    secret_kind="api_key", default_quota=5, platform_key_open=True,
    label="Hunter.io", help="emails", href="https://hunter.io",
)

CATEGORY = "Lead & company data"
PUBLISHER = "Hunter.io"
LOGO_DOMAIN = "hunter.io"

DESCRIPTION = (
    "Find and verify professional email addresses with Hunter.io. "
    "A free platform key is available, with a limited daily quota."
)
