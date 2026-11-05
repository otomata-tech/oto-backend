"""The Google account — the carrier that the services (gmail, drive, sheets, calendar,
tasks, chat since the 2026-09-26 split; bigquery since 2026-10-02; google_ads since
2026-10-08; youtube since 2026-11-04) borrow.

A single, read-only tool: `google_accounts` — the connected accounts and, for
each, the services it has AUTHORISED. This is the question that did not exist before the
split (one consent = the six scopes) and that each service now raises:
`drive_file` on an account that only authorised Gmail is refused, naming the card
to open. `gmail(op="accounts")` lists the Gmail mailboxes; this is where
"which accounts, with which rights?" is now asked — accounts SHARED by
the team or org included, since a call can name them in `account`.
"""
from __future__ import annotations

from fastmcp import FastMCP

from .. import access
from ..auth import google as google_oauth


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def google_accounts() -> dict:
        """List the connected Google accounts and the services each one has authorised.

        Returns {accounts: [{email, is_default, services, shared}]} where
        `services` names the Google services this account consented to (gmail,
        drive, sheets, calendar, tasks, chat). A service missing from the list has
        not been authorised on that account yet: connect it from its own connector
        card. `shared` is null for the user's own account, "group" or "org" for an
        account an admin shared with their team or the whole organization.
        Pass `email` as `_account` to the service tools — shared accounts
        included; omitted, the project's pinned account, else the default one.
        """
        sub = access.current_user_sub_or_raise()
        return {
            "accounts": [
                {"email": a.get("google_email"),
                 "is_default": a.get("is_default", False),
                 "services": google_oauth.services_granted(a.get("scopes")),
                 "shared": a.get("shared")}
                for a in google_oauth.reachable_accounts(sub)
            ]
        }
