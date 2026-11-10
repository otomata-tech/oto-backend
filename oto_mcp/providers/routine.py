"""Registry declaration of the `routine` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# routine: trigger a Claude Code ROUTINE (autonomous agent hosted at
# Anthropic) through its `/fire` endpoint. oto does not run the agent — it
# triggers it, and the agent comes back on `/mcp` with its account's tools.
#
# ONE INSTANCE = ONE ROUTINE (ADR 0038 B5, same pattern as "one key × one
# workspace" at lighton): the `/fire` token is scoped by Anthropic to ONE
# routine, so one automation = one instance, revocable on its own and bindable to
# a project. A single token that triggered a catch-all routine
# would lose exactly the security notch that makes this path interesting.
#
# byo only: the routine belongs to a claude.ai account (it is not an org
# object on the Anthropic side, and what it does appears under that identity). No
# platform key: there is nothing to pool, each automation has its token.
CONNECTOR = _c(
    "routine", ["routine"], auth_modes={"byo_user", "byo_org"},
    secret_kind="fields",
    label="Claude Code Routine",
    help="triggers a Claude Code routine (autonomous agent) — one instance per "
         "automation",
    publisher="Anthropic", href="https://claude.ai/code/routines",
    credential_fields=(
        CredentialField(
            "routine_id", "Routine ID", secret=False,
            help="visible in the routine's URL on claude.ai/code/routines "
                 "(starts with `trig_`)"),
        CredentialField(
            "token", "Trigger token", secret=True,
            help="generated in the routine's API trigger — shown ONCE, "
                 "not retrievable afterwards"),
    ),
)

CATEGORY = "AI & automation"
LOGO_DOMAIN = "anthropic.com"

DESCRIPTION = (
    "Trigger a Claude Code routine (autonomous agent hosted at Anthropic) "
    "via its `/fire` endpoint: oto does not run the agent, it launches it — "
    "the agent then comes back with your own account's tools. One instance "
    "= one routine, revocable on its own."
)
