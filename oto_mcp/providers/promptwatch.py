"""Registry declaration of the `promptwatch` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it doesn't
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# promptwatch: AI visibility monitoring (how a brand appears in
# ChatGPT/Claude/Gemini… answers) — prompts organized into monitors,
# visibility/sentiment/citations analytics, AI-generated content to
# close coverage gaps. Synchronous REST client in oto-core
# (`oto.tools.promptwatch`), curated tools in `tools/promptwatch.py` (10
# `op=` tools, ADR 0047 — the v1 scope covers projects/monitors/prompts
# (+ native bulk)/responses/visibility/citations/content+content-gap/
# tags+topics/personas/brands; Publishing, Content Agent, Ads Radar,
# Shopping, Site Health, Sitemap, Page Tracker, Models, Actions, Query
# Fanouts and Social Citations are DEFERRED, not built). 2-field credential
# (API key + optional project_id — only used by an
# ORG-level key targeting a specific project, a project-level key ignores it) →
# secret_kind="fields", resolved via resolve_credential_fields, same pattern
# as lighton. BYO only: no Otomata↔PromptWatch commercial agreement.
CONNECTOR = _c(
    "promptwatch", ["promptwatch"], auth_modes={"byo_user", "byo_org"},
    secret_kind="fields",
    label="PromptWatch",
    help="AI visibility monitoring — prompts, monitors, responses, "
         "citations, generated content to close the gaps",
    href="https://promptwatch.com", credential_fields=(
        CredentialField("api_key", "API key", secret=True,
                        help="Settings > API Keys on the PromptWatch dashboard"),
        CredentialField("project_id", "Default Project ID", secret=False,
                        required=False,
                        help="ORG-level key targeting a specific project "
                             "only (optional) — see promptwatch_project"),
    ),
)

CATEGORY = "Marketing & SEO"
PUBLISHER = "PromptWatch"
LOGO_DOMAIN = "promptwatch.com"

DESCRIPTION = (
    "How a brand appears in the answers of ChatGPT, Claude, Gemini "
    "and the like — visibility, sentiment and citations, organized into prompts and "
    "monitors, with AI-generated content to fill the coverage "
    "gaps spotted."
)
