"""Le compte Google — le porteur que les six services (gmail, drive, sheets,
calendar, tasks, chat) empruntent depuis le split du 2026-09-26.

Un seul outil, en lecture : `google_accounts` — les comptes connectés et, pour
chacun, les services qu'il a AUTORISÉS. C'est la question qui n'existait pas avant le
split (un consentement = les six scopes) et que chaque service pose désormais :
`drive_file` sur un compte qui n'a autorisé que Gmail est refusé en nommant la carte
à ouvrir. `gmail_list_accounts` reste (compat des procédures écrites) ; c'est ici que
« quels comptes, avec quels droits ? » se pose désormais.
"""
from __future__ import annotations

from fastmcp import FastMCP

from .. import access
from ..auth import google as google_oauth


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def google_accounts() -> dict:
        """List the connected Google accounts and the services each one has authorised.

        Returns {accounts: [{email, is_default, services}]} where `services` names
        the Google services this account consented to (gmail, drive, sheets,
        calendar, tasks, chat). A service missing from the list has not been
        authorised on that account yet: connect it from its own connector card.
        Use `email` as the `account` argument of the service tools; omit `account`
        to act on the default account.
        """
        sub = access.current_user_sub_or_raise()
        return {
            "accounts": [
                {"email": a.get("google_email"),
                 "is_default": a.get("is_default", False),
                 "services": google_oauth.services_granted(a.get("scopes"))}
                for a in google_oauth.list_accounts(sub)
            ]
        }
