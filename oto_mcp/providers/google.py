"""Registry declaration of the `google` connector — the Google ACCOUNT, carrier of the
credential for the six service connectors (split of 2026-09-26).

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.

Until the split, `google` carried six namespaces (gmail, tasks, calendar, sheets,
drive, chat): ONE card, ONE activation, ONE consent that requested all six
scopes at once — three of them RESTRICTED at Google (Gmail, Drive, Chat). A tenant
offering only Gmail and Drive still had to get Chat and Tasks verified, and a
user who only wanted their calendar handed over their mailbox. Same move as
the unipile split of 2026-08-28: each service is now a connector in its own
right — its card, its activation, its selection, its visibility, ITS consent
(its scopes only, as incremental authorisation on the same account) — and
borrows the account from here (`credential_of="google"`, see `service` below).

What the account keeps for itself: the vault (one row per address, the refresh
token, the OAuth client that issued it), the `/api/google/oauth/callback` callback, the
list of accounts and the default account. Its own consent requests all six
scopes under OUR app (the prior state, for a single-card dashboard) and
only the identity under a tenant's app — a partner never requests a
scope its Google project does not declare; its services add them one by one
(`auth/google.scopes_for`).
"""
from __future__ import annotations

from ._model import _c

# A single namespace since the split: `google_*` (the account). The other six became
# connectors — a namespace belongs to only ONE connector.
CONNECTOR = _c(
    "google", ["google"],
    # `byo_org` (2026-09-27): an org admin or team lead can entrust ONE account
    # to everyone (shared mailbox, team calendar) — filed under the org or team, resolved
    # after the member's own account (`auth/google._resolve_row`).
    auth_modes={"byo_user", "byo_org"},
    personal_session=True, secret_kind="oauth",
    # OAuth ⟹ the derivation would say mono; yet N consents = N accounts, and the
    # vault holds one row per address. Declared here, not in a cross-cutting list.
    cardinality="multi", account_axis_static=True,
    label="Google account",
    help="the Google account that Gmail, Drive, Sheets, Calendar, Tasks, Chat, BigQuery, "
         "Google Ads and YouTube borrow — each service connects from its own card",
    modules=("google",),
)

CATEGORY = "Comms"
PUBLISHER = "Google"
LOGO_DOMAIN = "google.com"

DESCRIPTION = (
    "Your Google account, via OAuth: the carrier that the Google services borrow. "
    "Each connected Google address becomes a distinct account in the vault — "
    "several consents, several accounts — and each service (Gmail, Drive, "
    "Sheets, Calendar, Tasks, Chat, BigQuery, Google Ads, YouTube) is authorised from its own card, with only its scopes."
)


def service(name: str, *, label: str, help: str, href: str,
            modules: tuple[str, ...] | None = None):
    """A Google SERVICE connector — one card per service, on the shared account.

    What it shares with the other five — the auth mode, the credential delegation,
    the multi-account cardinality, the publisher — is described HERE, at the account
    carrier, because it is a property of the ACCOUNT and not of the service. Copying
    it six times gives five chances for it to diverge (same reason as
    `unipile.channel`).

    The service OWNS nothing: `credential_of="google"` points vault and accounts at
    the carrier (`providers.credential_provider`). What it owns for itself is
    what is governed per service — activation, selection, visibility of its tools — and
    ITS consent: `auth/google.SERVICE_SCOPES[name]`, and nothing else.

    `href` is the SERVICE's: what the person authorises is their Gmail or their
    Drive. `publisher` stays Google — the publisher names who receives the call."""
    return _c(
        name, [name],
        auth_modes={"byo_user", "byo_org"},
        personal_session=True, secret_kind="oauth",
        credential_of="google",
        cardinality="multi", account_axis_static=True,
        publisher="Google",
        label=label, help=help, href=href,
        modules=modules or (name,),
    )
