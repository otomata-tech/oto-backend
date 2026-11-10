"""Registry declaration for the `claap` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# claap: meeting and call recordings, transcripts, views — same family as
# `fireflies`, `grain`, `granola` and `leexi`, hence the Knowledge category and
# the same BYO regime.
#
# keyed `api_key`: a single `cla_…` key, sent in the `X-Claap-Key` header.
# ⚠️ It is a WORKSPACE key acting as admin: it sees every recording visible in
# global search, not only those of the person who created it.
#
# Strict BYOK (`byo_user` + `byo_org`, no platform mode): these are the
# customer's recorded meetings, there can be no shared oto key.
CONNECTOR = _c(
    "claap", ["claap"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Claap",
    help="meeting and call recordings, transcripts, AI summaries, views",
    href="https://www.claap.io",
)

CATEGORY = "Calls & meetings"
PUBLISHER = "Claap"
LOGO_DOMAIN = "claap.io"

DESCRIPTION = (
    "Meetings and calls recorded by Claap: transcripts, AI summaries and fields, "
    "action items, saved views — same family as Fireflies, Grain, Granola and "
    "Leexi. Workspace API key (`cla_…`), created by an admin."
)
