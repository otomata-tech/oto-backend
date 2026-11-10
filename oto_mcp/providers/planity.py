"""Registry declaration of the `planity` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# planity: a salon's calendar + till, NATIVE (kind=tools). The connector
# authenticates with the Planity account's email and password, stored in the vault
# (`basic_auth`, member tier). The client lives in oto-core
# (`oto.tools.planity`), the tools in `tools/planity*.py` — the `planity_*` names
# and their schemas have not changed (agents and the listing know them); the
# additions of 2026-09-09 are additive.
#
# ⚠️ This connector was `kind="mount"` until 2026-09-09: the same tools
# were served by a standalone MCP server that we operated, to which the backend
# replayed the vault's `basic_auth` per request. The stored format and the
# credential do not change one iota — what changes is that there is no longer an
# intermediary to operate, hence no gateway left to announce in the listing.
#
# ⚠️ THE PUBLISHER IS US (corrected on 2026-09-02). The listing used to announce
# "Planity" as publisher, with the planity.com logo: it read as an official
# integration, whereas the connector is OURS and it authenticates with the user's
# Planity email and password — the heaviest commitment the listing makes, and one
# that was missing from it.
CONNECTOR = _c(
    "planity", ["planity"],
    auth_modes={"byo_user"}, secret_kind="basic_auth",
    # Thirty-two tools in four modules. The dividing line follows the families
    # of SOURCES, not a convenience split: reference data, customers and
    # the calendar (`planity`); the aggregates served by the statistics lambdas
    # (`planity_stats`); till detail, receipt by receipt (`planity_pos`);
    # what moves in stock (`planity_stock`). And because a file of more than
    # five hundred lines cannot be re-read.
    modules=("planity", "planity_stats", "planity_pos", "planity_stock"),
    label="Planity",
    help="Planity calendar + till (appointments, customers, revenue, stats) — oto "
         "replays your Planity login with your email and password",
    href="https://pro.planity.com",
)

CATEGORY = "Custom & industry apps"
# Declared, not left to the default: "Otomata" is also what the ABSENCE of a
# constant returns, and an oversight must not be confused with this choice.
PUBLISHER = "Otomata"
# Not Planity's logo: the connector is ours, not an official Planity
# integration. Monogram on the UI side.
SANS_LOGO_DE_MARQUE = True

DESCRIPTION = (
    "A salon's Planity calendar and till: appointments, customers, revenue "
    "and statistics. Read-only. Since Planity has no public API, oto itself "
    "replays your login with your Planity account's email and password — it uses "
    "nothing else, and what it can read is what that account can read."
)
