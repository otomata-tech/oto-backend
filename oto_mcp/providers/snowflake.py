"""Registry declaration of the `snowflake` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _decrit

# snowflake: SQL on the customer's OWN Snowflake account (SQL API v2). `snowflake`
# reads — SELECT / WITH / SHOW / DESCRIBE / EXPLAIN, one statement per call, values
# in bindings; a statement still running after the API's 45 s wait, or a further
# partition of a result, is read by its handle. `snowflake_act` acts —
# `execute_statement` (any one statement: DML, DDL, COPY, CALL; effect `destroy`,
# always async so its answer is the handle) and `cancel_statement`. Their `confirm`
# is declared for oto 2; oto 1 has no confirmation step on a direct call today
# (`tools/effets.py`: nothing refuses on the class yet; recipes refuse `detruire`):
# the gates are the MCP destructive annotation and the org's switch on the tool. A neighbour of `bigquery` by trade
# (data warehouse). Served by the GENERIC READER of its description
# (`tools/decrits.py`): no `tools/snowflake.py`, the library's engine runs the
# description (auth `bearer`, the token-type header a constant of the description).
#
# TWO fields, read in the description: the programmatic access token (secret) and
# the account identifier (`myorg-myaccount`), a `text` SETTING cited by `base_url`
# — hence `secret_kind="fields"`, both kept as one record and resolved by
# `access.resolve_credential_fields`. `byo_user` and `byo_org`, no platform key:
# the data is the customer's own.
#
# ⚠️ The SQL API has NO read-only mode: on the read side the input pattern refuses
# what does not open like a read, but the real boundary is the token's role. The
# help asks for a token made with ROLE_RESTRICTION on a role that holds only what
# the agent may read and write, never an administration role. A statement that
# names PROGRAMMATIC ACCESS TOKEN is refused on the act side: its secret would come
# back in the result set. A PAT is refused (401) when its user is under
# no network policy allowing our egress addresses: the help says it.
#
# A query bills the customer's warehouse credits. 5xx is not retryable: a
# statement may have run.
CONNECTOR = _decrit(
    "snowflake",
    help="SQL on your Snowflake account: queries, SHOW and DESCRIBE, and confirmed writes (DML, DDL, COPY)",
    href="https://www.snowflake.com",
    key_help="Snowflake → Snowsight → your user menu → Settings → Authentication → Programmatic access "
             "tokens → Generate new token, restricted to a role that holds ONLY what the agent may read and "
             "write (USAGE on a warehouse, SELECT, and INSERT/UPDATE/CREATE where it may act; never an admin "
             "role): the role is the real boundary, Snowflake has no read-only token. "
             "The secret is shown once. The user must be under a network policy that allows oto, or the "
             "token is refused.",
    settings_help={
        "account": "Your account identifier in the org-account form, as in "
                   "https://<myorg-myaccount>.snowflakecomputing.com (Snowsight → account menu → "
                   "View account details → Account identifier, with a dash instead of the dot).",
    },
)

CATEGORY = "Product & data"
PUBLISHER = "Snowflake"
LOGO_DOMAIN = "snowflake.com"

DESCRIPTION = (
    "Your Snowflake data warehouse: run SELECT, SHOW and DESCRIBE queries on the warehouse, role, "
    "database and schema of your choice, read long results by their handle, and, once confirmed, "
    "execute statements that write (INSERT, UPDATE, MERGE, CREATE, COPY) or cancel a running one. "
    "A programmatic access token restricted to the role the agent may use, and your account "
    "identifier. Queries bill your warehouse credits."
)
