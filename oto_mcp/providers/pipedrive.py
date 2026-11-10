"""Registry declaration of the `pipedrive` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# pipedrive: personal API token (1 secret) + OPTIONAL non-secret `company_domain`
# (routes the request to the account's data center — recommended by
# Pipedrive for latency, never required for auth) → multi-field
# credential (ADR 0011), resolve_credential_fields, not keyed.
CONNECTOR = _c(
    "pipedrive", ["pipedrive"], auth_modes={"byo_user", "byo_org"},
    secret_kind="fields", label="Pipedrive",
    help="CRM (deals, persons, organizations, activities, notes, leads)",
    href="https://app.pipedrive.com", credential_fields=(
        CredentialField("api_token", "API token", secret=True,
                        help="Pipedrive → Personal preferences → API"),
        CredentialField("company_domain", "Account subdomain",
                        secret=False, required=False,
                        help="acme for acme.pipedrive.com — optional"),
    ),
)

CATEGORY = "CRM"
PUBLISHER = "Pipedrive"
LOGO_DOMAIN = "pipedrive.com"

DESCRIPTION = (
    "The Pipedrive CRM: deals, persons, organizations, activities, notes and "
    "leads. The optional account domain speeds up requests by "
    "routing them to the right data center."
)
