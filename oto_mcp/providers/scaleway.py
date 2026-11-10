"""Registry declaration for the `scaleway` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# scaleway: transactional email via the ORG'S OWN Scaleway TEM account (BYO, like resend).
# The org brings its key (secret_key + project_id); the TEM API only sends from domains
# VERIFIED in the org's Scaleway account → domain ownership guaranteed by Scaleway,
# zero domain logic on the oto side, no more override/activation (normal self-serve connector).
# Config (senders + quiet window) in the email panel of the ORG connector card;
# email_send (spine) routes sender→connector→transport.
CONNECTOR = _c(
    "scaleway", ["scaleway"], auth_modes={"byo_org"}, secret_kind="fields",
           label="Scaleway TEM (email)",
    help="transactional email sending via your Scaleway TEM account (domain verified at Scaleway)",
    publisher="Scaleway", href="https://www.scaleway.com/en/transactional-email-tem/",
    credential_fields=(
        CredentialField("secret_key", "Scaleway secret key (X-Auth-Token)", secret=True),
        CredentialField("project_id", "Project ID Scaleway", secret=False),
        CredentialField("region", "TEM region (default fr-par)", secret=False),
    ),
)

CATEGORY = "Product & data"
LOGO_DOMAIN = "scaleway.com"

DESCRIPTION = (
    "Transactional email sending via your organization's Scaleway TEM account, "
    "from a domain verified at Scaleway. Senders and quiet hours are "
    "configured on the connector card."
)
