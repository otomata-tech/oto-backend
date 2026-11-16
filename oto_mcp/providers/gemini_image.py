"""Registry declaration of the `gemini_image` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# gemini_image: images generated and edited by Google's Gemini image models ("Nano
# Banana"), from a prompt and up to fourteen reference images. A neighbour of
# `higgsfield` (images and clips) by situation: media production. The client lives in
# the connector library (`oto.tools.gemini.GeminiClient.create_image`), the tools in
# `tools/gemini_image.py` (the pair `gemini_image` reads, `gemini_image_act` acts).
#
# keyed `api_key`: an AI Studio key, sent in the `x-goog-api-key` header. A free-tier
# project has NO image quota (Google answers 429 with `limit: 0`): billing must be on.
#
# BYO (user OR org: the customer's own Google billing) or a platform key on explicit
# grant (`platform_key_open=False`, `default_quota=0`: an org without a grant never
# reaches it). The call is synchronous and Google's answer counts the tokens it used:
# on the platform key the grant's quota is checked for the call's BOUND (published
# per-image price, plus a thinking allowance) before anything is sent, and the call
# notes what it actually cost (`units`, `quantity`, in micro-dollars). Every grant
# carries a daily quota in those micro-dollars (`grant_quota_unit`): the default, 0,
# would be unlimited on our billing. That quota is the ORG's (`grant_quota_per="org"`):
# every member of the org the call is made in draws on one counter, since the cost is
# billed to the org. The platform key serves only the models priced in
# `tools/gemini_image.py::PRICES`.
CONNECTOR = _c(
    "gemini_image", ["gemini_image"], auth_modes={"byo_user", "byo_org", "platform"},
    default_quota=0, platform_key_open=False,
    grant_quota_unit="micro-dollars (US$ 0.000001) of generation cost",
    grant_quota_per="org",
    keyed=True, secret_kind="api_key",
    credential_fields=(
        CredentialField(
            "key", "API key", secret=True,
            help="Google AI Studio (aistudio.google.com) → Get API key → Create API key. "
                 "Image generation needs billing enabled on the key's Google project: a "
                 "free-tier key is refused by Google for images."),
    ),
    label="Gemini images (Nano Banana)",
    help="image generation and editing with Google's Gemini image models (Nano Banana), "
         "from a prompt and reference images, on your Google billing or the shared key "
         "when it is granted",
    href="https://aistudio.google.com",
)

CATEGORY = "AI models"
PUBLISHER = "Google"
LOGO_DOMAIN = "gemini.google.com"

DESCRIPTION = (
    "Generate and edit images with Google's Gemini image models (Nano Banana): from a "
    "prompt, from reference images to follow (products, characters, a style), or by "
    "changing an image you give it, in 1K, 2K or 4K and many aspect ratios. Results come "
    "back as download links. Your own AI Studio key on your Google billing, or the shared "
    "key when it is granted to your org: each image is billed at what it cost."
)
