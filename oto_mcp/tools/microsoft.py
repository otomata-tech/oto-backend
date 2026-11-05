"""The Microsoft 365 account — the carrier that each Microsoft service (SharePoint &
OneDrive, Outlook, Outlook Calendar, Teams, OneNote, the directory) borrows.

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
        `shared` (present only then) = "group" or "org": an account an admin connected on
        behalf of your team or your whole organization, `shared_owner` ({id, name}) that
        team or org — not yours to revoke.
        Pass `account` as `_account` to the service tools; omitted, the project's
        pinned account, else your only one, else your default one, else your team's
        account, else your organization's (several at one of these levels and no default:
        refused, naming them). A colleague's account is not listed: it is used ONLY when
        `_account` names it, at the level its rules give you (oto_identity op=list).
        """
        sub = access.current_user_sub_or_raise()
        comptes = ms_auth.avec_noms(ms_auth.accounts_for(sub))
        return {
            "accounts": [
                {"account": c["account"],
                 "email": (c.get("meta") or {}).get("email"),
                 "is_default": bool((c.get("meta") or {}).get("is_default")),
                 "services": ms_auth.services_granted((c.get("meta") or {}).get("scopes")),
                 "directory": (c.get("meta") or {}).get("tenant"),
                 **({"shared": c["shared"], "shared_owner": c["shared_owner"]}
                    if c.get("shared") else {})}
                for c in comptes
            ]
        }
