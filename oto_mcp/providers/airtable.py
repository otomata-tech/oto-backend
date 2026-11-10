"""Registry declaration of the `airtable` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# airtable: bases / tables / fields / rows / comments / attachments /
# CSV sync — the whole "Base data" section of the Web API, PLUS the schema, without
# which an agent cannot write a row (it needs the column names and types).
# keyed api_key (Bearer Personal Access Token), **byo-only**: an Airtable PAT
# carries both scopes AND a list of explicitly granted bases —
# a platform key would expose Otomata's bases to every org.
CONNECTOR = _c(
    "airtable", ["airtable"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Airtable",
    help="bases, tables, fields, rows, comments, attachments, CSV sync",
    href="https://airtable.com", credential_fields=(
        CredentialField("key", "Personal Access Token", secret=True,
                        help="airtable.com/create/tokens → create a token, tick "
                             "the scopes data.records:*, data.recordComments:* and "
                             "schema.bases:* THEN add the bases under \"Access\" "
                             "(scopes alone give access to no base)"),
    ),
)

CATEGORY = "Workspace"
PUBLISHER = "Airtable"
DESCRIPTION = (
    "Airtable bases, read AND write: list and filter "
    "rows (formulas, views, sorts), create them, update them or "
    "match them by upsert, comment, attach files. The "
    "schema is exposed too (tables, fields, types and options), so "
    "the agent discovers the columns before writing into them."
)
LOGO_DOMAIN = "airtable.com"
