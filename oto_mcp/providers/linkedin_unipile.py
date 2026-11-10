"""Registry declaration of the `linkedin_unipile` connector — the operated LinkedIn session.

Single home of its entry: `providers/__init__.py` AGGREGATES it. The shape
common to the six hosted connections lives with the key holder
(`providers/unipile.channel`) — here, what sets THIS ONE apart.
"""
from __future__ import annotations

from .unipile import channel

# linkedin_unipile: YOUR LinkedIn session — search, profiles, posts, network, job
# offers, messaging. 8 `op=` tools under the `linkedin_unipile` namespace.
#
# ⚠️ The connector name IS its namespace, and it KEEPS the provider suffix
# (ADR 0010 §Amendment 2026-08-10): the namespace carries the CAPABILITY (LinkedIn)
# suffixed by the PROVIDER when several non-substitutable providers deliver it —
# here Unipile (the OPERATED session) and AI Ark (`linkedin_aiark_*`, data BOUGHT
# by the credit: email, mobile, reverse-lookup, none of which exists on
# LinkedIn). `namespace_of` resolves to the longest DECLARED prefix: the two keep
# a distinct gate. Locked by tests/test_linkedin.py.
#
# Tool names are a CONTRACT consumed outside the repo (procedures stored in the
# DB of several orgs, platform guides, a usage meter): the 2026-08-28 split
# does not touch them. What changes is the CARD — it is called "LinkedIn",
# links to linkedin.com and does not name the provider: what the person
# connects is their LinkedIn account. Unipile stays named on the `unipile`
# card, which IS the provider account and holds the key.
#
# Its tools live in `tools/unipile.py` (with the shared messaging factory
# and `unipile_connect_start`) — hence the explicit `modules=("unipile",)`: the
# module does not carry the connector's name, and `register_all` dedupes modules.
CONNECTOR = channel(
    "linkedin_unipile",
    hosted_channel="LINKEDIN",
    label="LinkedIn",
    help="Your LinkedIn session — search, profiles, posts, network, jobs, messaging. "
         "Your account connects through Unipile, our provider, which holds the session.",
    href="https://www.linkedin.com",
    modules=("unipile",),
)

CATEGORY = "Outreach"
LOGO_DOMAIN = "linkedin.com"
DESCRIPTION = (
    "Your LinkedIn session, operated for you: search for people and "
    "companies, profiles, posts and comments, network (invitations, "
    "connections), job offers and messaging. You connect YOUR account and act "
    "as yourself. For an email or a mobile number a profile doesn't publish, "
    "you need an enrichment connector (AI Ark, Dropcontact, FullEnrich…)."
)
