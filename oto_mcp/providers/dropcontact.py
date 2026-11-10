"""Registry declaration of the `dropcontact` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# dropcontact: contact + company enrichment (email/phone/SIRENE) in
# async batch (submit/fetch, same idiom as fullenrich). byo by default;
# platform key GRANT-ONLY since 26/08 (#405, GTM credits — never opened).
CONNECTOR = _c(
    "dropcontact", ["dropcontact"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    default_quota=0, platform_key_open=False,  # platform key on explicit grant (data bought per credit)
    secret_kind="api_key",
    label="Dropcontact", help="contact + company enrichment (email/phone/SIRENE)",
    href="https://www.dropcontact.com",
)

CATEGORY = "Lead & company data"
PUBLISHER = "Dropcontact"
LOGO_DOMAIN = "dropcontact.com"

DESCRIPTION = (
    "Contact and company enrichment at Dropcontact: email, "
    "phone, SIRENE data, processed in asynchronous batches (you submit, "
    "then fetch the result)."
)
