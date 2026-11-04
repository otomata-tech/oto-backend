"""Microsoft 365 — a person's connection, or the org's or a team's (OAuth 2.0, delegated
permissions).

The ACCOUNT carrier `microsoft` (`providers/microsoft.py`) holds the vault, the
callback and the application's coordinates; each SERVICE (`SERVICE_SCOPES`: `sharepoint`,
`outlook`, `outlook_calendar`, `teams`, `onenote`, `entra`) is a connector that borrows
it (`credential_of="microsoft"`) and asks for ITS scopes only — the Google pattern
(`auth/google.py`), written for Entra.

Flow hosted by oto, on the common pattern (`connectors/flow` + `auth/flow`):

1. "Sign in" on a card → `_start_flow_for(card)` returns the Microsoft dialog URL,
   asking for the identity plus the card's scopes (`scopes_for`), with a signed
   `state` that carries the identity, the card (`c`), the directory (`t`) and the tier
   (`s`, `g`: member, org or team);
2. Microsoft sends the browser back to `/api/microsoft/oauth/callback`
   (`api/microsoft.py`); the code there becomes a refresh token;
3. the refresh token goes to the vault under `microsoft`, MEMBER tier by default: the
   person acts with THEIR Microsoft 365 rights, no more and no less.

**An account of the org or of a team** (decision of 15/10/2026,
`docs/partage-de-compte.md` concept 1): an org admin (`scope=org`) or the lead of the
ACTIVE team (`scope=group`) connects an account ON BEHALF of it (`scope_target`, checked at
the start and again at the return). The row lives at the org or team tier, survives the
departure of whoever connected it, and serves the members WITHOUT being named, after their
own accounts: the cascade's team then org rungs (`byo_org`), each at the level its rules
give the member (no `lent_by`). Two accounts at the same tier and no `_account` = the cascade's named
ambiguity — no default is set by itself there, an admin picks one
(`oto_identity op=set scope=org|group`). The token carries every consent this Microsoft
account gave oto, the holder's personal ones included: the row keeps only what ITS
consents asked for, plus the administrator tier of those services (`_borne_partagee`),
and so does every renewal. Removed by its admin:
`DELETE /api/settings/api-keys/microsoft?scope=org|group&account=…`.

**Incremental consent.** Entra ACCUMULATES the consents of one account for one
application: authorizing Outlook after SharePoint yields ONE refresh token that
knows both, on the same vault row (recognized by its `microsoft_id`). `meta.scopes`
is the normalized union of what was granted (`scopes.normalize`); it is refreshed
from `grant.scope` at every renewal — an administrator's consent given later is seen
there without reconnecting. `services_granted(meta.scopes)` says which services an
account has authorized; a service tool refuses an account that has not authorized
ITS service, naming the card to open — never a mute 403 from Graph.

**Renewal.** An access token lives one hour; `access_token_for` requests it again
with `scopes.REFRESH` (`.default`: everything already consented) — never a list,
which would fail with AADSTS65001 as soon as it names a scope that was not
consented, and be taken for a dead grant. ⚠️ Entra ROTATES the refresh token: the new
one replaces the old one in the vault at every renewal, on THIS account's row. The
access token never goes to the database: it lives in a process cache, keyed by the
hash of the row and of its refresh token (a reconnection invalidates it by itself).
A dead authorization marks THIS account's row, not the others.

**The directory (`tenant`).** By default, the dialog signs in on `organizations`: any
work or school account, in its home directory. A person who is a GUEST of a client's
directory (a personal Microsoft account, or another company's account) must sign in
on THAT directory: the flow takes an optional `tenant` value (the client's domain, its
directory ID, or the address of one of its SharePoint sites, `normalize_tenant`),
carried by the signed state, used for the dialog and the code exchange, and kept in
`meta.tenant` — the renewal goes back to that same directory.

**Multiple accounts** (oto-backend#23): one vault row PER ACCOUNT, `account` = its
lowercase address (followed by the directory in brackets for a connection on a
client's directory), the Microsoft account (`id` from `/me`) in meta. The rest is the
GENERIC mechanics of multi-account connectors (`cardinality="multi"`): choice via
`_account=`, default via `oto_identity(op='set')`, refusal that otherwise names the
accounts (`access.resolve_credential`), removal via
`DELETE /api/settings/api-keys/microsoft?account=…`.

**Another person's account serves only when the call names it** (`_account=`, or an
explicit `_instance=`), at the level its rules give, for the services the rule covers
(#1499, `access/comptes.ChoixDuParcours.autrui_nomme`): never implicitly. Every response
says which account served and, when it is another person's, whose
(`_account` + `lent_by`, `middleware/call_context._echo_account`). The rule is the
COMMON one, resolved by `access.resolve_credential` like every connector.

**Administrator approval.** Many organizations forbid a person to consent alone:
`admin_consent_url` builds the link that a client's administrator opens to approve
oto's permissions for their whole directory (state of a dedicated audience, valid
seven days), served by the capability `me.microsoft_admin_consent`
(`POST /api/me/connectors/microsoft/admin-consent`, tool `microsoft_admin_consent`). Its
return writes nothing (`api/microsoft._retour_approbation`).

⚠️ The coordinates of oto's Microsoft application (multi-tenant, registered in the
operator's directory) live in the database, PLATFORM scope of `connector_settings`,
connector `microsoft`, set by the operator.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import NamedTuple, Optional
from urllib.parse import urlsplit

from .. import credentials_store, status_hints
from ..connectors import flow as connector_flow
from ..connectors import health as connector_health
from ..connectors import link as connector_link
from . import flow as oauth_flow

logger = logging.getLogger("oto_mcp.auth.microsoft")

#: The ACCOUNT carrier: vault, callback, application coordinates.
CONNECTOR = "microsoft"

#: Each service → the name of its scopes in the lib (`oto.tools.microsoft.scopes`).
#: Its flow, link state and identity backend are derived from the REGISTRY
#: (`credential_of`): a service whose card is not declared (`providers/`) has none.
SERVICE_SCOPES: dict[str, str] = {
    "sharepoint": "FILES",
    "outlook": "MAIL",
    "outlook_calendar": "CALENDAR",
    "teams": "TEAMS",
    "onenote": "NOTES",
    "entra": "DIRECTORY",
}
SERVICES: tuple[str, ...] = tuple(SERVICE_SCOPES)
SERVICE_LABELS = {"sharepoint": "SharePoint & OneDrive", "outlook": "Outlook",
                  "outlook_calendar": "Outlook Calendar", "teams": "Teams",
                  "onenote": "OneNote", "entra": "Microsoft 365 directory"}
#: The services whose OWN scopes need a tenant administrator (the organization's
#: directory): an approval link covers them only when asked for by name, or from their
#: card — never in the default « approve oto for the whole organization », which would
#: otherwise open the directory to every member who connects a card.
ADMIN_ONLY_SERVICES: frozenset[str] = frozenset({"entra"})
#: A service's ADMINISTRATOR tier: the lib's scopes that no card requests at connection
#: (only a tenant administrator can grant them, `admin_consent_url`), what they open,
#: checked at use by the service's tools (`has_scopes`). Teams: reading channel messages.
SERVICE_ADMIN_TIER: dict[str, tuple[str, str]] = {
    "teams": ("TEAMS_ADMIN", "reading channel messages"),
}

_AUD = "microsoft"
#: The administrator's approval: its own audience — a person's state is never worth an
#: approval, nor the reverse — and seven days of validity, the time for the link to
#: reach the client's administrator and be opened.
_AUD_ADMIN = "microsoft_admin"
ADMIN_LINK_TTL = 7 * 24 * 3600
_CALLBACK_PATH = "/api/microsoft/oauth/callback"

#: The default directory: any work or school account, in its home directory.
_ANNUAIRE_PAR_DEFAUT = "organizations"

#: `client_secret` carries the `_secret` suffix that the admin console masks.
_REGLAGES = ("client_id", "client_secret")

_COMMANDE = ('oto_admin_connector_setting(op="set", connector="microsoft", '
             'key="<key>", value="<value>")')

# {opaque key: (access_token, epoch expiry)} — never in the database.
_JETONS: dict[str, tuple[str, float]] = {}
_VERROU = threading.Lock()


class MicrosoftReauthRequired(RuntimeError):
    """The person's authorization is dead: they must reconnect."""


def _coeur():
    """oto-core's `oto.tools.microsoft`, imported AT CALL TIME — the connector stays
    mounted (and refuses, saying so) when the pinned tag does not carry it."""
    import importlib

    try:
        return importlib.import_module("oto.tools.microsoft")
    except ImportError as e:
        raise RuntimeError(
            "The Microsoft 365 connectors are not installed on this instance: their "
            "core lives in oto-core and the pinned tag does not ship it. This is an "
            "instance configuration issue, not your account — tell the operator. "
            f"Detail: {e}") from e


# --- scopes, per service -------------------------------------------------------

def _scopes():
    return _coeur().scopes


def service_scopes(service: str) -> tuple[str, ...]:
    """The lib's scopes of ONE service (full Graph URIs). An unknown service is
    refused: nothing here guesses a scope."""
    if service not in SERVICE_SCOPES:
        raise RuntimeError(f"\"{service}\" is not a known Microsoft service.")
    return tuple(getattr(_scopes(), SERVICE_SCOPES[service]))


def scopes_for(connector: str) -> tuple[str, ...]:
    """The scopes THIS consent requests: the identity, then the card's own —
    nothing more for the account (`microsoft`), whose services add theirs one by one."""
    identite = tuple(_scopes().IDENTITY)
    if connector == CONNECTOR:
        return identite
    return identite + service_scopes(connector)


def _normalises(scopes) -> frozenset:
    """A `scope` string (Entra's, or the one stored in `meta.scopes`) as short names."""
    if not scopes:
        return frozenset()
    return _scopes().normalize(scopes if isinstance(scopes, str) else " ".join(scopes))


def services_granted(scopes) -> list[str]:
    """The services whose scopes ALL appear in `scopes` (string or list) — what an
    account has actually authorized, in service order."""
    have = _normalises(scopes)
    if not have:
        return []
    short = _scopes().short
    return [svc for svc in SERVICES if short(service_scopes(svc)) <= have]


def has_scopes(meta: dict, scopes: tuple[str, ...]) -> bool:
    """Does this account (its vault `meta`) hold ALL these scopes? For a permission
    that no card requests at connection — a service's administrator tier
    (`admin_scopes`), granted by a tenant administrator (`admin_consent_url`) and
    checked at use by the service's tools."""
    return _scopes().short(tuple(scopes)) <= _normalises((meta or {}).get("scopes"))


def admin_scopes(service: str) -> tuple[str, ...]:
    """The lib's scopes of `service`'s administrator tier — none for a service that
    has no such tier."""
    if service not in SERVICE_ADMIN_TIER:
        return ()
    return tuple(getattr(_scopes(), SERVICE_ADMIN_TIER[service][0]))


def _union(ancien, nouveau) -> str:
    """The normalized union of two `scope` strings, sorted — what `meta.scopes` stores."""
    return " ".join(sorted(_normalises(ancien) | _normalises(nouveau)))


# --- the directory -------------------------------------------------------------

_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DOMAINE = re.compile(r"^(?=.{4,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                      r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
# `<x>.sharepoint.com`, `<x>-my.sharepoint.com` (OneDrive), `<x>-admin.sharepoint.com`.
_SHAREPOINT = re.compile(r"^([a-z0-9][a-z0-9-]*?)(?:-my|-admin)?\.sharepoint\.com$")
_ATTENDU = ("expected the client's Microsoft domain (contoso.onmicrosoft.com or "
            "contoso.com), its directory ID, or the address of one of its SharePoint sites")


def normalize_tenant(valeur: Optional[str]) -> Optional[str]:
    """The directory a connection signs in to, from what the person typed — `None` for
    the default directory (`organizations`). Raises `ValueError` (message to show).

    A SharePoint address (`https://contoso.sharepoint.com/sites/…`) gives
    `contoso.onmicrosoft.com`: a CONVENTION — the initial domain of a Microsoft 365
    directory is named like its SharePoint host — true almost always, not always (a
    renamed tenant keeps its old host). If Entra does not know the domain, its
    dialog says so; the client's administrator gives the right domain or the directory
    ID (Entra admin center, Overview)."""
    v = (valeur or "").strip().lower()
    if not v:
        return None
    if "/" in v:
        hote = urlsplit(v if "://" in v else f"https://{v}").hostname or ""
        m = _SHAREPOINT.match(hote)
        if not m:
            raise ValueError(f"Client directory {valeur!r} not understood: {_ATTENDU}.")
        return f"{m.group(1)}.onmicrosoft.com"
    m = _SHAREPOINT.match(v)
    if m:
        return f"{m.group(1)}.onmicrosoft.com"
    if v == _ANNUAIRE_PAR_DEFAUT:
        return None
    if v in ("common", "consumers"):
        raise ValueError(
            f"Client directory {valeur!r} is not accepted: SharePoint, Outlook and Teams "
            f"need an organization's directory — {_ATTENDU}.")
    if _GUID.match(v) or _DOMAINE.match(v):
        return v
    raise ValueError(f"Client directory {valeur!r} not understood: {_ATTENDU}.")


def annuaire(tenant: Optional[str]) -> str:
    """The `tenant` the lib receives: the connection's directory, or `organizations`."""
    return tenant or _ANNUAIRE_PAR_DEFAUT


# --- the application's coordinates -------------------------------------------

def _reglages() -> dict:
    """Database read: at the start of a flow, on return from a consent, and
    at the RENEWAL of an access token (once per hour per person)."""
    from ..db import connector_settings as store

    return {r["key"]: (r["value"] or "").strip()
            for r in store.list_connector_settings()
            if r["scope_type"] == "platform" and r["connector"] == CONNECTOR
            and r["key"] in _REGLAGES}


def coordonnees_manquantes() -> list[str]:
    poses = _reglages()
    return [nom for nom in _REGLAGES if not poses.get(nom)]


def app() -> dict:
    """The instance's `{client_id, client_secret}`, or a refusal that NAMES what is missing."""
    manquantes = coordonnees_manquantes()
    if manquantes:
        raise RuntimeError(
            f"The Microsoft 365 connectors are not configured on this instance: missing "
            f"{', '.join(manquantes)}. Nothing to do on your account — the instance "
            f"operator sets them with {_COMMANDE}.")
    poses = _reglages()
    return {"client_id": poses["client_id"], "client_secret": poses["client_secret"]}


def app_disponible(sub: str) -> bool:
    del sub
    return not coordonnees_manquantes()


# --- the signed state --------------------------------------------------------

class EtatConnexion(NamedTuple):
    """What a person's state carries to the callback; `scope`/`group` = the tier written."""
    sub: str
    org: int
    app: str
    connector: str
    tenant: Optional[str]
    scope: str = "member"
    group: Optional[int] = None


class EtatApprobation(NamedTuple):
    """What an administrator's approval state carries to the callback."""
    sub: str
    org: int
    app: str
    connector: str
    services: tuple[str, ...]


def _cartes() -> tuple[str, ...]:
    return (CONNECTOR, *SERVICES)


def _ctx_org(sub: str) -> int:
    from .. import access  # lazy: avoids any import cycle at boot

    org = access.current_org(sub)
    if org is None:
        raise RuntimeError(
            "No org in context — cannot scope the Microsoft connection. Sign in "
            "again and retry.")
    return org


def make_state(sub: str, org_id: int, return_app: str = "", connector: str = CONNECTOR,
               tenant: Optional[str] = None, scope: str = "member",
               group: Optional[int] = None) -> str:
    """`connector` (`c`): WHICH card requested the consent — the callback returns
    there. `tenant` (`t`): the directory the dialog signed in to — the code is
    exchanged there, and the vault keeps it for the renewal. `scope` (`s`) and `group`
    (`g`): the tier the account is written at."""
    return oauth_flow.sign_state(_AUD, {"sub": sub, "org": org_id, "app": return_app,
                                        "c": connector, "t": tenant, "s": scope,
                                        "g": group})


def verify_state(state: Optional[str]) -> Optional[EtatConnexion]:
    """The state's content if it is valid and issued FOR this flow, otherwise None."""
    data = oauth_flow.read_state(_AUD, state)
    if not data:
        return None
    sub, org, return_app = data.get("sub"), data.get("org"), data.get("app")
    connector, tenant = data.get("c"), data.get("t")
    if not isinstance(sub, str) or not isinstance(org, int) or connector not in _cartes():
        return None
    if tenant is not None and not isinstance(tenant, str):
        return None
    scope, group = data.get("s") or "member", data.get("g")
    if scope not in _PALIERS or (scope == "group" and not isinstance(group, int)):
        return None
    return EtatConnexion(sub, org, oauth_flow.resolve_return_app(
        return_app if isinstance(return_app, str) else ""), connector, tenant, scope,
        group if scope == "group" else None)


def verify_admin_state(state: Optional[str]) -> Optional[EtatApprobation]:
    """An administrator's approval state (its own audience, seven days), or None."""
    data = oauth_flow.read_state(_AUD_ADMIN, state, ttl=ADMIN_LINK_TTL)
    if not data:
        return None
    sub, org, return_app = data.get("sub"), data.get("org"), data.get("app")
    connector, services = data.get("c"), data.get("s")
    if (not isinstance(sub, str) or not isinstance(org, int) or connector not in _cartes()
            or not isinstance(services, list) or not services
            or any(s not in SERVICES for s in services)):
        return None
    return EtatApprobation(sub, org, oauth_flow.resolve_return_app(
        return_app if isinstance(return_app, str) else ""), connector, tuple(services))


# --- starting the flow -------------------------------------------------------

def build_auth_url(sub: str, return_app: str = "", connector: str = CONNECTOR,
                   tenant: Optional[str] = None, scope: str = "member",
                   login_hint: Optional[str] = None) -> str:
    """The Microsoft dialog URL — for the account, or for ONE service (identity +
    its scopes, `scopes_for`), on the directory `tenant` (already normalized), for the
    tier `scope` (`scope_target`: raises `PermissionError`/`ValueError`). `login_hint`
    pre-fills the address (a reconnection: `reconnection_target`)."""
    group = scope_target(sub, scope)
    org_id = _ctx_org(sub)
    return _coeur().auth.authorize_url(
        app()["client_id"], oauth_flow.redirect_uri(_CALLBACK_PATH),
        make_state(sub, org_id, oauth_flow.resolve_return_app(return_app), connector,
                   tenant, scope, group),
        scopes=scopes_for(connector), tenant=annuaire(tenant), login_hint=login_hint)


def reconnection_target(sub: str, account: str) -> tuple[str, Optional[str]]:
    """The address and directory to reconnect the person's linked account `account`
    with: the dialog is pre-filled with ITS address, on ITS directory (a guest connected
    on a client's directory must come back there, or Entra serves another account). The
    callback recognizes the account by its Microsoft `id` (`persist_grant`): its row,
    default status and rules stay. Raises `LookupError` for an account she has not linked."""
    meta = next((c.get("meta") or {} for c in _comptes(_ctx_org(sub), sub)
                 if c["account"] == account), None)
    if meta is None:
        raise LookupError(f"No linked Microsoft account named `{account}` to reconnect.")
    return meta.get("email") or "", meta.get("tenant")


_PALIERS = ("member", "org", "group")


def scope_target(sub: str, scope: str) -> Optional[int]:
    """The team a connection at `scope` is written for (None for the person or the org).
    Raises `ValueError` for an unknown tier, `PermissionError` unless `sub` administers
    it: org admin for the org, lead of the ACTIVE team for the team (Google's rule)."""
    from .. import access
    if scope not in _PALIERS:
        raise ValueError(f"invalid scope: {scope!r} (expected 'member', 'org' or 'group')")
    if scope == "member":
        return None
    group = access.current_group(sub) if scope == "group" else None
    if scope == "group" and group is None:
        raise PermissionError("No active team: choose the team before connecting a "
                              "Microsoft account on its behalf.")
    _exiger_admin(sub, _ctx_org(sub), scope, group)
    return group


def _exiger_admin(sub: str, org_id: int, scope: str, group: Optional[int]) -> None:
    from .. import roles
    if scope == "org" and not roles.is_org_admin(sub, org_id):
        raise PermissionError("Only an organization admin connects a Microsoft account "
                              "on behalf of the whole organization.")
    if scope == "group" and not roles.can_admin_group(sub, int(group)):
        raise PermissionError("Only a team lead connects a Microsoft account on behalf "
                              "of the team.")


def admin_consent_url(sub: str, services: tuple[str, ...], tenant: Optional[str] = None,
                      return_app: str = "", connector: Optional[str] = None) -> str:
    """The link a client's ADMINISTRATOR opens to approve, for their whole directory,
    the permissions of `services`, their administrator tier included (`admin_scopes`:
    Teams' `TEAMS_ADMIN`, which reading channel messages needs). Nobody's token comes
    out of it: each person still connects from the card afterwards. `tenant` (normalized): the client's directory;
    without it, the administrator's own. `connector`: the card the answer returns to
    (default: the first service). Valid seven days (`ADMIN_LINK_TTL`)."""
    services = tuple(dict.fromkeys(services))
    inconnus = [s for s in services if s not in SERVICES]
    if not services or inconnus:
        raise ValueError(f"Unknown Microsoft service(s): {', '.join(inconnus) or '(none)'}"
                         f" — expected among {', '.join(SERVICES)}.")
    connector = connector or services[0]
    if connector not in _cartes():
        raise ValueError(f"\"{connector}\" is not a Microsoft card.")
    org_id = _ctx_org(sub)
    demandes = list(_scopes().IDENTITY)
    for svc in services:
        demandes += service_scopes(svc) + admin_scopes(svc)
    state = oauth_flow.sign_state(_AUD_ADMIN, {
        "sub": sub, "org": org_id, "app": oauth_flow.resolve_return_app(return_app),
        "c": connector, "s": list(services)})
    return _coeur().auth.admin_consent_url(
        app()["client_id"], oauth_flow.redirect_uri(_CALLBACK_PATH), state,
        scopes=tuple(dict.fromkeys(demandes)), tenant=annuaire(tenant))


def admin_approval(sub: str, services: Optional[list[str]] = None,
                   tenant: Optional[str] = None, return_app: str = "",
                   connector: str = "sharepoint") -> dict:
    """The administrator's approval link, as the card serves it: `{url, services,
    tenant, expires_at}`. `services` defaults to the Microsoft cards — those of
    `ADMIN_ONLY_SERVICES` only when the link is asked from their own card; `tenant` is
    what the person typed (domain, directory ID, SharePoint address — `normalize_tenant`),
    `organizations` when empty. Raises `ValueError` (unknown service, card or directory),
    `RuntimeError` (application not configured)."""
    choisis = tuple(services) if services else tuple(
        s for s in SERVICES if s not in ADMIN_ONLY_SERVICES or s == connector)
    annuaire_choisi = normalize_tenant(tenant)
    url = admin_consent_url(sub, choisis, annuaire_choisi, return_app, connector)
    expire = datetime.fromtimestamp(time.time() + ADMIN_LINK_TTL, tz=timezone.utc)
    return {"url": url, "services": list(dict.fromkeys(choisis)),
            "tenant": annuaire(annuaire_choisi), "expires_at": _iso(expire)}


_PARAM_TENANT = connector_flow.FlowParam(
    name="tenant", label="Client directory (optional)", required=False,
    help="for a guest account: the client's Microsoft domain (contoso.onmicrosoft.com "
         "or contoso.com), its directory ID, or the address of one of its SharePoint sites")


def _start_flow_for(connector: str):
    """The "connect" action of ONE card: the account, or a service bounded to its
    scopes, on the directory the person may name."""
    def start(ctx, values: dict) -> "connector_flow.FlowStart":
        from ..capabilities._types import AuthzDenied

        values = values or {}
        try:
            tenant = normalize_tenant(values.get("tenant"))
        except ValueError as e:
            raise AuthzDenied(400, "invalid_tenant", str(e))
        # `account` (hidden, like `scope`): « Reconnect » on the line of an account of
        # hers — the dialog targets that account, on its directory.
        login_hint = None
        if values.get("account"):
            if (values.get("scope") or "member") != "member":
                raise AuthzDenied(400, "invalid_scope",
                                  "Only an account of your own is reconnected by name.")
            try:
                login_hint, tenant = reconnection_target(ctx.sub, values["account"])
            except LookupError as e:
                raise AuthzDenied(404, "account_not_found", str(e))
        try:
            # `scope` is a HIDDEN key like `app` (Google, Salesforce): never a form field.
            return connector_flow.FlowStart(auth_url=build_auth_url(
                ctx.sub, values.get("app") or "", connector, tenant,
                values.get("scope") or "member", login_hint=login_hint))
        except PermissionError as e:
            raise AuthzDenied(403, "scope_forbidden", str(e))
        except ValueError as e:
            raise AuthzDenied(400, "invalid_scope", str(e))
        except RuntimeError as e:
            raise AuthzDenied(503, "oauth_misconfigured", str(e))
    return start


def declared_services() -> list[str]:
    """The services whose card the REGISTRY declares (`credential_of="microsoft"`)."""
    from .. import providers

    return [c.name for c in providers._REGISTRY_LIST
            if c.credential_of == CONNECTOR and c.name in SERVICE_SCOPES]


connector_flow.declare(
    CONNECTOR,
    start=_start_flow_for(CONNECTOR),
    params=(_PARAM_TENANT,),
    label="Link a Microsoft 365 account",
    callback_path=_CALLBACK_PATH,
    app_ready=app_disponible,
)
for _svc in declared_services():
    connector_flow.declare(
        _svc,
        start=_start_flow_for(_svc),
        params=(_PARAM_TENANT,),
        label=f"Authorize {SERVICE_LABELS[_svc]}",
        callback_path=_CALLBACK_PATH,
        app_ready=app_disponible,
    )


# --- the vault ---------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scope(org_id: int, sub: str, scope: str = "member",
           group: Optional[int] = None) -> tuple[str, str]:
    return credentials_store.entity_for_scope(scope, org_id, sub, group)


def _cause_entra(e: Exception) -> str:
    """Entra's reason for a refused refresh, as the lib reads it (`AADSTS…: <first line>`),
    bounded: `code` alone when the text says nothing more, a generic phrase without either."""
    texte = str(e).split("): ", 1)[-1].strip()
    code = getattr(e, "code", None)
    if code and code not in texte:
        texte = f"{code}: {texte}" if texte else code
    return texte[:300] or "expired, revoked, or the password changed"


def _cle(ligne: tuple, refresh_token: str) -> str:
    """Cache key: the row (entity AND account) and its refresh token, hashed
    (never a clear-text secret as a key). A reconnection changes the refresh token,
    hence the key."""
    return hashlib.sha256("|".join((*ligne, refresh_token)).encode()).hexdigest()


def _garder(ligne: tuple, grant) -> None:
    with _VERROU:
        _JETONS[_cle(ligne, grant.refresh_token)] = (
            grant.access_token, time.time() + int(grant.expires_in))


def _comptes(org_id: int, sub: str) -> list[dict]:
    """The Microsoft accounts linked by the person in this org (no secret)."""
    entity_type, entity_id = _scope(org_id, sub)
    return credentials_store.list_accounts(entity_type, entity_id, CONNECTOR)


def finish_connection(etat: EtatConnexion, code: str) -> dict:
    """The callback's work: the code against a grant, on the state's directory and
    with the card's scopes, then the vault at the state's tier. Synchronous (DB + HTTP).
    A shared tier is re-checked here: a right lost since the start writes nothing."""
    if etat.scope != "member":
        _exiger_admin(etat.sub, etat.org, etat.scope, etat.group)
    coordonnees = app()
    grant = _coeur().auth.exchange_code(
        coordonnees["client_id"], coordonnees["client_secret"], code,
        oauth_flow.redirect_uri(_CALLBACK_PATH),
        scopes=scopes_for(etat.connector), tenant=annuaire(etat.tenant))
    return persist_grant(etat.sub, etat.org, grant, tenant=etat.tenant, scope=etat.scope,
                         group=etat.group, connector=etat.connector)


def _borne_partagee(scopes, connector: Optional[str] = None) -> frozenset:
    """What a row of the org or a team may hold: its scopes, what THIS consent asks
    (`connector`), and the administrator tier of those services — never the holder's
    other consents, which the Entra token carries too."""
    permis = _normalises(scopes) | (_scopes().short(scopes_for(connector))
                                    if connector else frozenset())
    tiers = tuple(s for svc in services_granted(" ".join(permis)) for s in admin_scopes(svc))
    return permis | _scopes().short(tiers)


def persist_grant(sub: str, org_id: int, grant, tenant: Optional[str] = None,
                  scope: str = "member", group: Optional[int] = None,
                  connector: str = CONNECTOR) -> dict:
    """Stores the account that has just connected, ALONGSIDE the others: the refresh
    token is the secret (`secret_kind="oauth"`), the identity read from `/me` goes into
    `meta`, with the union of the granted scopes and the directory (`tenant`).

    The account is recognized by its Microsoft `id`, not by its row name: a
    reconnection of the same account replaces ITS row, even if renamed
    (`oto_identity(op='rename')`); another account creates one, named by its
    lowercase address — followed by the directory in brackets on a client's directory
    (a guest's address is often the one of their own linked account). The first
    linked account is the default (Google rule) — not at the org or team tier, where an
    admin picks it; a reconnection keeps the row's default status and clears its health
    mark. `scope`/`group`: the tier written (`connector` bounds a shared row's scopes)."""
    me = _coeur().FilesClient(grant.access_token).get_me() or {}
    microsoft_id = me.get("id")
    adresse = (me.get("mail") or me.get("userPrincipalName") or "").strip()
    if not microsoft_id or not adresse:
        raise RuntimeError(
            "Microsoft did not say which account just connected (`/me` without `id` "
            "or without address): nothing was saved.")
    entity_type, entity_id = _scope(org_id, sub, scope, group)
    comptes = credentials_store.list_accounts(entity_type, entity_id, CONNECTOR)
    deja = next((c for c in comptes
                 if (c.get("meta") or {}).get("microsoft_id") == microsoft_id), None)
    if deja is not None:
        account = deja["account"]
    else:
        account = adresse.lower() + (f" ({tenant})" if tenant else "")
        if any(c["account"] == account for c in comptes):
            # Another Microsoft account was renamed to this name: overwriting it
            # would lose its connection.
            raise RuntimeError(
                f"Another linked Microsoft account already has the name `{account}`: "
                "rename it (oto_identity op='rename', connector='microsoft') then "
                "reconnect this one. Nothing was saved.")
    ancien = (deja or {}).get("meta") or {}
    partage = scope != "member"
    # Entra accumulates the consents: what this grant says, plus what was known.
    scopes = _union(ancien.get("scopes"), grant.scope)
    if partage:
        scopes = " ".join(sorted(_normalises(scopes)
                                 & _borne_partagee(ancien.get("scopes"), connector)))
    meta = {**{k: v for k, v in ancien.items()
               if not k.startswith("health_") and k != "tenant"},
            "email": adresse,
            "name": me.get("displayName"),
            "microsoft_id": microsoft_id,
            "scopes": scopes,
            "connected_at": _iso(datetime.now(timezone.utc)),
            "is_default": (bool(ancien.get("is_default")) if deja
                           else not comptes and not partage)}
    if tenant:
        meta["tenant"] = tenant
    if meta["is_default"] and not deja:
        # Born default: its author and date (D3, `regles_de_comptes.marque_du_defaut`);
        # a reconnected default keeps its own, carried by `ancien`.
        from ..db import regles_de_comptes
        meta.update(regles_de_comptes.marque_du_defaut(sub))
    credentials_store.set_credential(entity_type, entity_id, CONNECTOR,
                                     grant.refresh_token, set_by=sub, meta=meta,
                                     account=account)
    _garder((entity_type, entity_id, account), grant)
    logger.info("microsoft: account %s (org=%s, tier=%s)",
                "reconnected" if deja else "added", org_id, scope)
    return {"account": account, "email": adresse, "name": meta["name"]}


def _ranger_rotation(ligne: tuple, lu: str, nouveau: str) -> None:
    """The refresh token has rotated: the new one replaces the old one on THIS
    account's row, its `meta` passed back as is (the upsert would overwrite it otherwise). Conditional
    write: a concurrent call that has already rotated holds the most
    recent value, we do not replace it with ours."""
    entity_type, entity_id, account = ligne
    row = credentials_store.get_credential_with_meta(entity_type, entity_id, CONNECTOR,
                                                     account=account)
    if not row or row.get("secret") != lu:
        return
    credentials_store.set_credential(entity_type, entity_id, CONNECTOR, nouveau,
                                     set_by=row.get("set_by"), meta=row.get("meta") or {},
                                     account=account)


def resolve_account(sub: str, service: str, account: Optional[str] = None):
    """`(ResolvedCredential, meta)` of the account a call to `service` designates.

    The account is chosen by the COMMON resolution of multi-account connectors
    (`access.resolve_credential`, under the SERVICE called): `account` or the call's
    `_account=`, otherwise the account pinned by the project (on the service's card,
    otherwise on the account's), otherwise the only linked account, otherwise the
    default account, otherwise a refusal that names the accounts (`McpError`).
    The person's OWN accounts first, then those of her active team, then of her org (the
    cascade's rungs, unnamed, each at the level the call requires); another person's
    account only when the call NAMES it, at the level its rules give."""
    from .. import access  # lazy: avoids any import cycle at boot

    if service not in SERVICE_SCOPES:
        raise RuntimeError(f"\"{service}\" is not a known Microsoft service.")
    rc = access.resolve_credential(service, want="byo", sub=sub, account=account or None)
    meta = next((a.get("meta") or {} for a in credentials_store.list_accounts(
        rc.entity_type, rc.entity_id, CONNECTOR) if a["account"] == (rc.account or "")),
        None)
    if meta is None:   # removed between the resolution and this read
        raise RuntimeError(f"The Microsoft account `{rc.account}` is no longer linked: "
                           "reconnect it from your connectors page.")
    return rc, meta


def access_token_for(sub: str, service: str, account: Optional[str] = None, *,
                     renew: bool = False) -> str:
    """A valid access token for the person, on the account the call designates
    (`resolve_account`), renewed if it expires in less than a minute.

    `renew=True` renews it whatever the cache holds: an administrator's approval given
    after the cached token was issued is only carried by a new one (and `meta.scopes`
    only learns it at a renewal). For a tool that has a reason to believe so — once per
    call, never in a loop.

    Refuses BEFORE any network call an account that has not authorized `service`,
    naming the card to open. Raises a `McpError` (no account, unknown account,
    ambiguity), `RuntimeError` (application not configured, service not authorized)
    or `MicrosoftReauthRequired` (dead authorization: THIS account's row is
    marked, the card says "needs reconnecting")."""
    rc, meta = resolve_account(sub, service, account)
    label = SERVICE_LABELS[service]
    if service not in services_granted(meta.get("scopes")):
        # The account exists but has not authorized THIS service: name the card to
        # open, not "reconnect" — Graph would answer a 403 without saying which.
        raise RuntimeError(
            f"The Microsoft account {rc.account} has not yet authorized {label}: "
            f"connect {label} from its card on your connectors page. Nothing was done.")
    ligne = (rc.entity_type, rc.entity_id, rc.account)
    refresh_token = rc.key
    with _VERROU:
        cached = _JETONS.get(_cle(ligne, refresh_token))
    if cached and cached[1] > time.time() + 60 and not renew:
        return cached[0]

    coeur = _coeur()
    coordonnees = app()
    try:
        grant = coeur.auth.refresh(coordonnees["client_id"], coordonnees["client_secret"],
                                   refresh_token, scopes=tuple(coeur.scopes.REFRESH),
                                   tenant=annuaire(meta.get("tenant")))
    except coeur.MicrosoftGrantExpired as e:
        # Entra's own words name the cause (AADSTS50173 password changed or sessions
        # revoked, AADSTS700082 inactivity…): without them, nobody can say afterwards
        # why an account was cut. First line only (the lib drops Trace ID and timestamp).
        cause = _cause_entra(e)
        logger.warning("microsoft: refresh refused for %s (%s)", rc.account, cause)
        message = (f"Microsoft no longer accepts the sign-in of `{rc.account}` "
                   f"(Entra: {cause}). Reconnect it with the « Reconnect » "
                   f"button on its line, card « {label} » of the connectors page: its "
                   "default setting and its rules are kept.")
        connector_health.mark_rejected(rc.entity_type, rc.entity_id, CONNECTOR,
                                       rc.account, message)
        raise MicrosoftReauthRequired(message) from e
    if grant.refresh_token != refresh_token:
        _ranger_rotation(ligne, refresh_token, grant.refresh_token)
    # `.default` returns everything consented TODAY: an administrator's approval given
    # since the connection appears here, with no reconnection.
    if grant.scope:
        connus = _normalises(grant.scope)
        if rc.entity_type != credentials_store.MEMBER:
            connus &= _borne_partagee(meta.get("scopes"))
        connus = " ".join(sorted(connus))
        if connus != meta.get("scopes"):
            credentials_store.update_meta(rc.entity_type, rc.entity_id, CONNECTOR,
                                          rc.account, {"scopes": connus})
    # A successful renewal clears THIS account's "needs reconnecting" mark.
    connector_health.record_health(CONNECTOR, ligne, True, None)
    _garder(ligne, grant)
    return grant.access_token


# --- what the cards display --------------------------------------------------

def shared_accounts(sub: str, org: Optional[int]) -> list[dict]:
    """The accounts of the person's ACTIVE team, then of her org — the cascade's order —
    with `shared` (`group`/`org`) and `shared_owner` (`{id}`, named by `avec_noms` where
    shown); never her default."""
    from .. import access  # lazy
    if org is None:
        return []
    equipe = access.current_group(sub)
    paliers = ([("group", int(equipe))] if equipe is not None else []) + [("org", int(org))]
    out = []
    for palier, cible in paliers:
        out += [{**c, "meta": {**(c.get("meta") or {}), "is_default": False},
                 "shared": palier, "shared_owner": {"id": cible}}
                for c in credentials_store.comptes_du_palier(palier, str(cible), CONNECTOR)]
    return out


def avec_noms(comptes: list[dict]) -> list[dict]:
    """`shared_owner` named (`{id, name}`) — only on the surfaces that show it, so that
    resolution and presence read no team or org name (#1148)."""
    from .. import group_store, org_store  # lazy
    out = []
    for c in comptes:
        if c.get("shared"):
            cible = c["shared_owner"]["id"]
            nom = ((group_store.get_group(cible) or {}).get("name") if c["shared"] == "group"
                   else org_store.nom_d_org(cible))
            c = {**c, "shared_owner": {"id": cible, "name": nom}}
        out.append(c)
    return out


def accounts_for(sub: str, service: Optional[str] = None) -> list[dict]:
    """The Microsoft accounts of the person in their context org, then those of her team
    and org (`shared`, `shared_accounts`) — ALL of them for the account, those that
    AUTHORIZED `service` for a service's card. Another person's account is not listed:
    it serves only named. Vault rows (`account`, `meta`, `set_at`), no secret. A name appears
    once, at the nearest level: the one a call naming it resolves."""
    from .. import access  # lazy

    org = access.current_org(sub)
    comptes = _comptes(org, sub) if org is not None else []
    vus = {c["account"] for c in comptes}
    for c in shared_accounts(sub, org):
        if c["account"] not in vus:
            vus.add(c["account"])
            comptes.append(c)
    if service is None:
        return comptes
    return [c for c in comptes
            if service in services_granted((c.get("meta") or {}).get("scopes"))]


def _morts(comptes: list[dict]) -> list[dict]:
    """Those whose authorization has lapsed (`health_ko`), to be reconnected one by one."""
    return [c for c in comptes if (c.get("meta") or {}).get("health_ko")]


def _link_state_for(service: Optional[str]):
    """Linked as soon as there is one account (that authorized the service, for a
    service's card); "needs reconnecting" if one of them does, naming it."""
    def read(sub: str) -> connector_link.LinkState:
        comptes = accounts_for(sub, service)
        if not comptes:
            return connector_link.LinkState(linked=False)
        morts = _morts(comptes)
        return connector_link.LinkState(
            linked=True, accounts=len(comptes),
            set_at=max((str(c.get("set_at") or "") for c in comptes), default="") or None,
            health_ko=True if morts else None,
            health_reason="; ".join(
                f"{c['account']}: {(c.get('meta') or {}).get('health_reason') or 'rejected'}"
                for c in morts) or None)
    return read


def _etape_manquante_for(service: Optional[str]):
    """What remains to be done — and by WHOM. Without application coordinates, it is
    not for the person to click "Sign in" in a loop. A dead account among several is
    named: the others keep serving."""
    def hint(sub: str, org, group, entry: dict) -> Optional[str]:
        del org, group, entry
        if coordonnees_manquantes():
            return "Microsoft app to be configured by the operator"
        if not accounts_for(sub):
            return "Sign in with Microsoft"
        comptes = accounts_for(sub, service)
        if not comptes:
            return f"Authorize {SERVICE_LABELS[service]}"
        morts = _morts(comptes)
        if morts:
            return f"Sign-in expired for {', '.join(c['account'] for c in morts)} — reconnect"
        if service in SERVICE_ADMIN_TIER:
            tier = admin_scopes(service)
            sans = [c["account"] for c in comptes
                    if not has_scopes(c.get("meta") or {}, tier)]
            if sans:
                pour = f" for {', '.join(sans)}" if len(comptes) > 1 else ""
                return (f"{SERVICE_ADMIN_TIER[service][1].capitalize()} requires your "
                        f"Microsoft admin's approval{pour}")
        return None
    return hint


for _carte in (CONNECTOR, *declared_services()):
    _svc_ou_rien = None if _carte == CONNECTOR else _carte
    connector_link.register(_carte, _link_state_for(_svc_ou_rien))
    status_hints.register(_carte, _etape_manquante_for(_svc_ou_rien))


# --- moving the vault from the old card ---------------------------------------

#: Until the carrier existed, the vault rows and the application's coordinates lived
#: under the `sharepoint` card.
_ANCIENNE_CARTE = "sharepoint"


def copier_depuis_sharepoint() -> dict:
    """Boot step: COPIES the vault rows and the application's coordinates of
    `sharepoint` to the carrier. Additive — the old rows stay, for the code still
    served on the shared base (prod and preprod) until the tag; their removal is a
    later lot. Idempotent: a copied row is marked (`credentials_store.copy_connector_rows`),
    the coordinates are only copied to a carrier that has none."""
    from ..db import connector_settings as store

    bilan = credentials_store.copy_connector_rows(_ANCIENNE_CARTE, CONNECTOR)
    bilan["settings"] = store.copy_platform_settings(_ANCIENNE_CARTE, CONNECTOR, _REGLAGES)
    if any(bilan.values()):
        logger.info("microsoft: vault copied from %s — %s", _ANCIENNE_CARTE, bilan)
    return bilan


def avertir_au_demarrage() -> None:
    """Says AT BOOT what will prevent the connectors from serving. Never raises."""
    try:
        manquantes = coordonnees_manquantes()
    except Exception as e:  # noqa: SILENT — at boot the database may not be ready
        logger.info("microsoft: configuration cannot be verified at startup (%s) — "
                    "the first flow will settle it.", type(e).__name__)
        return
    if manquantes:
        logger.warning(
            "microsoft: connectors mounted but NOT configured — %s missing. "
            "The \"Sign in\" button will refuse and say so. Set with: %s",
            ", ".join(manquantes), _COMMANDE)
