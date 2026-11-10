"""Registry declaration of the `zeendoc` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# zeendoc: French document management (GED) — binders of indexed documents
# (supplier and customer invoices, expense reports…), searched by their indexing
# fields, each with its extracted text and its PDF. The client lives in the
# connector library (`oto.tools.zeendoc.ZeendocClient`), the tool in
# `tools/zeendoc.py` (one read-only tool, the read verb in `op`).
#
# THREE fields, hence `secret_kind="fields"` and resolution via
# `access.resolve_credential_fields`: Zeendoc has no API key, an account is a
# Zeendoc USER (login and password, the web application's) and its cabinet
# (`url_client`, the id in armoires.zeendoc.com/<url_client>/). The user's rights
# bound every read, hence a dedicated user with read rights on the binders to
# serve. `url_client` is a path segment on a fixed host, validated by the client:
# no field designates a destination, no egress guard to set.
#
# Strict BYO (`byo_user` + `byo_org`, no platform mode): these are the customer's
# documents. Read-only: no upload, no index update, no deletion.
CONNECTOR = _c(
    "zeendoc", ["zeendoc"], auth_modes={"byo_user", "byo_org"},
    secret_kind="fields", label="Zeendoc",
    help="document management (GED), read-only: binders, search by index, text and PDF",
    href="https://www.zeendoc.com",
    credential_fields=(
        CredentialField(
            "login", "Zeendoc user login", secret=False,
            help="The email a Zeendoc user signs in with — preferably a user dedicated "
                 "to oto, whose rights bound what is visible (read on the binders to "
                 "serve is enough)."),
        CredentialField(
            "password", "Zeendoc user password", secret=True,
            whitespace_significant=True),
        CredentialField(
            "url_client", "Cabinet id (Url_Client)", secret=False,
            help="The segment after armoires.zeendoc.com/ in the address of the "
                 "cabinet, e.g. my_company in armoires.zeendoc.com/my_company/."),
    ),
)

CATEGORY = "Workspace"
PUBLISHER = "Zeendoc"
LOGO_DOMAIN = "zeendoc.com"

DESCRIPTION = (
    "Documents stored in Zeendoc (GED): the binders a user can open and their "
    "indexing fields, search and count by those fields or by full text, and each "
    "document's index values, extracted text and PDF. Read-only. A Zeendoc user's "
    "login and password and the cabinet id: that user's rights bound what is "
    "visible."
)
