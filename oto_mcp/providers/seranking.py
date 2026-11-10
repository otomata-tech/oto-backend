"""Registry declaration of the `seranking` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _decrit

# seranking: SE Ranking's Data API and Project API, one API key for both, served by
# the GENERIC READER of its description (`tools/decrits.py`): no `tools/seranking.py`,
# the library's engine runs the description (`oto.connectors.engine`, auth `api_key`,
# header `Authorization: Token <key>`). Label, namespace, modes and the credential
# field are read in the description: `byo_user` and `byo_org`, no platform key. The
# key is kept under `key`, resolved by `access.resolve_api_key`.
#
# The pair splits on the description's classes: `seranking` reads the projects and
# audits (free), `seranking_act` holds every call that spends Data API credits, a
# positions recheck and an audit start (the plan's checks). The credits are the
# customer's own: no call here goes through a re-billed key, so none is reserved
# (`access/credits.py`).
CONNECTOR = _decrit(
    "seranking",
    help="SE Ranking: a domain's SEO data (keywords, competitors, backlinks, "
         "keyword research — spends Data API credits) and the projects tracked "
         "in the account (positions, competitors, site audits)",
    href="https://online.seranking.com",
    key_help="API key from the SE Ranking account (API > Dashboard); one key "
             "opens both the Data API and the Project API",
)

CATEGORY = "Marketing & SEO"
PUBLISHER = "SE Ranking"
LOGO_DOMAIN = "seranking.com"

DESCRIPTION = (
    "SE Ranking, with your own API key: a domain's SEO overview and history, its "
    "organic and paid keywords, competitors, backlinks and referring domains, "
    "keyword metrics and research (these spend the account's Data API credits), "
    "and the projects tracked in the account — their keywords, positions, "
    "competitors and site audits. Rechecking positions and starting an audit use "
    "the plan's checks."
)
