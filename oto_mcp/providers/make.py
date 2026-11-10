"""Registry declaration of the `make` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

CONNECTOR = _c(
    "make", ["make"], auth_modes={"byo_user", "byo_org"}, secret_kind="fields",
    label="Make",
    help="workflow automation — scenarios, execution, logs (API v2)",
    href="https://www.make.com", credential_fields=(
        CredentialField("api_token", "API token", secret=True),
        CredentialField("base_url", "Zone URL", secret=False,
                        help="e.g. https://eu1.make.com or https://us1.make.com"),
    ),
)

CATEGORY = "AI & automation"
PUBLISHER = "Make"
LOGO_DOMAIN = "make.com"

DESCRIPTION = (
    "Make (ex-Integromat) scenarios: list, trigger and follow "
    "execution, and read logs, via the v2 API. Two fields: the API token "
    "and the zone URL of your account (Europe or US)."
)
