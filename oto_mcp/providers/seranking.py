"""Registry declaration of the `seranking` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _decrit

# seranking: SE Ranking's Data API (a domain's keywords, competitors and
# backlinks, keyword research, paid in credits) and Project API (tracked
# projects, positions, competitors, site audits), served by the GENERIC READER
# of its description (`tools/decrits.py`): no `tools/seranking.py`, the
# library's engine runs the description (`oto.connectors.engine`, auth
# `api_key` in `Authorization: Token`). Label, namespace, modes and the
# credential are read in the description: `byo_user` and `byo_org`, no
# platform key. ONE key opens both APIs, kept under `key`. Every call that
# spends Data API credits, and rechecking positions or starting an audit, is on
# `seranking_act`; the rest reads on `seranking`.
CONNECTOR = _decrit(
    "seranking",
    help="SEO with SE Ranking — a domain's keywords, competitors and backlinks, "
         "keyword research, tracked projects and positions, site audits",
    href="https://online.seranking.com",
    key_help="In your SE Ranking account, API > Dashboard: one key serves both the "
             "Data API (paid in credits) and the Project API",
)

CATEGORY = "Marketing"
PUBLISHER = "SE Ranking"
LOGO_DOMAIN = "seranking.com"

DESCRIPTION = (
    "SEO with SE Ranking, on your own subscription: a domain's overview, "
    "organic and paid keywords, competitors and backlinks, keyword metrics and "
    "ideas (Data API, paid in SE Ranking credits); the tracked projects, their "
    "keywords, positions and competitors, and the site audits (Project API). "
    "One API key opens both; no platform key."
)
