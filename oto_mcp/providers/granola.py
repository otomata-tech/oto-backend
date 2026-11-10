"""Registry declaration of the `granola` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). Cf. `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# granola: meeting notes, transcripts, AI summaries, folders, audit
# log, webhook endpoints. keyed api_key (Bearer), byo-only (no platform
# key) — personal key (any Business member) or workspace key
# (admin, Enterprise), both a simple Bearer here.
CONNECTOR = _c(
    "granola", ["granola"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Granola",
    help="meeting notes, transcripts, AI summaries, folders, audit, webhooks",
    href="https://granola.ai",
)

CATEGORY = "Calls & meetings"
PUBLISHER = "Granola"
LOGO_DOMAIN = "granola.ai"

DESCRIPTION = (
    "The meeting notes taken by Granola: transcripts, AI-generated "
    "summaries, folders, audit log and webhook endpoints. Personal key "
    "(any Business subscription) or workspace key (admin, Enterprise)."
)
