"""Registry declaration of the `wordpress` connector.

The single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# wordpress: core REST API (`wp/v2`), auth = application password
# (WordPress ≥ 5.6, HTTP Basic over HTTPS) — no plugin to install.
# Three fields, like n8n: the site is self-hosted, there is no single host
# (`site_url` goes through `egress.check_url`). Two ways to set the same
# credential: the form (paste the password) or the "Connect" flow
# (`tools/wordpress.py`, WordPress's native authorization screen,
# `wp-admin/authorize-application.php`) which fills it in without copy-paste.
# Derived multi-account (`fields`): one account = one site — an agency manages ten.
CONNECTOR = _c(
    "wordpress", ["wordpress"], auth_modes={"byo_user", "byo_org"},
    secret_kind="fields", account_noun="site",
    label="WordPress",
    help="posts, pages, media and taxonomies (application password)",
    href="https://wordpress.org",
    credential_fields=(
        CredentialField("site_url", "Site URL", secret=False,
                        help="e.g. https://blog.example.com"),
        CredentialField("username", "WordPress username", secret=False,
                        help="your wp-admin login username"),
        CredentialField("application_password", "Application password",
                        secret=True,
                        help="Users → Profile → Application Passwords "
                             "(not your login password)"),
    ),
)

CATEGORY = "Marketing & SEO"
PUBLISHER = "WordPress"
LOGO_DOMAIN = "wordpress.org"

DESCRIPTION = (
    "Your WordPress site: write and schedule posts, manage pages, "
    "custom content types, media, categories and tags. Everything starts as a "
    "draft, publishing is a separate step."
)
