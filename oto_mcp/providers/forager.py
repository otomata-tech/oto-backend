"""Registry declaration for the `forager` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# forager : job posts + firmographics + people/contact enrichment, billed
# per credit per lookup. Auth = flat `X-API-KEY` header (a single secret),
# but NOT `secret_kind="api_key"`: every datastorage call also needs an
# integer `account_id` in the path, resolved at runtime via `GET
# /api/users/current/` → `accounts[]` — hence the multi-field model (ADR
# 0011, `secret_kind="fields"`) even though the secret itself is a plain
# bearer, to carry that second (non-secret) field. `account_id` is
# OPTIONAL: `ForagerClient` resolves it on its own if the key only has access
# to one account, and REFUSES (instead of guessing) if it has several —
# guessing could bill the wrong account. BYO (the customer's paid account) or a
# platform key on explicit grant. The platform key serves the ops the instance
# chose (`tools/forager.py::platform_ops`, by default a person's phone numbers);
# every other op asks for the customer's own key. No API key management tool (create/delete) —
# dashboard-only, see tools/forager.py.
CONNECTOR = _c(
    "forager", ["forager"], auth_modes={"byo_user", "byo_org", "platform"},
    default_quota=0, platform_key_open=False, secret_kind="fields",
    label="Forager", help="job posts, firmographics and contact enrichment (pay-per-credit); "
         "the shared key, when granted, covers the lookups this instance chose",
    publisher="Forager.ai", href="https://forager.ai", credential_fields=(
        CredentialField("api_key", "API key (X-API-KEY)", secret=True),
        CredentialField(
            "account_id", "Account ID", secret=False, required=False,
            help="leave empty unless your key has access to several Forager accounts — "
                 "otherwise resolved automatically"),
    ),
)

CATEGORY = "Prospecting"
PUBLISHER = "Forager.ai"
LOGO_DOMAIN = "forager.ai"

DESCRIPTION = (
    "Job postings, company data (firmographics) and contact enrichment from "
    "Forager, billed per credit per search. A key gives access to one or "
    "several Forager accounts; the right account resolves itself "
    "when it only has one. The shared key, when granted to an org, covers "
    "the lookups this instance chose (by default, a person's phone numbers); "
    "every other search needs your own key."
)
