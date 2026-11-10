"""Registry declaration of the `zohosign` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c
from .zoho import data_center_field

# The fourth Zoho connector: same credential as the CRM and Desk (OAuth2 self-client
# or server-based consent, `auth/zoho.py`), same region field. byo_user OR byo_org: an
# org may share one Zoho Sign account — the requests then go out in that account's name.
# No `zoho_org_id`: Zoho Sign documents no read of the account's organization, so a
# consent could not check it (`auth/zoho.ORG_UNREADABLE`).
CONNECTOR = _c(
    "zohosign", ["zohosign"], auth_modes={"byo_user", "byo_org"}, secret_kind="fields",
    label="Zoho Sign",
    help="Zoho Sign e-signature (signature requests, templates, signed PDFs)",
    href="https://www.zoho.com/sign/", credential_fields=(
        CredentialField("client_id", "Client ID", secret=True,
                        help="1000.XXXXXXXX… (self-client)", text_key="zoho.client_id"),
        CredentialField("client_secret", "Client Secret", secret=True,
                        help="self-client secret", text_key="zoho.client_secret"),
        # OPTIONAL: in "sign in with Zoho" mode (server-based) it is not pasted — the
        # consent flow fills it in. Required only when a self client is set up by hand.
        CredentialField("refresh_token", "Refresh Token", secret=True,
                        required=False,
                        help="1000.xxxxx.yyyyy — leave empty if you sign in via Zoho", text_key="zoho.refresh_token"),
        data_center_field(),
    ),
)

CATEGORY = "Workspace"
PUBLISHER = "Zoho"
LOGO_DOMAIN = "zoho.com"

DESCRIPTION = (
    "Electronic signature in Zoho Sign: follow signature requests and their "
    "recipients, download signed documents and completion certificates, send a "
    "request from a template, remind or recall it."
)

# Access levels — same contract as `providers/zoho.py`. Zoho Sign's reference lists its
# scopes by group (documents, templates, account; CREATE, READ, UPDATE) without mapping
# each endpoint: the table below is the client's reading of the groups
# (`oto.tools.zohosign.client._SCOPE_BY_CALL`). `full` asks for no `ALL` and no DELETE:
# nothing here deletes. `scope_implies` is empty on purpose — no scope listed here
# covers another.
_DOCS_READ, _DOCS_CREATE = "ZohoSign.documents.READ", "ZohoSign.documents.CREATE"
_DOCS_UPDATE, _TEMPLATES_READ = "ZohoSign.documents.UPDATE", "ZohoSign.templates.READ"
ACCESS_LEVELS = {
    "levels": {
        "read": (_DOCS_READ, _TEMPLATES_READ),
        "full": (_DOCS_READ, _DOCS_CREATE, _DOCS_UPDATE, _TEMPLATES_READ),
    },
    "scope_implies": {},
    "tools": {
        "zohosign": {"list": (_DOCS_READ,), "get": (_DOCS_READ,),
                     "download_pdf": (_DOCS_READ,), "download_certificate": (_DOCS_READ,),
                     "list_templates": (_TEMPLATES_READ,),
                     "get_template": (_TEMPLATES_READ,)},
        # Sending reads the template (its recipients and fields) then creates the request.
        "zohosign_act": {"send_from_template": (_TEMPLATES_READ, _DOCS_CREATE),
                         "remind": (_DOCS_UPDATE,), "cancel": (_DOCS_UPDATE,)},
    },
}
