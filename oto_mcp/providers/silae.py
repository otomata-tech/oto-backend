"""Registry declaration of the `silae` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# silae: French payroll, READ-ONLY. OAuth2 client-credentials auth (Azure AD B2C) = 3
# secrets → generic multi-field model (ADR 0011). NOT keyed (resolved via
# access.resolve_credential_fields, no platform key or quota: byo-only,
# the credential IS the grant). Outside the base set → installable on demand
# (activation gate per org). ⚠️ No server redaction default: JSON responses are in clear
# until the org sets a policy; documents are refused while that policy masks anything
# (tools/silae.py, tools/silae_documents.py). Two modules, one credential.
CONNECTOR = _c(
    "silae", ["silae"], auth_modes={"byo_user"}, secret_kind="fields",
    label="Silae", help="French payroll (read) — Silae Paie API v1: dossiers, employees, "
                        "payslips, payroll reports, DSN",
    href="https://www.silae.fr", modules=("silae", "silae_documents"), credential_fields=(
        CredentialField("client_id", "Client ID", secret=True),
        CredentialField("client_secret", "Client Secret", secret=True),
        CredentialField("subscription_key", "Subscription Key", secret=True),
    ),
)

CATEGORY = "HR & recruiting"
PUBLISHER = "Silae"
LOGO_DOMAIN = "silae.fr"

DESCRIPTION = (
    "A company's payroll in Silae (read): employees and jobs, payslip headers and lines, "
    "charges table and contributions detail, monthly DSN. Nothing is masked by default: "
    "set a field-filter policy to redact sensitive fields. OAuth2 with three secrets, "
    "from the Silae API portal."
)
