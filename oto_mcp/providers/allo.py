"""Registry declaration of the `allo` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# allo: business phone system with AI call summaries (withallo.com) — the calls
# and texts of a team, its contacts CRM, the Power Dialer queue, analytics, SMS.
# The client lives in the connector library (`oto.tools.allo.AlloClient`), the
# tools in `tools/allo.py` (the pair `allo` / `allo_act`).
#
# ONE API key per team, created under Settings → API by an Admin or a Manager,
# sent as `Authorization: Api-Key <key>`. Its scopes (conversations read, CRM
# read/write, SMS send…) bound what it reaches: `allo(op="me")` lists them.
#
# Strict BYOK (`byo_user` + `byo_org`, no platform mode): these are the customer
# team's calls, recordings and contacts.
CONNECTOR = _c(
    "allo", ["allo"], auth_modes={"byo_user", "byo_org"},
    keyed=True, secret_kind="api_key",
    credential_fields=(
        CredentialField(
            "key", "API key", secret=True,
            help="Allo web app → Settings → API → create a key (Admin or Manager "
                 "role). Give it the scopes to serve: conversations read for the "
                 "calls, CRM read and write for the contacts, SMS send for texts."),
    ),
    label="Allo",
    help="phone system: calls and texts with AI summaries and transcripts, "
         "contacts CRM, Power Dialer queue, team analytics, SMS",
    href="https://www.withallo.com",
)

CATEGORY = "Calls & meetings"
PUBLISHER = "Allo"
LOGO_DOMAIN = "withallo.com"

DESCRIPTION = (
    "Your team's calls and texts on Allo: search them by keyword, read a call's "
    "AI summary and transcript, get its recording, tag it and add notes. Also the "
    "contacts CRM (people, companies, deals), the Power Dialer queue to load "
    "numbers to call, team and outbound analytics, and sending texts. An API key "
    "created by an Allo admin."
)
