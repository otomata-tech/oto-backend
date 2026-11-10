"""Registry declaration for the `instagram_meta` connector — the STATISTICS of a
professional Instagram account, via Meta's official API.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# ⚠️ **Two Instagram connectors, and the name says which.** `instagram` (without
# suffix) is the account's MESSAGING, operated by Unipile; this one is the
# STATISTICS, served by Meta's official API ("Instagram API with Instagram
# Login"). They are not substitutable: not the same source, not the same
# credential, not the same rights — this is exactly the case ADR 0010
# §Amendment of 2026-08-10 names, "the namespace carries the CAPABILITY, suffixed with the
# PROVIDER when several non-substitutable providers deliver it", and of which
# `linkedin_unipile_*` / `linkedin_aiark_*` is the precedent.
#
# `tool_visibility.namespace_of` resolves to the longest DECLARED prefix: an
# `instagram_meta_get_profile` therefore lands on THIS connector and not on the other —
# call gate, ACL, selection and visibility remain distinct. Locked by
# `tests/test_instagram_meta_natif.py`.
#
# ⚠️ The symmetry is imperfect and stays so: the doctrinal pair would be
# `instagram_unipile` / `instagram_meta`. Renaming the existing one would break a contract
# consumed OUTSIDE this repo (`instagram_chat`, now `instagram`, is called by procedures in the database
# and by guides) — which is the very reason the Unipile split of
# 2026-08-28 touched no tool name. We add the suffix to the newcomer,
# we do not retrofit it onto the old one.
#
# ⚠️ **Without Meta's App Review, this connector only serves accounts INVITED
# as testers on our application.** This is not a code limit: it is the
# regime of unpublished Meta apps. A person who signs up alone
# gets a refusal at consent, and that is what the card announces BEFORE and
# what the error message names AFTER.
CONNECTOR = _c(
    "instagram_meta", ["instagram_meta"],
    auth_modes={"byo_user"},
    # The Instagram consent IS personal: it originates from the person's account,
    # not from their membership in an org. Same family as `google`.
    personal_session=True, secret_kind="oauth",
    label="Instagram (statistiques)",
    help="The statistics of your professional Instagram account — profile, "
         "posts, reach, interactions. You authorize oto at Instagram; "
         "no Facebook Page is needed. Read-only.",
    href="https://www.instagram.com",
)

CATEGORY = "Marketing & SEO"
# Who receives the call (`docs/connector-vault.md` §"What the card SAYS"): Meta.
# It is THEIR official API, THEIR consent dialog, and it is they who decide
# what the token opens. The contrast with `planity` — declared "Otomata" because the
# connector replays a session there for lack of a public API — is the substance of the
# rule, not an exception to it.
PUBLISHER = "Meta"
LOGO_DOMAIN = "instagram.com"

DESCRIPTION = (
    "The statistics of a professional Instagram account (Business or Creator) "
    "via Meta's official API: profile and followers, recent posts, "
    "insights for a post and for the account, plus a calculation of the best "
    "posting hours. Read-only — nothing is published, modified or deleted. You "
    "connect your Instagram account directly: no Facebook Page is "
    "required. These are the statistics, not the messages: DMs have their own "
    "connector."
)
