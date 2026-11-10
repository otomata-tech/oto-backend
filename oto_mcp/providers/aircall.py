"""Registry declaration for the `aircall` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# aircall: cloud telephony, READ-ONLY — calls (recording,
# voicemail), a call's conversational AI, users, teams,
# numbers, contacts.
#
# **Basic** auth with TWO fields (`api_id` + `api_token`), hence `secret_kind="fields"`
# and resolution via `access.resolve_credential_fields`. A key is created in the
# Aircall Dashboard (Company Settings → API Keys) by an admin account, and gives
# access to the whole company: no finer scope on Aircall's side.
#
# `api_id` is declared NOT secret: it identifies the key that was set without exposing the
# token that goes with it (same split as `leexi` between its KEY_ID and its secret).
#
# Strict BYOK (`byo_user` + `byo_org`, no platform mode): these are the calls
# and recordings of the customer company.
CONNECTOR = _c(
    "aircall", ["aircall"], auth_modes={"byo_user", "byo_org"},
    secret_kind="fields",
    credential_fields=(
        CredentialField(
            "api_id", "API ID", secret=False,
            help="Aircall Dashboard → Company Settings → API Keys → Add a new "
                 "API key (admin account required)."),
        CredentialField(
            "api_token", "API Token", secret=True,
            help="Shown only ONCE when the key is created: Aircall does not "
                 "keep it in clear text."),
    ),
    label="Aircall",
    help="telephony, read-only: calls, recordings, AI transcriptions and "
         "summaries, users, numbers, contacts",
    href="https://aircall.io",
)

CATEGORY = "Calls & meetings"
PUBLISHER = "Aircall"
LOGO_DOMAIN = "aircall.io"

DESCRIPTION = (
    "A company's calls on Aircall: log, search, recordings "
    "and voicemails, and — with Aircall's AI offer — transcription, "
    "summary, topics, sentiment and action items. Also the users, "
    "teams, numbers and shared contacts. Read-only; API key "
    "created by an Aircall admin."
)
