"""Registry declaration of the `zohoanalytics` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c
from .zoho import data_center_field

CONNECTOR = _c(
    "zohoanalytics", ["zohoanalytics"], auth_modes={"byo_user", "byo_org"},
    secret_kind="fields", label="Zoho Analytics",
    help="Zoho Analytics (workspaces, views, export, SQL queries)",
    href="https://analytics.zoho.com", credential_fields=(
        CredentialField("client_id", "Client ID", secret=True, text_key="zoho.client_id"),
        CredentialField("client_secret", "Client Secret", secret=True, text_key="zoho.client_secret"),
        # OPTIONAL: filled in by the "connect with Zoho" flow (server-based).
        CredentialField("refresh_token", "Refresh Token", secret=True,
                        required=False,
                        help="leave empty if you connect via Zoho", text_key="zoho.refresh_token"),
        CredentialField("org_id", "Org ID", secret=False, text_key="zoho.org_id"),
        # OPTIONAL, non-secret: the Zoho organization this app is meant for. Checked at
        # each consent (`auth/zoho.py::check_org`) — without it, any Zoho account can
        # consent to the app, and the consent goes through signaled.
        CredentialField("zoho_org_id", "Expected Zoho organization ID", secret=False,
                        required=False,
                        help="the organization ID shown in Zoho Analytics (Settings → Organization)", text_key="zoho.expected_org_analytics"),
        data_center_field(("sa", "Saudi Arabia (zoho.sa)")),
    ),
)

CATEGORY = "Product & data"
PUBLISHER = "Zoho"
LOGO_DOMAIN = "zoho.com"

DESCRIPTION = (
    "Zoho Analytics dashboards: workspaces, views, data export "
    "and SQL queries on datasets. OAuth2 self-client, like the "
    "other Zoho connectors."
)
