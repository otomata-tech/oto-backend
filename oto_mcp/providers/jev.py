"""Registry declaration for the `jev` connector (aggregated by `providers/__init__.py`)."""
from __future__ import annotations

from ._model import CredentialField, _c

# Jev: TypeSafe's typed-answer model, served through OpenRouter. Named after the model,
# not the router: going direct to TypeSafe only changes the client's base URL.
#
# ⚠️ SHARED key only, never a personal one: the TENANT's key, or a shared key set at
# the PLATFORM level of the instance (on any instance; the primary tenant of an instance
# never carries a tenant key, its shared keys are platform keys, cf.
# `credentials_store.TENANT`). `byo_org` is declared only because
# `require_credential("tenant", …)` needs it; a closer key (org/team/user) is refused
# at call time (`tools/jev.py::client_partage`). No free tier (`platform_key_open=False`): the
# platform key only serves the orgs it is granted to.
CONNECTOR = _c(
    "jev", ["jev"],
    auth_modes={"byo_org", "platform"}, keyed=True,
    secret_kind="api_key",
    # Mono: one call uses ONE key; two keys on the same rung could not be told apart.
    cardinality="mono",
    platform_key_open=False,
    label="Jev",
    help="Typed answers with probabilities (yes/no, choice, scale). Shared OpenRouter key: "
         "the tenant's, or one set at the instance's platform level.",
    href="https://openrouter.ai/settings/keys",
    # Named field: the key is an OpenRouter key (TypeSafe issues none).
    credential_fields=(
        CredentialField("key", "OpenRouter API key", secret=True,
                        help="Starts with `sk-or-`. Use a dedicated key with a spend cap."),
    ),
)

CATEGORY = "AI & automation"
PUBLISHER = "TypeSafe"
LOGO_DOMAIN = "typesafe.ai"

DESCRIPTION = (
    "Jev answers closed questions about a state you give it: yes/no with a probability, "
    "a choice among options, or a position on a scale. No text, just typed answers. "
    "Built for triaging many rows or profiles with one rubric. The state goes to a third "
    "party (OpenRouter, then TypeSafe): send only the fields the judgement needs. Runs on "
    "the tenant's key, or a shared key set at the instance's platform level."
)
