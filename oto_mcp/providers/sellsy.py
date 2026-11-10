"""Registry declaration of the `sellsy` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# sellsy: CRM + FR sales management (CRM and invoicing in the same
# account). Credential = OAuth2 **client_credentials** (client_id + client_secret
# of a "personal" access in the developer portal) → multi-field, not keyed:
# the call key is a derived token, not the stored secret. byo-only — a Sellsy
# account belongs to a company, there is no platform key to share.
CONNECTOR = _c(
    "sellsy", ["sellsy"], auth_modes={"byo_user", "byo_org"},
    secret_kind="fields", label="Sellsy",
    help="CRM + FR sales management (third parties, opportunities, quotes, invoices, payments)",
    href="https://www.sellsy.fr", credential_fields=(
        CredentialField("client_id", "Client ID", secret=True,
                        help="Sellsy → Settings → Developer portal → API V2"),
        CredentialField("client_secret", "Client Secret", secret=True),
    ),
)

CATEGORY = "CRM"
PUBLISHER = "Sellsy"
LOGO_DOMAIN = "sellsy.com"

DESCRIPTION = (
    "A company's CRM and sales management in Sellsy: third parties, "
    "opportunities, quotes, invoices and payments, in the same account. OAuth2 "
    "with a personal access from the Sellsy developer portal."
)
