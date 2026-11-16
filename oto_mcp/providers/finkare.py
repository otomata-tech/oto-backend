"""Registry declaration for the `finkare` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# finkare: AI-driven receivables collection (reminders, voice agents,
# formal notice). REST API v1 documented at docs.finkare.io — invoices,
# debtors, payments, and the dunning workflow.
#
# ⚠️ **The key carries its environment**: `fk_test_…` targets the sandbox,
# `fk_live_…` production. The client DERIVES its base URL from it rather than
# taking it as a parameter — so a test key cannot reach prod
# by mistake, and nobody can pair a live key with a sandbox URL.
#
# ⚠️ **No platform key**: each org sets its own (BYO). A Finkare
# key gives access to the receivables of ONE company — pooling it would make
# no sense, and the cascade must stop at the org tier.
# ⚠️ **Their documentation announces a hosted MCP server that DOES NOT EXIST.**
# `docs.finkare.io/mcp-server.md` describes `https://mcp.finkare.io/mcp` (OAuth 2.1 +
# PKCE + dynamic registration, 21 tools) — checked on 2026-09-02: **no DNS
# record**, the host does not resolve. If that server existed, this connector
# would have no reason to be: we would reach it through the generic `api` connector
# (ADR 0069 — MCP federation, for its part, has been removed since 2026-09-09).
#
# The note is here, and not in someone's head: a service that announces a nonexistent door
# will make every agent that reads its docs believe it. ⚠️ **And the day it answered,
# that would NOT be a reason to federate it**: that is precisely what ADR 0069
# removed. The classic connector remains the way.
#
# ⚠️ Their public `openapi.json` is NOT usable either: it is Mintlify's
# example template ("OpenAPI Plant Store", two paths on `/plants`). The
# client methods come from the reference pages, read one by one.

# ⚠️ **NEVER EXERCISED AGAINST THE REAL SERVICE** — state as of 2026-09-02, and nobody
# asked otherwise (chasing for a trial key was explicitly
# dropped). What IS verified: the four tools mount, each method
# matches the reference documentation read endpoint by endpoint, and an
# `fk_test_` key cannot reach real receivables. What is NOT: the real
# shape of responses, error codes, pagination.
#
# The note is here so a first user knows what to expect — a shipped connector
# reads like a proven connector, and nothing tells the two apart in use
# until the first call has happened.

CONNECTOR = _c(
    "finkare", ["finkare"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key",
    label="Finkare",
    help="receivables collection: invoices, debtors, payments, dunning",
    href="https://app.finkare.io",
)

CATEGORY = "Finance"
# THIRD-PARTY publisher, named as the third-party connector regime requires: this
# service is not operated by Otomata, and the catalog must not let anyone
# believe otherwise.
PUBLISHER = "Finkare"
DESCRIPTION = ("AI-automated receivables collection — import invoices, "
               "track payments, steer dunning and read a debtor's "
               "payment score.")
LOGO_DOMAIN = "finkare.io"
