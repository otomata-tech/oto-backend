"""Registry declaration for the `twilio` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# twilio: programmable voice and SMS — phone numbers (search, buy, configure,
# release), bridged outbound calls and their recordings, SMS and MMS.
#
# **Basic** auth with TWO fields (`account_sid` + `auth_token`), hence
# `secret_kind="fields"` and resolution via `access.resolve_credential_fields`. The
# Auth Token is required rather than an API key: it is also what signs the webhooks
# Twilio sends for the account, which the receiver checks.
#
# `account_sid` is declared NOT secret: it names the account without exposing the
# token (same split as `aircall`).
#
# Two modes, like every connector (Julien, 27/11):
# - `byo_org`: the ORG's own Twilio account; Twilio bills it directly.
# - `platform`: the numbers the org bought from the platform, billed in credits by the
#   instance's commerce, which holds the platform's Twilio account (webhooks, browser
#   phone, regulatory dossiers, monthly rent) and scopes every call to the org's own
#   numbers and to the destinations it prices. oto never holds those Twilio keys: the
#   platform key here is the commerce's PHONE RELAY credential — `account_sid` = the
#   platform's Twilio account SID (informational, never sent to Twilio), `auth_token` =
#   the relay token (`tools/twilio_courtier.py`). It is never handed to `TwilioClient`
#   (`_client()` resolves byo only). OPEN to every org, like Unipile (Julien, 27/11:
#   "anyone with credit can access it"): the commerce cuts ONE org out of credits
#   (`grant:twilio` → `share_denied`), and resolution then refuses. An org reaches only
#   its own numbers through the relay, so an org without any sees nothing.
CONNECTOR = _c(
    "twilio", ["twilio"], auth_modes={"byo_org", "platform"},
    default_quota=0, platform_key_open=True,
    secret_kind="fields",
    credential_fields=(
        CredentialField(
            "account_sid", "Account SID", secret=False,
            help="Twilio Console → Account Info: starts with AC."),
        CredentialField(
            "auth_token", "Auth Token", secret=True,
            help="Twilio Console → Account Info → Auth Token (Show). It also signs "
                 "the webhooks Twilio sends."),
    ),
    label="Twilio",
    help="phone numbers, calls (bridged, recorded) and SMS on your own Twilio account",
    href="https://www.twilio.com",
)

CATEGORY = "Calls & meetings"
PUBLISHER = "Twilio"
LOGO_DOMAIN = "twilio.com"

DESCRIPTION = (
    "Your organization's Twilio account: search and buy phone numbers, place "
    "bridged calls (your phone rings first, then the contact's) that are recorded "
    "and can be transcribed, and send and read SMS. Twilio bills the numbers, "
    "minutes and messages directly to your account."
)
