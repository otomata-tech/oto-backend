"""Registry declaration of the `silae` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# silae: French payroll. Reads in `silae` + `silae_documents`; the four payroll ENTRIES
# (variable element, bonus, hours, confirmation) in `silae_act`, served only at the `full`
# access level — `read` is the default (ACCESS_LEVELS below). OAuth2 client-credentials auth (Azure AD B2C) = 3
# secrets → generic multi-field model (ADR 0011). NOT keyed (resolved via
# access.resolve_credential_fields, no platform key or quota: byo-only,
# the credential IS the grant). Outside the base set → installable on demand
# (activation gate per org). ⚠️ No server redaction default: JSON responses are in clear
# until the org sets a policy; documents are refused while that policy masks anything
# (tools/silae.py, tools/silae_documents.py). Three modules, one credential.
CONNECTOR = _c(
    "silae", ["silae"], auth_modes={"byo_user"}, secret_kind="fields",
    label="Silae", help="French payroll — Silae Paie API v1: dossiers, employees, payslips, "
                        "payroll reports, DSN; entering variable elements, bonuses and hours",
    href="https://www.silae.fr", modules=("silae", "silae_documents", "silae_act"), credential_fields=(
        CredentialField("client_id", "Client ID", secret=True),
        CredentialField("client_secret", "Client Secret", secret=True),
        CredentialField("subscription_key", "Subscription Key", secret=True),
    ),
)

CATEGORY = "HR & recruiting"
PUBLISHER = "Silae"
LOGO_DOMAIN = "silae.fr"

DESCRIPTION = (
    "A company's payroll in Silae: employees and jobs, payslip headers and lines, "
    "charges table and contributions detail, monthly DSN. At the `full` access level, "
    "agents can also enter variable elements, bonuses and hours for the month and confirm "
    "the entries (Silae's \"Usage interne RH\" contract); the default level only reads. "
    "Nothing is masked by default: set a field-filter policy to redact sensitive fields. "
    "OAuth2 with three secrets, from the Silae API portal."
)

# Access levels (`connectors/access_levels.py`). Silae has no OAuth scopes: its perimeter
# is the subscription key and the contract. The two "scopes" below are ours and only
# split reading from entering: `read` serves every read and leaves `silae_act` out of the
# toolbox; `full` serves the entries too. `read` is the DEFAULT, so a key installed while
# the connector only read keeps reading until an org admin opens `full` — no consent to
# replay (the key carries whatever Silae opened for it).
_READ = ("silae:read",)
_ENTRIES = ("silae:entries",)
ACCESS_LEVELS = {
    "levels": {"read": _READ, "full": _READ + _ENTRIES},
    "default": "read",
    "widening_needs_consent": False,
    "tools": {
        "silae_dossier": {op: _READ for op in ("list", "numbers", "collective_agreements",
                                               "current_period", "establishments",
                                               "organisms")},
        "silae_employee": {op: _READ for op in ("list", "get", "jobs", "from_internal_id")},
        "silae_payslip": {op: _READ for op in ("pdf_ids", "indices", "header", "lines",
                                               "details", "totals")},
        "silae_variables_to_enter": _READ,
        "silae_report": {op: _READ for op in ("generate", "start", "status")},
        "silae_declaration": {op: _READ for op in ("dsn_list", "dsn_content", "states")},
        "silae_act": {op: _ENTRIES for op in ("add_variable_element", "add_bonus",
                                              "add_hours", "set_entries_confirmed")},
    },
}
