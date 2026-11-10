"""Registry declaration of the `salesforce` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it doesn't
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# salesforce: OAuth2 Connected App → multi-field credential (ADR 0011, like
# zoho), resolved via resolve_credential_fields. byo_user OR byo_org (sales
# team shares a Connected App). No fixed region table: the Salesforce refresh
# returns the `instance_url`, `login_url` only selects prod vs sandbox
# (or a My Domain).
# salesforce: no more hand-set `refresh_token` — the live OAuth flow
# (salesforce_oauth.py) is now the ONLY way to obtain it. The
# form only collects the client_id/client_secret/login_url triplet;
# "the consent remains" is expressed via `status_hints` (register_state +
# pending_action, declared in tools/salesforce.py) — NOT via a separate auth
# method: the `auth_method` set is closed and read by a dashboard switch. The
# `client_id`/`client_secret` remain PER-CUSTOMER (each org creates its
# own Connected App) — no shared Otomata client possible here,
# unlike google (see salesforce_oauth.py).
CONNECTOR = _c(
    "salesforce", ["salesforce"], auth_modes={"byo_user", "byo_org"},
    secret_kind="fields", label="Salesforce",
    help="Salesforce CRM (Contacts, Accounts/companies, Leads, Opportunities, notes)",
    href="https://login.salesforce.com", credential_fields=(
        CredentialField("client_id", "Consumer Key", secret=True,
                        help="Consumer Key of the Connected App"),
        CredentialField("client_secret", "Consumer Secret", secret=True,
                        help="revealed by \"Consumer Details\" on your "
                             "Salesforce application, after email verification"),
        # ⚠️ The label used to say "login.salesforce.com (prod) or test.salesforce.com
        # (sandbox)". That is outdated: My Domain has since become mandatory, and an org that
        # blocks authentication via login.salesforce.com — increasingly the
        # default — makes the consent fail. Experienced on 31/07.
        CredentialField("login_url", "Login URL (your My Domain)",
                        secret=False,
                        help="https://<your-domain>.my.salesforce.com — WITHOUT the "
                             "\"-setup\" of the console domain. Sandbox: "
                             "https://<domain>.sandbox.my.salesforce.com"),
    ),
)

CATEGORY = "CRM"
PUBLISHER = "Salesforce"
LOGO_DOMAIN = "salesforce.com"

DESCRIPTION = (
    "The Salesforce CRM: contacts, accounts (companies), leads, opportunities "
    "and notes. OAuth2 via a Connected App, in a consent flow — no more "
    "refresh token to paste by hand."
)
