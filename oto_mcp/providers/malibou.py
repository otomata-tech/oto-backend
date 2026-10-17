"""Registry declaration of the `malibou` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# malibou: French payroll and HR software for small companies — the employees
# (collaborators) with their contracts, the absences and the declared work
# locations (remote or on site). A neighbour of `payfit` and `sivan` by trade,
# hence the HR category and the same BYO regime. The client lives in the
# connector library (`oto.tools.malibou.MalibouClient`), the tool in
# `tools/malibou.py` (one read-only tool, the read verb in `op`).
#
# TWO fields, hence `secret_kind="fields"` and resolution via
# `access.resolve_credential_fields`: the API key (Bearer) and the organization
# id, which every route carries in its path. Both come from the same admin page
# in Malibou. `organization_id` is declared NOT secret: it designates the
# organization, it authenticates nothing.
#
# The key carries the scopes ticked at creation: without the "details" scope,
# employees come without social security number, address, phone or bank
# account. When it has it, the redaction floor masks NIR and IBAN/BIC
# (`field_filter_defaults.SERVER_DEFAULTS["malibou"]`) until an org_admin lifts it.
#
# Strict BYOK (`byo_user` + `byo_org`, no platform mode): these are the
# customer's personnel data, there can be no shared oto key. Read-only.
CONNECTOR = _c(
    "malibou", ["malibou"], auth_modes={"byo_user", "byo_org"},
    secret_kind="fields", label="Malibou",
    help="payroll and HR, read-only: employees and contracts, absences, work locations",
    href="https://www.malibou.com",
    credential_fields=(
        CredentialField(
            "api_key", "API key", secret=True,
            help="Malibou → Admin → Company → API keys → Create an API key (organization "
                 "admin). Tick the read scopes to grant (employees, employee details, "
                 "contracts, absences, absence types, work locations); the key is shown "
                 "only ONCE."),
        CredentialField(
            "organization_id", "Organization ID", secret=False,
            help="same page, actions menu → Copy organization ID."),
    ),
)

CATEGORY = "HR"
PUBLISHER = "Malibou"
LOGO_DOMAIN = "malibou.com"

DESCRIPTION = (
    "Payroll and HR by Malibou: the organization's employees with their "
    "contracts, the absences and the declared work locations (remote or on "
    "site). Read-only. API key and organization ID, created by an organization "
    "admin."
)
