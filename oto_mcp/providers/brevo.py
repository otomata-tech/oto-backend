"""Registry declaration of the `brevo` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# brevo: PUBLIC v3 API (`api.brevo.com/v3`, `api-key` header). One key covers
# the whole account (no scope) → byo. Do NOT confuse with `brevoauto`
# (automations, browser session): disjoint surfaces, distinct credentials.
CONNECTOR = _c(
    "brevo", ["brevo"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Brevo",
    help="emailing & CRM (contacts, lists, transactional, campaigns, deals)",
    publisher="Brevo", href="https://app.brevo.com",
    # 2 modules, 1 namespace: the native CRM is a distinct subdomain, split out
    # to keep file size down. `brevo_crm_*` → namespace_of = `brevo`.
    modules=("brevo", "brevo_crm"),
)

CATEGORY = "Marketing & SEO"
LOGO_DOMAIN = "brevo.com"

DESCRIPTION = (
    "Brevo (formerly Sendinblue) emailing and CRM: contacts, lists, campaigns, "
    "transactional sends and native CRM deals. A single key covers the whole "
    "account — not to be confused with `brevoauto`, which drives automations "
    "through a separate browser session."
)
