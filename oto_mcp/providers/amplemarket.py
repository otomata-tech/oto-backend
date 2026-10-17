"""Registry declaration of the `amplemarket` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# amplemarket: B2B prospecting and outbound — people and company search,
# enrichment (single and batch), email validation, lead lists, sequences, tasks,
# calls, mailboxes and exclusion lists. A neighbour of `apollo` and `lemlist` by
# trade. The client lives in the connector library
# (`oto.tools.amplemarket.AmplemarketClient`), the tools in `tools/amplemarket.py`
# (the pair `amplemarket` reads, `amplemarket_act` acts).
#
# keyed `api_key`: one workspace key, sent as `Authorization: Bearer`.
#
# Strict BYOK (`byo_user` + `byo_org`, no platform mode): the searches,
# enrichments and sequences run on the customer's own Amplemarket seat and
# credits, there can be no shared oto key.
CONNECTOR = _c(
    "amplemarket", ["amplemarket"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Amplemarket",
    help="B2B prospecting: people and company search, enrichment, lead lists, sequences",
    href="https://www.amplemarket.com",
    credential_fields=(
        CredentialField(
            "key", "Amplemarket API key", secret=True,
            help="an Amplemarket admin creates it under Settings > API; it is sent "
                 "as `Authorization: Bearer`. The key acts for the whole workspace."),
    ),
)

CATEGORY = "Prospecting"
PUBLISHER = "Amplemarket"
LOGO_DOMAIN = "amplemarket.com"

DESCRIPTION = (
    "B2B prospecting and outbound with Amplemarket: search people and companies, "
    "enrich them (emails, phones), validate emails, build lead lists, enroll leads "
    "in sequences, work tasks and calls, manage exclusion lists. Consumes the "
    "credits of the connected account. Workspace API key, created by an admin."
)
