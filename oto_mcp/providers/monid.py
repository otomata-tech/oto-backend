"""Registry declaration of the `monid` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# monid: a paid GATEWAY — roughly 2,000 endpoints from about 70 providers
# (web search, scraping, enrichment, social networks…) behind a single key,
# each call debited from the Monid workspace's prepaid wallet. The client lives in
# oto-core (`oto.tools.monid`), the tools in `tools/monid.py`.
# keyed api_key, same regime as `apify`: BYO by default (the wallet is that of the
# connected account); platform key GRANT-ONLY, because a call there spends money.
CONNECTOR = _c(
    "monid", ["monid"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    default_quota=0, platform_key_open=False,  # platform key on explicit grant (calls billed to the wallet)
    secret_kind="api_key",
    label="Monid",
    # `help` goes into the namespace map served to ALL sessions: short,
    # it says that third-party providers get paid here, and for what needs.
    help="paid gateway to ~2,000 data endpoints (web search, scraping, "
         "enrichment, social networks…), billed per call",
    href="https://monid.ai",
    credential_fields=(
        CredentialField(
            "key", "Monid API key", secret=True,
            help="monid.ai → dashboard → API keys; it starts with `monid_`"),
    ),
)

CATEGORY = "Web search & scraping"
PUBLISHER = "Monid"
LOGO_DOMAIN = "monid.ai"

DESCRIPTION = (
    "The endpoints of about 70 data providers (web search, scraping, "
    "enrichment, social networks…) behind a single key: find the endpoint, "
    "read its price and input schema, launch it. Each call is debited from the "
    "connected account's Monid wallet; platform access is restricted (explicit grant)."
)
