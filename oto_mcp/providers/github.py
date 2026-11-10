"""Registry declaration of the `github` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# github: repositories and code, issues, pull requests, organizations and Actions.
# Dev category, next to `posthog` and `supabase`.
#
# **Two fields, a single secret.** The token, and a NON-secret `base_url` that
# only exists for GitHub Enterprise Server (`https://<host>/api/v3`) — left
# empty, it is `api.github.com`. It is declared here rather than guessed, because
# an on-premise customer cannot use the connector without it,
# and a second "github enterprise" card would have duplicated everything else.
#
# ⚠️ **What the token can do is NOT readable from this card**: a classic
# token carries *scopes* (`repo`, `read:org`, `workflow`…), a "fine-grained"
# token carries permissions AND a repository list. The connector
# can neither verify them at setup nor widen them — the connection probe
# therefore reports what the token SEES, and that is all we can promise.
#
# ⚠️ Diagnostic trap to know before reading a support ticket: on a
# private resource outside the token's reach, GitHub answers **404, not 403**, on purpose
# so as not to disclose its existence. "Repository not found" therefore almost always
# means "token without the right", not "misspelled name".
#
# Strict BYOK (`byo_user` + `byo_org`, no platform mode): a GitHub token
# carries its holder's identity — every commit, every comment, every
# merge is attributed to THEM. A shared oto key would sign an org's writes
# in the name of an account that is not its own, which makes no sense here.
# Multi-field, hence `secret_kind="fields"` and **not** `keyed`: resolution
# goes through `access.resolve_credential_fields` (pure byo, no platform tier
# or quota), exactly like `posthog` — same shape, a secret plus a non-secret
# instance setting. `keyed=True` would go through `resolve_api_key`, which
# only returns ONE value and would lose the `base_url`.
CONNECTOR = _c(
    "github", ["github"], auth_modes={"byo_user", "byo_org"},
    secret_kind="fields",
    credential_fields=(
        CredentialField(
            "token", "Access token", secret=True,
            help="Personal token (classic or fine-grained), or app "
                 "installation token. Its scopes decide what the "
                 "connector can read and write."),
        CredentialField(
            "base_url", "API URL (Enterprise Server)", secret=False,
            required=False,
            help="Leave EMPTY for github.com. For a self-hosted GitHub "
                 "Enterprise Server: https://<your-host>/api/v3"),
    ),
    label="GitHub",
    help="repositories and code, issues, pull requests, reviews, organizations and "
         "teams, GitHub Actions runs",
    href="https://github.com",
)

CATEGORY = "Product & data"
PUBLISHER = "GitHub"
LOGO_DOMAIN = "github.com"

DESCRIPTION = (
    "The repositories of a GitHub organization or account: code, issues, pull "
    "requests and their reviews, organizations and Actions workflows. Classic "
    "personal token, fine-grained or GitHub App token, each with its "
    "own scopes — what the token can do is not readable from this card. "
    "GitHub Enterprise Server support via a dedicated base URL."
)
