"""Registry declaration of the `n8n` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# Connectors to third-party automation platforms. byo, outside the base set
# (opt-in, installable from the library), no platform key (each
# sets their own). Inert until activated in DB (deny-by-default, like hubspot).
# n8n / make: 2-field credential (key + instance/zone base URL —
# self-hosting & regionalization require their own URL) → secret_kind="fields",
# resolved via resolve_credential_fields. zapier: simple key (AI Actions API),
# keyed → resolve_api_key.
CONNECTOR = _c(
    "n8n", ["n8n"], auth_modes={"byo_user", "byo_org"}, secret_kind="fields",
    label="n8n",
    help="workflow automation — workflows + executions (public API)",
    href="https://n8n.io", credential_fields=(
        CredentialField("api_key", "API key", secret=True),
        CredentialField("base_url", "Instance URL", secret=False,
                        help="e.g. https://acme.app.n8n.cloud"),
    ),
)

CATEGORY = "AI & automation"
PUBLISHER = "n8n"
LOGO_DOMAIN = "n8n.io"

DESCRIPTION = (
    "n8n workflows: list, trigger and track the execution of your instance's "
    "workflows (cloud or self-hosted), via the public API. Two fields: "
    "the API key and your instance's URL — n8n is self-hosted, there is no "
    "single endpoint."
)
