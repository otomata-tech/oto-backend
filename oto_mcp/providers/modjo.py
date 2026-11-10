"""Registry declaration of the `modjo` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# modjo: conversation intelligence for sales teams — recorded calls, transcripts,
# AI summaries, notes and next steps, call reviews, the CRM objects Modjo mirrors
# (accounts, contacts, deals), and a question to Modjo's AI. Same family as
# `claap`, `leexi`, `aircall`.
# The client lives in the connector library (`oto.tools.modjo`), the tools in
# `tools/modjo.py` (calls) and `tools/modjo_annuaire.py` (reviews, CRM objects,
# workspace, AI question); the shared base in `tools/modjo_socle.py`.
#
# keyed `api_key`, sent as `Authorization: Bearer`. ⚠️ It is a WORKSPACE key: it
# sees every member's calls, not only those of the person who created it.
#
# **byo-only** (`byo_user` + `byo_org`, no platform mode): these are the
# customer's recorded calls, there can be no shared oto key.
#
# ⚠️ The import of a call cannot be undone through the API and is logged in the
# connected CRM: the tool returns a PREVIEW until `confirm=True`.
CONNECTOR = _c(
    "modjo", ["modjo"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key",
    modules=("modjo", "modjo_annuaire"),
    label="Modjo",
    help="recorded sales calls, transcripts, AI summaries and next steps, call "
         "reviews, mirrored CRM deals",
    href="https://www.modjo.ai",
    credential_fields=(
        CredentialField(
            "key", "Modjo Public API key", secret=True,
            help="Modjo → Settings → Integrations → \"Public API\" → create a key "
                 "(admin or manager account). The key belongs to the workspace: "
                 "it reads every member's calls."),
    ),
)

CATEGORY = "Calls & meetings"
PUBLISHER = "Modjo"
LOGO_DOMAIN = "modjo.ai"

DESCRIPTION = (
    "Sales calls recorded by Modjo: transcripts, AI summaries, notes and next "
    "steps, call reviews, the deals and accounts mirrored from your CRM, and "
    "questions to Modjo's AI about a call or a deal — same family as Claap and "
    "Leexi. Workspace API key, created by an admin or a manager."
)
