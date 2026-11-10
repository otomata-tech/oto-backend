"""Registry declaration of the `signwell` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# signwell: electronic signature — documents sent for signature, templates,
# bulk sends, webhooks (26 operations, full coverage of the public API).
# The client lives in oto-core (`oto.tools.signwell`), the tools in
# `tools/signwell.py` (documents, templates) and `tools/signwell_envois.py` (bulk
# sends, webhooks, account).
#
# **byo-only, by nature**: a SignWell key acts on behalf of the account that created it —
# that name is what appears on the invitations and in the audit trail of the signed
# document. A platform key would send contracts in someone else's name.
#
# ⚠️ SENDS to real people: a sent document, a reminder, a bulk send
# go out by email. The tools create a DRAFT by default, and a bulk
# send is a preview until `dry_run=False` is passed.
CONNECTOR = _c(
    "signwell", ["signwell"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key",
    modules=("signwell", "signwell_envois"),
    label="SignWell",
    help="electronic signature: send a document to sign, track it, retrieve the signed PDF",
    href="https://www.signwell.com",
    credential_fields=(
        CredentialField(
            "key", "SignWell API key", secret=True,
            help="SignWell → Settings → API → \"Create API key\". The key acts on behalf "
                 "of the account that creates it: invitations go out under that name."),
    ),
)

CATEGORY = "Workspace"
PUBLISHER = "SignWell"
LOGO_DOMAIN = "signwell.com"

DESCRIPTION = (
    "Electronic signature with SignWell, from your assistant: send a "
    "contract or an NDA to sign, place the fields, track who has signed, send reminders, "
    "retrieve the signed PDF, work from templates or in bulk sends. "
    "Everyone sets their own key: documents go out in the name of their account."
)
