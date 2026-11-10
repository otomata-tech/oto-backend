"""Registry declaration of the `threecx` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# threecx: 3CX phone system (v20): call log and recordings (`threecx`),
# configuration read (`threecx_config`) and written (`threecx_act`). Multi-field
# (ADR 0011), byo-only: the address of the phone system (`base_url`, a DESTINATION →
# egress guard in `tools/threecx.py`) + ONE of the two credentials, chosen by
# `auth_mode` (`field_discriminator`) — an API client (`client_id`/`client_secret`,
# which 3CX reserves for certain licenses) or a user account (`username`/`password`).
# `when=` makes each pair required in ITS mode only: setup refuses an incomplete pair.
AUTH_MODES = ("api_client", "user")

CONNECTOR = _c(
    "threecx", ["threecx"], auth_modes={"byo_user", "byo_org"}, secret_kind="fields",
    label="3CX", help="telephony: call log, recordings and configuration",
    modules=("threecx", "threecx_config", "threecx_act"),
    href="https://www.3cx.com", field_discriminator="auth_mode", credential_fields=(
        CredentialField(
            "base_url", "Phone system address", secret=False,
            help="The https address of the 3CX web client, e.g. https://your-company.3cx.fr"),
        CredentialField(
            "auth_mode", "Access", secret=False, choices=AUTH_MODES,
            help="api_client: an API client from the 3CX admin console "
                 "(Integrations > API). user: a 3CX account."),
        CredentialField(
            "client_id", "Client ID", secret=False, when=("api_client",),
            help="The API client identifier; its role bounds what is visible "
                 "(System Admin for the whole phone system)."),
        CredentialField(
            "client_secret", "API key", secret=True, when=("api_client",)),
        CredentialField(
            "username", "3CX account username", secret=False, when=("user",),
            help="This account's rights bound what is visible; two-factor "
                 "authentication must be disabled on it."),
        CredentialField(
            "password", "3CX account password", secret=True, when=("user",),
            whitespace_significant=True),
    ),
)

CATEGORY = "Calls & meetings"
PUBLISHER = "3CX"
LOGO_DOMAIN = "3cx.com"

DESCRIPTION = (
    "The call log of a 3CX phone system (caller, callee, duration, status), the "
    "audio of its recordings, and its configuration: extensions, queues, IVRs, "
    "inbound rules, office hours, forwarding — read, and changed with a preview "
    "first. Access through an API client or a 3CX account: its rights bound what "
    "is visible and what can change."
)
