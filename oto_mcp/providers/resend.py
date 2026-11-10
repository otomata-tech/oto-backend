"""Registry declaration of the `resend` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# resend: the ORG's Resend account, for two things.
#
# 1. BYOK transactional email: the key is consumed by `email_send`
#    (transport=resend) via resolve_api_key, user > org cascade. The sending
#    domain is verified on the Resend side by the org (the key can only send from
#    the domains verified in the org's Resend account → zero domain logic on the
#    oto side); the `from` address lives in orgs.email_settings, not in the
#    credential. That routing is `EMAIL_CONNECTOR_TRANSPORT`, unchanged.
# 2. The contact list behind Broadcasts: contacts, segments, topics — tools in
#    `tools/resend.py` (contacts) and `tools/resend_segments.py` (segments,
#    topics), shared base in `tools/resend_socle.py`, client in the connector
#    library (`oto.tools.resend.ResendClient`). These routes need a key with
#    Full access; a sending-only key keeps sending and is refused there with a
#    message that says so.
#
# byo_org only (email is org-level); self_serve = available on request to any org.
CONNECTOR = _c(
    "resend", ["resend"], auth_modes={"byo_org"}, keyed=True,
    secret_kind="api_key",
    modules=("resend", "resend_segments"),
    label="Resend",
    help="the org's Resend account: contacts, segments and topics behind "
         "Broadcasts (its key also sends `email_send`)",
    publisher="Resend", href="https://resend.com",
)

CATEGORY = "Product & data"
LOGO_DOMAIN = "resend.com"

DESCRIPTION = (
    "Your organization's Resend account. Its key sends the platform's emails "
    "from a domain you have verified at Resend, and, with Full access, keeps "
    "your Broadcast audience up to date: contacts, segments, and the topics "
    "recipients subscribe to or leave."
)
