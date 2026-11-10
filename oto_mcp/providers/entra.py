"""Registry declaration of the `entra` connector — the Microsoft 365 directory (Microsoft
Entra ID) — read, and three changes — on the Microsoft 365 account.

Sole home of its entry: `providers/__init__.py` AGGREGATES it. The shape common to
the Microsoft services lives with the account carrier (`providers/microsoft.service`) —
here, what distinguishes THIS one.
"""
from __future__ import annotations

from .microsoft import service

# entra: the organization's directory via Microsoft Graph, ON BEHALF OF THE PERSON —
# scope `DIRECTORY` (users and licences, groups and members, sign-ins, sites) read; only
# licences, enabled accounts and group members written.
# Its scopes need a tenant administrator's approval, never included in the default
# « approve oto for the whole organization » (`auth/microsoft.ADMIN_ONLY_SERVICES`); the
# person's own Microsoft role still bounds what Graph returns.
CONNECTOR = service(
    "entra",
    label="Microsoft 365 directory",
    help="your organization's Microsoft 365 directory, for its administrators: accounts, "
         "licences, groups and their members, sites; assign licences, enable or disable "
         "accounts, manage group members",
    href="https://learn.microsoft.com/graph/api/resources/users",
)

CATEGORY = "Product & data"
LOGO_DOMAIN = "microsoft.com"

DESCRIPTION = (
    "Your organization's Microsoft 365 directory, for its administrators: every account "
    "with its licences and last sign-in, the subscriptions bought and used, the groups, "
    "teams and mailing lists with their members, the SharePoint sites. It assigns or "
    "removes a licence, enables or disables an account, adds or removes a group member; "
    "it never creates or deletes an account, nor touches a password."
)
