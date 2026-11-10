"""Registry declaration of the `supabase` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "supabase", ["supabase"], auth_modes={"byo_user"}, keyed=True,
    secret_kind="api_key", label="Supabase",
    help="Management API (projets, config auth, logs)",
    href="https://supabase.com",
)

CATEGORY = "Product & data"
PUBLISHER = "Supabase"
LOGO_DOMAIN = "supabase.com"

DESCRIPTION = (
    "The Supabase Management API: list and configure projects, the "
    "authentication configuration, and read the logs. Not the application's own "
    "data (the project's Postgres tables) — only its "
    "administration."
)
