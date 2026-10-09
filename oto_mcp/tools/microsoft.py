"""The Microsoft 365 account — the carrier that each Microsoft service (SharePoint &
OneDrive, Outlook, Outlook Calendar, Teams) borrows.

A single, read-only tool, the twin of `google_accounts`: `microsoft_accounts` — the
linked accounts and, for each, the services it has AUTHORISED and the directory it
signs in to. It is the question each service raises: `sharepoint_file` on an account
that has not authorised SharePoint is refused, naming the card to open. No network
call: what the vault knows (`meta.scopes`, refreshed at every token renewal).
"""
from __future__ import annotations

from fastmcp import FastMCP

from .. import access
from ..auth import microsoft as ms_auth


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def microsoft_accounts() -> dict:
        """List the linked Microsoft 365 accounts and the services each one has authorised.

        Returns {accounts: [{account, email, is_default, services, directory}]} where
        `services` names the Microsoft services this account consented to (sharepoint…).
        A service missing from the list has not been authorised on that account yet:
        connect it from its own connector card. `directory` is the client's directory the
        account signs in to (a guest account), null for its home directory.
        `lent_by` (present only then) = the sub of the colleague who LENT you that
        account: usable like yours, not yours to revoke.
        Pass `account` as `_account` to the service tools; omitted, the project's
        pinned account, else the only one, else the default one — and with no account
        of your own, the only account lent to you for that service.
        """
        sub = access.current_user_sub_or_raise()
        return {
            "accounts": [
                {"account": c["account"],
                 "email": (c.get("meta") or {}).get("email"),
                 "is_default": bool((c.get("meta") or {}).get("is_default")),
                 "services": ms_auth.services_granted((c.get("meta") or {}).get("scopes")),
                 "directory": (c.get("meta") or {}).get("tenant"),
                 # Lent by a peer (`oto_instance op=lend`): usable via `_account`,
                 # not yours to revoke.
                 **({"lent_by": c["lent_by"]} if c.get("lent_by") else {})}
                for c in ms_auth.accounts_for(sub)
            ]
        }
