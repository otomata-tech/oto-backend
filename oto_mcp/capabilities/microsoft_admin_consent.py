"""Capability "get the link a Microsoft administrator opens to approve oto".

Many organizations forbid a person to authorize an application alone: the connection
then comes back `admin_required`, and the card offers this link — to copy and send to
the organization's Microsoft 365 administrator, or to open if one is an administrator.
The administrator approves, for their whole directory, the permissions of the chosen
Microsoft services; nobody's token comes out of it, each person then connects from the
card as usual. The answer comes back on the card: `?connector=<card>&connect=
admin_approved|admin_refused` (`api/microsoft._retour_approbation`).

The gesture lives with the connector (`auth/microsoft.admin_approval`); this capability
routes, guards and translates a refusal.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from ..auth import microsoft as ms_auth
from ._authz import ORG_MEMBER
from ._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class AdminConsentInput(BaseModel):
    services: Optional[list[str]] = Field(
        default=None, description="Microsoft services to approve (sharepoint, outlook, "
                                  "outlook_calendar, teams, onenote, entra, powerbi) — default: "
                                  "all of them but the directory (entra) and Power BI "
                                  "(powerbi), which are approved from their own card or "
                                  "by name only.")
    tenant: Optional[str] = Field(
        default=None, description="The organization's directory: its Microsoft domain "
                                  "(contoso.onmicrosoft.com, contoso.com), its directory "
                                  "ID, or the address of one of its SharePoint sites — "
                                  "default: the administrator's own directory.")
    app: Optional[str] = Field(default=None, description="Front key, for the return.")
    connector: Optional[str] = Field(
        default="sharepoint", description="The card the answer returns to.")


class AdminConsentLink(BaseModel):
    """The link is NOT an approval: it only becomes one when an administrator of the
    directory opens it and accepts. It is valid until `expires_at` (seven days)."""
    url: str
    services: list[str]
    tenant: str
    expires_at: str


def _lien(ctx: ResolvedCtx, inp: AdminConsentInput) -> dict:
    try:
        return ms_auth.admin_approval(ctx.sub, inp.services, inp.tenant, inp.app or "",
                                      inp.connector or "sharepoint")
    except ValueError as e:
        raise AuthzDenied(400, "invalid_admin_consent", str(e))
    except RuntimeError as e:
        raise AuthzDenied(503, "oauth_misconfigured", str(e))


CAPABILITIES += [
    Capability(
        key="me.microsoft_admin_consent",
        handler=_lien,
        Input=AdminConsentInput,
        authz=ORG_MEMBER,
        Output=AdminConsentLink,
        # Under ITS connector's namespace: the per-connector gate resolves on it.
        mcp="microsoft_admin_consent",
        errors=(DeclaredError(400, "invalid_admin_consent",
                              "unknown Microsoft service or card, or unreadable "
                              "directory — the message names it"),
                DeclaredError(503, "oauth_misconfigured",
                              "oto's Microsoft application is not configured on this "
                              "instance")),
        rest=RestBinding(verb="POST", path="/api/me/connectors/microsoft/admin-consent"),
        description=(
            "Get the link a Microsoft 365 ADMINISTRATOR opens to approve oto for their "
            "whole organization — needed when connecting a Microsoft service comes back "
            "'admin approval required'. Send the `url` to the organization's "
            "administrator (valid seven days); once approved, each person connects from "
            "the service's card. `services` default: all Microsoft services; `tenant`: "
            "the organization's domain, directory ID or a SharePoint address."),
    ),
]
