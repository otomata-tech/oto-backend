"""Registry declaration of the `zohodesk` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c
from .zoho import data_center_field

CONNECTOR = _c(
    "zohodesk", ["zohodesk"], auth_modes={"byo_user", "byo_org"}, secret_kind="fields",
    label="Zoho Desk",
    help="Zoho Desk support (tickets, threads, contacts, KB articles)",
    href="https://desk.zoho.com", credential_fields=(
        CredentialField("client_id", "Client ID", secret=True,
                        help="1000.XXXXXXXX… (self-client)", text_key="zoho.client_id"),
        CredentialField("client_secret", "Client Secret", secret=True,
                        help="self-client secret", text_key="zoho.client_secret"),
        # OPTIONAL: in "sign in with Zoho" mode (server-based) it is not
        # pasted — the consent flow fills it in. Required only when a self client
        # is set up by hand.
        CredentialField("refresh_token", "Refresh Token", secret=True,
                        required=False,
                        help="1000.xxxxx.yyyyy — leave empty if you sign in via Zoho", text_key="zoho.refresh_token"),
        # OPTIONAL: the KB endpoints (articles) resolve the portal from the
        # single-org token — verified empirically. And a credential scoped to
        # `Desk.articles.READ` alone CANNOT discover it (/organizations →
        # 403 SCOPE_MISMATCH), so requiring it made the connector impossible to
        # set up for that case. Still useful for endpoints that require the
        # `orgId` header (tickets…), which will then demand it on the API side.
        CredentialField("org_id", "Org ID (optional)", secret=False,
                        required=False,
                        help="e.g. 800123456 — not needed to read articles", text_key="zoho.org_id"),
        # OPTIONAL, non-secret: the Zoho organization this app is meant for. Checked at
        # each consent (`auth/zoho.py::check_org`) — without it, any Zoho account can
        # consent to the app, and the consent goes through signaled.
        CredentialField("zoho_org_id", "Expected Zoho organization ID", secret=False,
                        required=False,
                        help="the organization ID shown in Zoho Desk (Setup → Developer Space → API)", text_key="zoho.expected_org_desk"),
        data_center_field(),
    ),
)

CATEGORY = "Communication"
PUBLISHER = "Zoho"
LOGO_DOMAIN = "zoho.com"

DESCRIPTION = (
    "Customer support in Zoho Desk: tickets, conversation threads, contacts "
    "and knowledge-base articles."
)

# Access levels — same contract as `providers/zoho.py`. Required scopes follow the
# endpoint → scope table of the oto-core client (`oto.tools.zohodesk.client`,
# `_SCOPE_BY_PREFIX`): `tickets/search` → `Desk.search.READ`, `tickets` / `contacts` →
# `.READ` on GET and `.WRITE` otherwise, `articles` → `Desk.articles.READ`,
# `departments` → `Desk.basic.READ`. Ticket search also requires `Desk.tickets.READ`:
# `Desk.search.READ` is not bounded to articles, and a level that serves the article
# search (`read`) must never reach a ticket. ⚠️ `full` (the historical consent) grants no WRITE
# scope: the three write tools are not covered by it — served at `full` all the same
# (nothing is filtered there), and refused by Zoho as before.
ACCESS_LEVELS = {
    "levels": {
        "read": ("Desk.articles.READ", "Desk.search.READ"),
        "full": ("Desk.articles.READ", "Desk.basic.READ", "Desk.search.READ",
                 "Desk.tickets.READ", "Desk.contacts.READ"),
    },
    "scope_implies": {},
    "tools": {
        "zohodesk_tickets": ("Desk.tickets.READ",),
        "zohodesk_ticket": ("Desk.tickets.READ",),
        "zohodesk_ticket_threads": ("Desk.tickets.READ",),
        "zohodesk_search_tickets": ("Desk.search.READ", "Desk.tickets.READ"),
        "zohodesk_create_ticket": ("Desk.tickets.WRITE",),
        "zohodesk_update_ticket": ("Desk.tickets.WRITE",),
        "zohodesk_contacts": ("Desk.contacts.READ",),
        "zohodesk_create_contact": ("Desk.contacts.WRITE",),
        "zohodesk_departments": ("Desk.basic.READ",),
        "zohodesk_articles": ("Desk.articles.READ",),
        "zohodesk_search_articles": ("Desk.search.READ", "Desk.articles.READ"),
        "zohodesk_article": ("Desk.articles.READ",),
    },
}
