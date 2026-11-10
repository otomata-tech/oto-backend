"""Registry declaration of the `microsoft` connector — the Microsoft 365 ACCOUNT, carrier
of the credential for the Microsoft service connectors.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.

Same shape as the Google account (`providers/google.py`): each Microsoft service
(`auth/microsoft.SERVICE_SCOPES`: SharePoint & OneDrive, Outlook, Outlook Calendar,
Teams, OneNote, the directory) is a connector in its own right (its card, its activation, its selection, ITS
consent: its scopes only, added to the same Entra consent of the account) and borrows
the account from here (`credential_of="microsoft"`, see `service` below).

What the account keeps for itself: the vault (one row per linked Microsoft account,
the refresh token, the directory it signs in to — a person's, or the org's or a team's when
an admin connected it on their behalf), the `/api/microsoft/oauth/callback`
callback, the coordinates of oto's Entra application (`connector_settings`, platform
scope), the list of accounts and the default account. Its own consent requests the
identity only: each service adds its scopes from its card (`auth/microsoft.scopes_for`).
The application is oto's own, multi-tenant: the client registers none. The Graph
host is fixed: no egress guard to set.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "microsoft", ["microsoft"],
    # `byo_org`: an org admin or a team lead connects an account ON BEHALF of the org or
    # team (`auth/microsoft.scope_target`), served to its members after their own.
    auth_modes={"byo_user", "byo_org"},
    personal_session=True, secret_kind="oauth",
    # OAuth ⟹ the derivation would say single; yet a person links several Microsoft
    # accounts (their directory, a client's) and the vault holds one row per account
    # (`auth/microsoft.persist_grant`). Same provider reason as google.
    # `_account` announced STATICALLY, on the carrier and on every service (`service`
    # below), like google: the dynamic announcement counts vault rows under the carrier
    # and never sees a loan, so a borrower — whose lent account serves ONLY when
    # `_account` names it — would never see the axis to name it.
    cardinality="multi", account_axis_static=True,
    label="Microsoft 365 account",
    help="the Microsoft 365 account that the Microsoft services borrow — each service "
         "connects from its own card",
    href="https://learn.microsoft.com/graph/overview",
)

CATEGORY = "Workspace"
PUBLISHER = "Microsoft"
LOGO_DOMAIN = "microsoft.com"

DESCRIPTION = (
    "Your Microsoft 365 account, via OAuth: the carrier that the Microsoft services "
    "borrow. Each linked Microsoft account is a distinct account in the vault, and each "
    "service is authorised from its own card, with only its permissions. An org admin or "
    "a team lead can connect one on behalf of the whole org or team."
)


def service(name: str, *, label: str, help: str, href: str,
            modules: tuple[str, ...] | None = None):
    """A Microsoft SERVICE connector — one card per service, on the shared account.

    What it shares with the other services — the auth mode, the credential delegation,
    the multi-account cardinality and its static `_account` axis, the publisher — is
    described HERE, at the account
    carrier, because it is a property of the ACCOUNT, not of the service (same reason
    as `google.service`).

    The service OWNS nothing: `credential_of="microsoft"` points vault and accounts at
    the carrier (`providers.credential_provider`). What it owns is what is governed per
    service — activation, selection, visibility of its tools — and ITS consent:
    `auth/microsoft.SERVICE_SCOPES[name]`, and nothing else."""
    return _c(
        name, [name],
        auth_modes={"byo_user", "byo_org"},
        personal_session=True, secret_kind="oauth",
        credential_of="microsoft",
        cardinality="multi", account_axis_static=True,
        publisher="Microsoft",
        label=label, help=help, href=href,
        modules=modules or (name,),
    )
