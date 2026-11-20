"""Registry declaration of the `demodesk` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _decrit

# demodesk: a sales meeting platform that records the calls — meetings,
# recordings, transcripts, AI-generated notes and coaching scorecards. Same
# family as `claap`, `fireflies`, `granola`, `leexi` and `modjo`, hence the
# "Calls & meetings" category. Served by the GENERIC READER of its description
# (`tools/decrits.py`): no `tools/demodesk.py`, the library's engine runs the
# description (auth `bearer`). Label, namespace, modes and the credential field
# are read in the description: `byo_user` and `byo_org`, no platform key (these
# are the customer's own recorded calls). The key is kept under `key`, resolved
# by `access.resolve_api_key`.
#
# Read only: every function of the description is `read`, so the reader serves
# `demodesk` alone, never a `demodesk_act`.
#
# ⚠️ The key inherits the rights of the Demodesk user who created it: it sees
# what that user sees. The whole company takes an admin's key who is a member of
# every restricted group; an `only_me` recording stays its host's alone.
CONNECTOR = _decrit(
    "demodesk",
    help="recorded sales calls: meetings, recordings, transcripts, AI notes, scorecards",
    href="https://demodesk.com",
    key_help="Demodesk → Settings → Integrations → Other: generate an API key. It sees "
             "what its creator sees: to read the whole company, create it as an admin "
             "who is a member of every restricted group.",
)

CATEGORY = "Calls & meetings"
PUBLISHER = "Demodesk"
LOGO_DOMAIN = "demodesk.com"

DESCRIPTION = (
    "Sales calls recorded by Demodesk: meetings, recordings, transcripts (translated "
    "on demand), AI-generated notes and coaching scorecards, read only — same family "
    "as Claap, Fireflies, Granola and Modjo. Your own API key; it sees what its "
    "creator sees in Demodesk."
)
