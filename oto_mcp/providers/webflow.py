"""Registry declaration of the `webflow` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# webflow: CMS (collections + items), API v2 (developers.webflow.com/data).
# keyed=True, ONE single field (token): a Webflow Site API token is bound to
# ONE site (verified against reference/authentication/site-token — "Site
# tokens are created per site"), so no site_id to enter — the client
# (oto-core) resolves it itself via GET /sites (sites:read scope) on the
# first call, cached. Paste-the-token, like folk/cognism — no
# second field to go and look for in the settings. byo-only (no platform
# key, no commercial agreement Otomata↔Webflow). v1 scope = read/
# write of STAGED (draft) collections/items + explicit publish — no
# pages/assets/forms/ecommerce here.
CONNECTOR = _c(
    "webflow", ["webflow"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key",
    label="Webflow",
    help="CMS — collections & items (site API token)",
    publisher="Webflow", href="https://webflow.com",
    credential_fields=(
        CredentialField("token", "Site API token", secret=True,
                        help="Site Settings → Apps & Integrations → API access — "
                             "generate a token with the scopes cms:read, "
                             "cms:write and sites:read (the latter lets oto "
                             "find the site without you having to copy "
                             "its ID)"),
    ),
)

CATEGORY = "Marketing & SEO"
LOGO_DOMAIN = "webflow.com"

DESCRIPTION = (
    "The Webflow CMS: a site's collections and items, with read, write and "
    "publish. A site token is enough — the target site resolves itself, "
    "no need to specify it."
)
