"""Registry declaration of the `yousign` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# byo (user OR org), resolved via `resolve_credential(want="byo")` like forager:
# everyone connects THEIR OWN Yousign key — NO shared platform key (a key
# acts on behalf of the account that created it). Two fields: the key, and the environment.
# Yousign has TWO HOSTS (sandbox, production) and a key from one is rejected by
# the other: the environment is DECLARED at setup, it is not guessed. Really
# writes (sends a signature request to third parties): unlike
# gocardless (read-only), activation notifies real people.
CONNECTOR = _c(
    "yousign", ["yousign"], availability="self_serve",
    auth_modes={"byo_user", "byo_org"}, secret_kind="fields",
    label="Yousign", help="electronic signature (requests, status, signed document)",
    credential_fields=(
        CredentialField(
            "key", "Yousign API key", secret=True,
            help="Yousign → Developers → API keys. The key acts on behalf of the account "
                 "that creates it: invitations go out under that name."),
        CredentialField(
            "environment", "Environment", secret=False, required=False,
            choices=("production", "sandbox"),
            help="\"sandbox\" for a Yousign sandbox key; empty or "
                 "\"production\" otherwise. A key from one environment is rejected "
                 "by the other."),
    ),
)

CATEGORY = "Workspace"
PUBLISHER = "Yousign"
LOGO_DOMAIN = "yousign.com"

DESCRIPTION = (
    "Electronic signature: create a signature request from a "
    "PDF with its signers, activate it (sends the invitations), track its "
    "status and retrieve the signed document. Everyone connects their own "
    "Yousign key, in production or in the sandbox (to be specified at setup) — no "
    "shared platform key."
)
