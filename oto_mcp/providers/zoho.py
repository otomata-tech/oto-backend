"""Registry declaration for the `zoho` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# The Zoho regions, as a person recognizes them: the end of their Zoho address. One home,
# shared by the key's field (the four connectors) and the consent's select (`tools/zoho.py`).
REGIONS = (
    ("eu", "Europe (zoho.eu)"), ("com", "International (zoho.com)"),
    ("in", "India (zoho.in)"), ("au", "Australia (zoho.com.au)"),
    ("jp", "Japan (zoho.jp)"), ("ca", "Canada (zohocloud.ca)"),
)


def data_center_field(*extra_regions: tuple[str, str]) -> CredentialField:
    """The key's region: a closed list with no preselected value. Free text with `eu` as
    its help was copied as is by someone whose account is on zoho.com, and the wrong
    region then bound every consent (`auth/zoho.consent_region`)."""
    regions = REGIONS + extra_regions
    return CredentialField(
        "data_center", "Data center (region of your Zoho account)", secret=False,
        choices=tuple(value for value, _ in regions), choice_labels=regions,
        help="look at your Zoho address: zoho.eu is Europe, zoho.com is International",
        text_key="zoho.data_center")


# zoho / zohodesk: OAuth2 self-client → multi-field credential (ADR 0011,
# like silae), resolved via resolve_credential_fields. byo_user OR byo_org
# (zoho: shareable org/group key — a sales team shares one self-client).
# `data_center` (non-secret) selects the Zoho region (com/eu/in…).
CONNECTOR = _c(
    "zoho", ["zoho"], auth_modes={"byo_user", "byo_org"}, secret_kind="fields",
    label="Zoho CRM", account_noun="organisation",
    # No `cardinality`: `fields` derives it as multi since oto-backend#409.
    # Only the STATIC announcement of the axis remains curated — a Zoho user in
    # practice has several organizations, the axis is worth having in the schema upfront.
    account_axis_static=True,
    help="Zoho CRM (module CRUD, notes)", href="https://crm.zoho.com",
    credential_fields=(
        CredentialField("client_id", "Client ID", secret=True,
                        help="1000.XXXXXXXX… (self-client)", text_key="zoho.client_id"),
        CredentialField("client_secret", "Client Secret", secret=True,
                        help="self-client secret", text_key="zoho.client_secret"),
        # OPTIONAL: in "connect with Zoho" mode (server-based) it is not
        # pasted — the consent flow fills it in. Required only when setting up
        # a self client by hand.
        CredentialField("refresh_token", "Refresh Token", secret=True,
                        required=False,
                        help="1000.xxxxx.yyyyy — leave empty if you connect via Zoho", text_key="zoho.refresh_token"),
        # OPTIONAL, non-secret: the Zoho organization this app is meant for. Checked at
        # each consent (`auth/zoho.py::check_org`) — without it, any Zoho account can
        # consent to the app, and the consent goes through signaled.
        CredentialField("zoho_org_id", "Expected Zoho organization ID", secret=False,
                        required=False,
                        help="the Org ID shown in Zoho CRM (Setup → Company Details)", text_key="zoho.expected_org_crm"),
        data_center_field(),
    ),
)

CATEGORY = "CRM"
PUBLISHER = "Zoho"
LOGO_DOMAIN = "zoho.com"

DESCRIPTION = (
    "The Zoho CRM: create, read, update modules and add notes. "
    "OAuth2 self-client, shareable by a whole team; the Zoho region "
    "(com/eu/in…) is chosen at connection time."
)

# Access levels (connectors repo, `docs/conception/niveaux-d-acces.md`) — DATA, read and
# validated by `connectors.access_levels`. `full` is the scope list the consent asked
# for before levels existed, byte for byte: at `full` nothing is filtered. A tool (or an
# `op` of a multiplexed tool) declares the scopes it REQUIRES; it is served at a level
# when each of them is in the closure of the level's scopes by `scope_implies` — an
# implication the provider documents, never a rule guessed from the scope names.
_READ, _CREATE = "ZohoCRM.modules.READ", "ZohoCRM.modules.CREATE"
_UPDATE, _DELETE = "ZohoCRM.modules.UPDATE", "ZohoCRM.modules.DELETE"
_SETTINGS_MODULES = "ZohoCRM.settings.modules.READ"
ACCESS_LEVELS = {
    "levels": {
        "read": (_READ, _SETTINGS_MODULES),
        "full": ("ZohoCRM.modules.ALL", _SETTINGS_MODULES,
                 "ZohoCRM.settings.fields.READ", "ZohoCRM.users.READ"),
    },
    "scope_implies": {"ZohoCRM.modules.ALL": (_READ, _CREATE, _UPDATE, _DELETE)},
    "tools": {
        "zoho_modules": (_SETTINGS_MODULES,),
        "zoho_record": {"list": (_READ,), "get": (_READ,), "search": (_READ,),
                        "create": (_CREATE,), "update": (_UPDATE,), "delete": (_DELETE,)},
        "zoho_note": {"list": (_READ,), "create": (_CREATE,)},
        # The consent verb itself requires nothing: it is how a level gets its token.
        "zoho_connect": (),
    },
}
