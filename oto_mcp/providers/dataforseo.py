"""Registry declaration of the `dataforseo` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _decrit

# dataforseo: SEO data — keyword volumes and ideas, a domain's rankings, traffic
# and competitors, backlinks, AI-search visibility, site audits, technologies —
# served by the GENERIC READER of its description (`tools/decrits.py`) since
# 11/11/2026: no `tools/dataforseo*.py`, the library's engine runs the description
# (`oto.connectors.engine`, auth `basic`). Label, namespace, modes and the two
# credential fields are read in the description: the API login (not secret) and
# the API password, kept as two fields (`secret_kind="fields"`) under the same names
# as the hand-written tools kept them, so every credential already posted resolves
# unchanged. One tool, `dataforseo`, whose `op` is the function (SIMULATION: a site
# crawl and a reviews task are created, nothing is sent to a third party).
#
# Pay-as-you-go: no seat, no plan, a prepaid USD balance drawn per call — which is
# what makes a platform key resellable, unlike Ahrefs (expensive named seat,
# byo-only). Every answer says its real USD cost; the description settles it in
# micro-dollars (`settle.scale`), and the reader notes it as the call's `quantity`,
# so billing is the real spend times a rate. No quota `units`
# (`access.reglement.SANS_DEBIT`).
# Paid platform key, like `forager`: posted CLOSED (`set_platform_key`), it serves
# only the orgs granted to it — the host enrols and cuts orgs on its own billing.
# /admin posts the platform key as BOTH fields: the login is required and not
# secret, so a lone password is refused at posting (`validate_fields`). Reading the
# account (`user_data`, the probe) is refused on the shared key: its balance is the
# platform's.
CONNECTOR = _decrit(
    "dataforseo",
    help="SEO data — keywords, a domain's rankings and traffic, backlinks, AI-search "
         "visibility, site audits, technologies (pay-as-you-go)",
    href="https://app.dataforseo.com/api-access",
    key_help={"login": "Dashboard → API Access.",
              "password": "Dashboard → API Access. Not your sign-in password."},
    platform_key_open=False, default_quota=0,
)

CATEGORY = "Marketing & SEO"
PUBLISHER = "DataForSEO"
LOGO_DOMAIN = "dataforseo.com"

DESCRIPTION = (
    "SEO data from DataForSEO: Google keyword volume, CPC, difficulty, intent, "
    "ideas and trends; a domain's ranking keywords, estimated traffic and its "
    "history, top pages and competitors, and the keyword gap between two sites; "
    "backlinks, referring domains, link gap and new/lost links; brand mentions "
    "in AI answers (Google AI Overviews, ChatGPT) and on the web; technical "
    "audits (one page, Lighthouse, site crawl); the technologies a site runs on, "
    "and the sites using one; Google organic and Maps results, a business "
    "profile and its reviews. Pay-as-you-go, billed per call on its real cost. "
    "Runs on your own DataForSEO API login, or on the shared key when it is "
    "granted to your org."
)
