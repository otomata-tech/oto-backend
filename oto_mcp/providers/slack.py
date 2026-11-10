"""Registry declaration for the `slack` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# slack: messaging. BYO 100% configurable per org/user (#25) — MULTI-FIELD
# credential (bot token xoxb- AND/OR user token xoxp-, at least one required),
# resolved via resolve_credential_fields (silae/zoho model, NOT keyed). byo_user
# OR byo_org (a workspace shared by the org = its bot token). Read fallback
# for the legacy credential (single pre-multi-field token) in tools/slack.py.
# MULTI-WORKSPACE (#409): one vault account = one workspace, since a Slack
# token is issued per installation of the app in a workspace. Chosen per call
# via `_account=`; the `oto.tools.slack` lib can already serve N workspaces.
CONNECTOR = _c(
    "slack", ["slack"], auth_modes={"byo_user", "byo_org"}, secret_kind="fields",
    personal_session=False, label="Slack", account_noun="workspace",
    help="Slack messaging (bot token xoxb- and/or user token xoxp-)",
    href="https://slack.com", credential_fields=(
        CredentialField("bot_token", "Bot token (xoxb-)", secret=True,
                        required=False),
        CredentialField("user_token", "User token (xoxp-)", secret=True,
                        required=False),
    ),
)

CATEGORY = "Communication"
PUBLISHER = "Slack"
LOGO_DOMAIN = "slack.com"

DESCRIPTION = (
    "The Slack messaging of one or more workspaces: channels, messages, "
    "threads. A bot token (xoxb-) and/or a user token (xoxp-) — "
    "at least one of the two; each connected workspace becomes a distinct "
    "account in the vault."
)
