"""Registry declaration of the `boondmanager` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# boondmanager: the CRM/ERP of IT-services and consulting firms. NARROW surface:
# search, read and create of contacts, companies, opportunities and actions —
# no update, no deletion.
#
# `X-Jwt-Client-BoondManager` auth with THREE fields (`secret_kind="fields"`,
# resolved by `access.resolve_credential_fields`): the client signs a JWT per
# call with the `client_key`. `client_token` is declared NON-secret: it identifies
# the Boond account without signing anything. REST API access must be enabled in
# the Boond account, otherwise every call is refused.
#
# Strict BYOK (`byo_user` + `byo_org`): it is the customer's CRM, and every call
# consumes its MONTHLY Boond API quota.
CONNECTOR = _c(
    "boondmanager", ["boondmanager"], auth_modes={"byo_user", "byo_org"},
    secret_kind="fields",
    credential_fields=(
        CredentialField(
            "client_token", "Client token", secret=False,
            help="Boond → administrator interface → dashboard (developer / "
                 "API area)."),
        CredentialField(
            "client_key", "Client key", secret=True,
            help="Same screen as the client token: the key that signs the calls."),
        CredentialField(
            "user_token", "User token", secret=True,
            help="Boond → user settings → security. Calls "
                 "act with this user's rights; REST API access "
                 "must be enabled there."),
    ),
    label="BoondManager",
    help="CRM for IT-services firms: contacts, companies, opportunities, actions — search, "
         "read, create",
    href="https://www.boondmanager.com",
)

CATEGORY = "CRM"
PUBLISHER = "BoondManager"
LOGO_DOMAIN = "boondmanager.com"

DESCRIPTION = (
    "The CRM of an IT-services or consulting firm in BoondManager: search "
    "and read contacts, companies, opportunities and actions, and create them — never "
    "any modification or deletion. Three tokens taken from the Boond account, "
    "which must allow API access."
)
