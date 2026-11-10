"""Registry declaration of the `dataforseo` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# dataforseo: SEO data — keyword volumes and ideas, a domain's rankings, traffic
# and competitors, backlinks. HTTP Basic auth on two fields (API login + API
# password), hence `secret_kind="fields"`. Pay-as-you-go: no seat, no plan, a
# prepaid USD balance drawn per call — which is what makes a platform key
# resellable, unlike Ahrefs (expensive named seat, byo-only). Every call reports
# its real USD cost, traced as `quantity` in micro-dollars (`tools/dataforseo.py`),
# so billing is the real spend times a rate.
# Paid platform key, like `forager`: posted CLOSED (`set_platform_key`), it serves
# only the orgs granted to it — the host enrols and cuts orgs on its own billing.
# /admin posts the platform key as BOTH fields (`fields`): the login is required and
# not secret, so a lone password is refused at posting (`validate_fields`).
CONNECTOR = _c(
    "dataforseo", ["dataforseo"], auth_modes={"byo_user", "byo_org", "platform"},
    default_quota=0, platform_key_open=False, secret_kind="fields",
    label="DataForSEO",
    help="SEO data — keywords, a domain's rankings and traffic, backlinks, AI-search "
         "visibility, site audits, technologies (pay-as-you-go)",
    publisher="DataForSEO", href="https://app.dataforseo.com/api-access",
    # Eight tools over three modules: keywords/domain/backlinks/account, AI and web
    # mentions, site audit and technologies (tools/dataforseo*.py).
    modules=("dataforseo", "dataforseo_insights", "dataforseo_site"),
    credential_fields=(
        CredentialField("login", "API login", secret=False,
                        help="Dashboard → API Access."),
        CredentialField("password", "API password", secret=True,
                        help="Dashboard → API Access. Not your sign-in password."),
    ),
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
    "and the sites using one. Pay-as-you-go, billed per call on its real cost. "
    "Runs on your own DataForSEO API login, or on the shared key when it is "
    "granted to your org."
)
