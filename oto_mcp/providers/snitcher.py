"""Registry declaration of the `snitcher` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# snitcher: website visitor identification — which COMPANIES
# visit, with sessions/events per visit (including submitted form
# values), contacts (email reveal = paid credit), segments,
# tags and custom fields. keyed api_key (Bearer Personal Access Token,
# dashboard → Settings → Account → API), byo-only: a PAT is bound to ONE
# Snitcher account, a platform key would make no sense.
CONNECTOR = _c(
    "snitcher", ["snitcher"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Snitcher",
    help="identify the companies visiting your site: "
         "organisations, sessions, contacts, segments, tags, custom fields",
    href="https://snitcher.com", credential_fields=(
        CredentialField("key", "Personal Access Token", secret=True,
                        help="Snitcher → Settings → Account → API → "
                             "Generate New Token"),
    ),
)

CATEGORY = "Lead & company data"
PUBLISHER = "Snitcher"
LOGO_DOMAIN = "snitcher.com"

DESCRIPTION = (
    "Identify the companies visiting your website, with their sessions "
    "and visit events (including submitted form values): "
    "organisations, contacts (email reveal by credit), segments, tags and custom "
    "fields."
)
