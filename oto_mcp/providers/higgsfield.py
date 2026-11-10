"""Registry declaration of the `higgsfield` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# higgsfield: image and video generation across many models (Higgsfield Soul,
# Kling, Seedance, Wan, Hailuo, Recraft…), priced before each job. A neighbour of
# `elevenlabs` (voice and music) by situation: media production. The client lives in
# the connector library (`oto.tools.higgsfield.HiggsfieldClient`), the tools in
# `tools/higgsfield.py` (the pair `higgsfield` reads, `higgsfield_act` acts).
#
# keyed `api_key`: the console gives a key id and a secret, sent together as
# `Authorization: Key <id>:<secret>`; they are pasted as ONE value joined by a colon,
# the form the API documents and the connector description declares.
#
# BYO (user OR org: the customer's own prepaid balance, in dollars) or a platform key on
# explicit grant (`platform_key_open=False`, `default_quota=0`: an org without a grant
# never reaches it). Higgsfield charges a job only once it completes, at the price the
# guard read before submitting: on the platform key that price is recorded with the job
# (`db/higgsfield_jobs.py`) and billed by the read of the completed job (`quantity`, in
# micro-dollars). Every grant carries a daily quota in those micro-dollars
# (`grant_quota_unit`), checked at submission for the job's price: the default, 0, would
# be unlimited on our balance.
CONNECTOR = _c(
    "higgsfield", ["higgsfield"], auth_modes={"byo_user", "byo_org", "platform"},
    default_quota=0, platform_key_open=False,
    grant_quota_unit="micro-dollars (US$ 0.000001) of job price",
    keyed=True, secret_kind="api_key",
    credential_fields=(
        CredentialField(
            "key", "API key (KEY_ID:KEY_SECRET)", secret=True,
            help="Higgsfield console (console.higgsfield.ai) → API keys → create a "
                 "key. Paste the key id and the secret joined by a colon: "
                 "KEY_ID:KEY_SECRET. The secret is shown only once."),
    ),
    label="Higgsfield",
    help="image and video generation (Soul, Kling, Seedance, Wan…), each job priced "
         "before it runs, on your prepaid balance or the shared key when it is granted",
    href="https://higgsfield.ai",
)

CATEGORY = "AI & automation"
PUBLISHER = "Higgsfield"
LOGO_DOMAIN = "higgsfield.ai"

DESCRIPTION = (
    "Generate images and video clips with your Higgsfield account, across its models "
    "(Higgsfield Soul, Kling, Seedance, Wan and more): from a prompt, a still to "
    "animate, or a first and last frame. Each job is priced before it runs and refused "
    "above the amount you allow. Results come back as download links kept seven days. "
    "Your own API key, on your prepaid balance, or the shared key when it is granted "
    "to your org: a job is then billed only once it completes."
)
