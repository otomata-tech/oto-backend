"""Registry declaration of the `pipecorn` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# pipecorn: waterfall contact enrichment — a person's mobile phone or work email, from a
# LinkedIn profile (or, for an email, a name and a company), Pipecorn running its own
# cascade of providers behind ONE asynchronous lookup (submitted, then read back) — plus
# synchronous reads of its database: a person's profile, a company's firmographics, its
# technologies and its open job postings (served without any email or phone).
# The client lives in the connector library (`oto.tools.pipecorn.PipecornClient`),
# the tool in `tools/pipecorn.py` (one read tool, `pipecorn`: a lookup changes
# nothing at Pipecorn, it spends credits). A third party's personal email is refused.
#
# ONE key in the `X-API-KEY` header (`keyed=True`, vault field `key`).
#
# BYO (user OR org: the customer's own Pipecorn credits). A platform key only on explicit
# grant (`platform_key_open=False`, `default_quota=0`: an org without a grant never
# reaches it, and none is set up by default — the connector starts closed). Granted, it
# would serve the contact lookup (a phone, a work email), its worst case reserved on the
# units budget (`access/credits.py`) and settled at the read; the synchronous lookups,
# outside that budget, and the balance stay on the customer's own key. What a lookup
# found and spent is traced (`tools/pipecorn.py`).
CONNECTOR = _c(
    "pipecorn", ["pipecorn"], auth_modes={"byo_user", "byo_org", "platform"},
    default_quota=0, platform_key_open=False,
    keyed=True, secret_kind="api_key",
    credential_fields=(
        CredentialField(
            "key", "API key", secret=True,
            help="Pipecorn → Settings → API keys. A test key (pc_test_…) answers "
                 "with sample data and spends nothing."),
    ),
    label="Pipecorn",
    help="waterfall contact enrichment (a person's mobile phone or work email), "
         "profiles, company data, technologies and job postings, on your own Pipecorn "
         "key",
    href="https://www.pipecorn.com",
)

CATEGORY = "Lead & company data"
PUBLISHER = "Pipecorn"
LOGO_DOMAIN = "pipecorn.com"

DESCRIPTION = (
    "Find a person's mobile phone or work email with Pipecorn, which queries many "
    "data providers one after the other in a single lookup. A LinkedIn profile is "
    "enough; an email can also be found from a name and a company. Also reads a "
    "person's profile, a company's data, the technologies it uses and its open job "
    "postings. You only pay for what is found, on your own Pipecorn API key."
)
