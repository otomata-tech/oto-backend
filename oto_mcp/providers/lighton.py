"""Registry declaration of the `lighton` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# lighton: sovereign document indexing (API v3 api.lighton.ai —
# the Paradigm app and its v2 API are deprecated on the LightOn side):
# hybrid multivector retrieval (search), grounded RAG (ask), parse →
# Markdown, structured extraction, per-workspace ingestion (SharePoint/Drive
# sync possible from the console). 3-field credential (API key
# + optional base URL for private instance + default workspace_id —
# the ADR 0038 instance becomes "one key × one workspace", bindable to a
# project) → secret_kind="fields", resolved via resolve_credential_fields.
# BYO only: the LightOn account belongs to the customer, no platform
# agreement.
CONNECTOR = _c(
    "lighton", ["lighton"], auth_modes={"byo_user", "byo_org"},
    secret_kind="fields",
    label="LightOn",
    help="sovereign document indexing — hybrid search + grounded RAG "
         "+ parse/extract (API v3)",
    href="https://lighton.ai", credential_fields=(
        CredentialField("api_key", "API key", secret=True,
                        help="created on console.lighton.ai"),
        CredentialField("base_url", "Instance URL", secret=False, required=False,
                        help="private instance only (default: SaaS "
                             "https://api.lighton.ai)"),
        CredentialField("workspace_id", "Default workspace", secret=False,
                        required=False,
                        help="id of the LightOn workspace that scopes search/ask/upload "
                             "by default (optional)"),
    ),
)

CATEGORY = "Workspace"
PUBLISHER = "LightOn"
LOGO_DOMAIN = "lighton.ai"

DESCRIPTION = (
    "Sovereign document indexing with LightOn: hybrid multivector search, "
    "grounded question answering over your documents (RAG), "
    "conversion to Markdown, structured extraction, and ingestion per "
    "workspace (SharePoint/Drive synchronization possible from the LightOn "
    "console)."
)
