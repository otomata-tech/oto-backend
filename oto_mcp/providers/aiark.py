"""Registry declaration of the `aiark` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# aiark: classic connector (kind="tools", ex-federated MCP #152 → requalified
# #160). Synchronous REST client in oto-core (`oto.tools.aiark`), curated tools
# in `tools/aiark.py` (LLM contract), standard key cascade
# (`resolve_api_key`) + a quota settled per call (`units`) → platform mode possible.
# v1 = synchronous endpoints (company/people search, single-person export+email,
# reverse-lookup, mobile phone); AI Ark's bulk exports are async
# (webhook) → out of scope.
# ⚠️ Tools namespace = `linkedin_aiark` (ADR 0010 §Amendment 2026-08-10):
# the name carries the CAPABILITY (LinkedIn) suffixed by the PROVIDER, because two
# NON-SUBSTITUTABLE providers deliver it — AI Ark = data BOUGHT per credit
# (no connected account), Unipile = the OPERATED session (`linkedin_unipile_*`).
# `namespace_of` resolves to the longest declared prefix: the two keep a
# distinct gate. The connector, for its part, keeps the provider's name — it is the unit
# of activation and credential.
# **Absorbs the former `linkedin` connector** (#231, dropped on 2026-08-10,
# oto-backend#279): same vendor, same `AiArkClient` client, same 5 functions —
# it differed only by the auth mode (`platform` only vs BYO), which is
# an INSTANCE distinction (ADR 0038/0044 §F), not a connector one. The duplicate
# cost setting the same key TWICE for a single vendor credit pool
# (ADR 0024: each connector resolves ITS name). The "offered by oto" packaging
# survives as is: it is the platform grant on `aiark`. Nothing to migrate on the
# vault side — no grant had been set under `linkedin`, whose 5 tools had been
# mounted and inoperative since going live.
CONNECTOR = _c(
    "aiark", ["linkedin_aiark"],
    auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    secret_kind="api_key",
    label="AI Ark",
    help="people & company search via LinkedIn (data bought per credit)",
    href="https://ai-ark.com",
)

CATEGORY = "Lead & company data"
PUBLISHER = "AI Ark"
LOGO_DOMAIN = "ai-ark.com"

DESCRIPTION = (
    "Search for people and companies via LinkedIn, as data BOUGHT per "
    "credit — no LinkedIn account to connect. Enriched person record "
    "(export + email), reverse search and mobile phone, as a "
    "synchronous call. Alternative to Unipile (operated LinkedIn session) for those who do not "
    "want to connect an account."
)
