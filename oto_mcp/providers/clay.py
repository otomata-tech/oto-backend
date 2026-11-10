"""Registry declaration for the `clay` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# ONE card, TWO kinds of named entries (multi-account derived from `fields`):
#   · kind=api   — the Public API key (routines, search, Enterprise tables).
#     PERSONAL key on Clay's side (tied to a user and their credits).
#   · kind=table — the incoming webhook of ONE Clay table ("Monitor webhook" source),
#     the only WRITE path for rows into Clay. As many entries as tables;
#     the entry name is the table name the agent passes to `clay_push_rows`.
# `kind` lives in `meta` (`in_meta`): the tools list the tables and find
# the api entry WITHOUT decrypting. The discriminator hides the other kind's fields
# and `validate_fields` drops them on write.
# No endpoint creates a table webhook: it is copied from the Clay UI. The `webhook`
# field therefore accepts the URL OR the cURL command Clay displays (URL + token in
# a header), re-read by `oto.tools.clay.parse_curl` at resolution.
# byo only, no platform key: each call consumes the customer's Clay credits.
CONNECTOR = _c(
    "clay", ["clay"], auth_modes={"byo_user", "byo_org"}, secret_kind="fields",
    label="Clay",
    help="GTM enrichment — routines, people/companies search, and writing "
         "rows into your Clay tables (webhooks)",
    href="https://www.clay.com",
    account_noun="table",
    field_discriminator="kind",
    credential_fields=(
        CredentialField("kind", "Type", secret=False, choices=("table", "api"),
                        in_meta=True,
                        help="`table` = a Clay table to write rows into (webhook); "
                             "`api` = your Public API key (routines, search)"),
        # `whitespace_significant`: a pasted cURL command LIVES on its spaces —
        # the default cleanup (stripping all whitespace) would make it unreadable.
        CredentialField("webhook", "Webhook URL or cURL command", secret=True,
                        when=("table",), whitespace_significant=True,
                        help="in Clay: + Add → Monitor webhook, then copy the URL "
                             "or the whole cURL command (the auth token is picked up)"),
        CredentialField("auth_token", "Auth token", secret=True, required=False,
                        when=("table",),
                        help="optional — only if you added a token to the webhook "
                             "and pasted the URL alone"),
        CredentialField("api_key", "API key", secret=True, when=("api",),
                        help="Settings → Account → API keys in Clay"),
    ),
)

CATEGORY = "Lead & company data"
PUBLISHER = "Clay"
LOGO_DOMAIN = "clay.com"

DESCRIPTION = (
    "Your Clay tables: add each table by its webhook (paste the cURL command "
    "Clay displays) and oto writes rows into it. With your Clay API key as well: "
    "run your routines (functions, workflows), search Clay's people/"
    "companies database and read your tables (Enterprise). Each call consumes your "
    "Clay credits."
)
