"""Registry declaration of the `crunchbase` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# LinkedIn is no longer a browser connector here: replaced by the `unipile`
# connector (hosted LinkedIn). The local LinkedIn browser stays in oto-cli.
# crunchbase: company/person records via the frontend's PRIVATE API
# (`www.crunchbase.com/v4/data`, v4 schema without user_key). Execution =
# **Browserbase** (hosted remote Chrome, ADR 0026): the user logs in once via
# Live View (`crunchbase_connect_start`), their session persists in a Context =
# the per-user credential (`crunchbase` vault). No more in-process DOM scraping.
CONNECTOR = _c(
    "crunchbase", ["crunchbase"], auth_modes={"byo_user"}, personal_session=True,
    secret_kind="cookie", label="Crunchbase",
    help="company/person records (Browserbase session)", publisher="Crunchbase",
    href="https://www.crunchbase.com/",
)

CATEGORY = "Lead & company data"
PUBLISHER = "Crunchbase"
LOGO_DOMAIN = "crunchbase.com"

DESCRIPTION = (
    "Crunchbase company and person records, via your session connected through a "
    "hosted browser. You log in once, the session then persists "
    "in the vault."
)
