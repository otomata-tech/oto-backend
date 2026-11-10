"""Registry declaration for the `minari` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# minari: phone prospecting — transcribed call log (AI summary, detected
# objections), contact lists to dial, custom fields, team analytics
# (pickup rate, conversations, meetings booked).
# keyed api_key (Bearer), byo-only, NO platform key: the key is created in
# Settings → API & webhook and carries the rights of the WHOLE company — a
# customer's call log is theirs, a key shared across orgs would make no
# sense (same principle as `stripe` and `fireflies`).
# ⚠️ Endpoint scope is not uniform on Minari's side: lists and contacts
# only see the CSV import source, while calls and analytics cover
# all sources (CRM included). The tools module says so in its responses,
# it is the connector's trap no. 1.
CONNECTOR = _c(
    "minari", ["minari"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Minari",
    help="phone prospecting — transcribed calls, objections, call lists, "
         "team analytics",
    href="https://minari.ai",
)

CATEGORY = "Calls & meetings"
PUBLISHER = "Minari"
LOGO_DOMAIN = "minari.ai"

DESCRIPTION = (
    "Phone prospecting tracked by Minari: transcribed call log "
    "(AI summary, detected objections), contact lists to dial, "
    "custom fields, team statistics (pickup rate, conversations, "
    "meetings booked)."
)
