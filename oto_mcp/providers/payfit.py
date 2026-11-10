"""Registry declaration of the `payfit` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# payfit: payroll and HR, EVERYTHING the Partner API documents, read and write
# (writes wired on 21/10/2026 after being refused since 24/09/2026). An org or a team
# that wants a read-only PayFit sets the `read` access level (ACCESS_LEVELS below).
# keyed api_key (Bearer), BYO only: a PayFit API key is created by a
# company admin and only opens that company — a platform key
# would make no sense, and that is also why a GROUP of companies sets one connector
# instance per company (see `connectors/docs/payfit.md`).
#
# ⚠️ PAYROLL software: NIR, IBAN, pay, health-related absence reasons.
# Since 17/09/2026 (usage signal #1063) the connector no longer REMOVES
# anything hard-coded: everything is served, and protection goes through the **server
# default for field filters** (`field_filter_defaults.SERVER_DEFAULTS`), which
# the org_admin can lift — see `tools/payfit.py`.
#
# Four modules, a single key: the tools of `payfit.py` and its siblings, mounted
# together by `modules` — `payfit_act` carries every write.
CONNECTOR = _c(
    "payfit", ["payfit"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="PayFit",
    help="payroll and HR: company, directory, contracts, absences, payslips, "
         "payroll accounting, worked time, health insurance — and hiring, "
         "contracts, absences, affiliations",
    href="https://payfit.com",
    modules=("payfit", "payfit_paie", "payfit_social", "payfit_act"),
    # PayFit's word for an "account": one key = one COMPANY, and a group sets as many
    # as it has companies. Without this word, an agent stuck on an ambiguity
    # reads "several payfit accounts" and has to translate; with it, it reads "several
    # companies" and knows what it is looking for. Multi-account itself is not declared
    # here: it already applies to every connector whose credential can be set
    # (`Connector.auth_multi_account`).
    account_noun="société",
)

CATEGORY = "HR & recruiting"
PUBLISHER = "PayFit"
LOGO_DOMAIN = "payfit.com"

DESCRIPTION = (
    "HR and financial management of a company run on PayFit: the employee "
    "directory, their contracts (nature, collective agreement, day-based "
    "package, probation, termination), their absences, payslips (metadata and PDF), "
    "payroll accounting entries and their export, the payment file, "
    "the payroll cycle status, actual worked time, meal vouchers, "
    "health insurance (mutuelle) and death and disability cover (prévoyance). "
    "It also writes what the API allows: creating a collaborator and their "
    "contract, recording or cancelling an absence (sick leave included), "
    "affiliating a contract to the health or provident cover. The key's scopes "
    "decide what is allowed, and an org can restrict the connector to reading. "
    "NIR, bank details and absence reason are masked by a server default that "
    "an org administrator can lift."
)

# Access levels (`connectors/access_levels.py`): `read` serves every read and no
# write (`payfit_act` leaves the toolbox), `full` (the default) everything. A PayFit
# key carries its scopes from its creation in PayFit — there is no consent to replay
# here: widening a level does not give a key a write scope it was not created with, a
# new key does. The scopes below
# are PayFit's, as the Partner API documents them per endpoint.
_READS = ("collaborators:read", "contracts:read", "time:read",
          "contracts:payslips:read", "accounting:read", "payment-files:read",
          "health-insurance:read", "collaborators:meal-vouchers:read", "pension:read")
_WRITES = ("collaborators:write", "collaborators:contracts:write", "time:write",
           "health-insurance:write")
ACCESS_LEVELS = {
    "levels": {"read": _READS, "full": _READS + _WRITES},
    "tools": {
        "payfit_company": (),
        # The readers' old write ops only answer `op_moved` — nothing upstream — but
        # they are declared with the write's scope: a `read` org does not even see
        # the pointer to a tool it is not served.
        "payfit_collaborator": {"list": ("collaborators:read",),
                                "get": ("collaborators:read",),
                                "create": ("collaborators:write",)},
        "payfit_contract": {"list": ("contracts:read",), "get": ("contracts:read",),
                            "create": ("collaborators:contracts:write",)},
        "payfit_absence": {"list": ("time:read",), "create": ("time:write",),
                           "cancel": ("time:write",)},
        "payfit_payslip": {"list": ("contracts:payslips:read",),
                           "download": ("contracts:payslips:read",),
                           "overtime": ("contracts:payslips:read",)},
        "payfit_payroll": {"status": (), "accounting": ("accounting:read",),
                           "gross_by_contract": ("accounting:read", "contracts:read"),
                           "accounting_export": ("accounting:read",),
                           "payment_file": ("payment-files:read",)},
        "payfit_worked_time": ("time:read",),
        "payfit_meal_voucher": ("collaborators:meal-vouchers:read",),
        "payfit_insurance": {"list": ("health-insurance:read",),
                             "affiliate": ("health-insurance:write",),
                             "regularize": ("health-insurance:write",)},
        "payfit_document": {"income_tax": ("contracts:payslips:read",),
                            "auto_enrolment": ("pension:read",),
                            "download": ("contracts:payslips:read",)},
        "payfit_act": {"create_collaborator": ("collaborators:write",),
                       "create_contract": ("collaborators:contracts:write",),
                       "create_absence": ("time:write",),
                       "cancel_absence": ("time:write",),
                       "set_affiliation": ("health-insurance:write",),
                       "request_regularization": ("health-insurance:write",)},
    },
}
