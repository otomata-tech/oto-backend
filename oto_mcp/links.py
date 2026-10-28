"""The links we give a user — and what they look like ON THEIR SIDE.

A partner's customer was receiving links to our dashboard: a product
they do not have. The first fix made the ADDRESS follow the tenant — insufficient,
and the partner's code proves it: **none of its paths look like ours**
(`/network/<org>/knowledge/<id>` where we serve `/docs/<id>`), and for some
of our views it has **no equivalent** — its tables do not exist. Gluing our
paths under its domain would thus have manufactured dead links, which is worse than a
link to our brand: a dead link cannot be diagnosed, it can only be endured.

Hence one pattern per link TYPE, declared by the tenant, and a simple rule:

    no pattern ⟹ no link.

**Two families, and confusing them breaks a flow**:

- a **DISPLAYED** link (a table, a shared page) may not exist: we write
  nothing rather than send somewhere. `link_for` then returns `None`, and the caller
  omits the link — it always has enough to be useful without it.
- a **REDIRECT** (the return from an OAuth connection) must ALWAYS land: we
  cannot "not redirect". Without a pattern, it falls back to ours — the user
  sees our brand once, which beats a blank page in the middle of a
  connection. That is `redirect_for`, and it is the only path that falls back this way.

⚠️ **Updated 10/09/2026 (oto#63).** The partner described above now has
a table page; until its tenant row declares it, its accounts
receive `null`. A BARE `null` said "not found" to whoever read it — hence
`raison_sans_lien`, which says why the address is missing, and `patron_reclame`, which
only charges an expensive parameter (the caller's org) to the product that requires it.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from . import config

logger = logging.getLogger(__name__)

# The paths of THIS instance's dashboard, when it is not the one `DEFAULT_PATHS`
# describes. An instance whose dashboard routes everything under the org
# (`/org/{org}/tables/{id}`) was handing out `/data/{id}` for every table: an address
# its dashboard does not route, so every table link in an agent's report led nowhere.
# Declared per INSTANCE (the convention of `docs/tenants.md`: nothing new per tenant),
# as a JSON object `{kind: path}` merged over `DEFAULT_PATHS`; absent = our paths.
ENV_INSTANCE_PATHS = "OTO_DASHBOARD_LINK_PATHS"

# Our own paths, by type. Single source: what the `oto` tenant serves, and the
# default of any tenant that declares nothing. `{…}` = the link's named parameters.
DEFAULT_PATHS: dict[str, str] = {
    "home": "",
    "table": "/data/{id}",
    "public_doc": "/p/d/{token}",
    # ⚠️ Said `/docs/{id}` from 2026-08-13 (41e7928) to 2026-08-28 — a path that
    # OUR dashboard does not route: its router only knows the `/documents` section
    # (without an id), and its catch-all sends any unknown path to
    # `/overview`. The link would thus not have shown an error: it would have opened the
    # home page while passing itself off as the requested page. It never had
    # a caller, which kept it invisible — and it is exactly the dead link
    # this module exists to forbid, planted at home. The REAL path of a page,
    # the one the front itself writes (`searchNav.ts`,
    # `ProjectDetailView`), opens it INSIDE its project: a page has no screen of its own.
    # Consequence: this pattern requires `project_id`, and a call that does not pass it
    # returns no link (`_render` guard) — never an address with holes.
    "doc": "/projects/{project_id}?doc={id}",
    "project": "/projects/{id}",
    # The dashboard's billing area (`/org/billing`, cf. oto-dashboard's
    # router) — that is where invoices are downloaded. A tenant that
    # does not declare this pattern does not have this view: the e-mail then goes out without a button,
    # rather than with a dead link.
    "billing": "/org/billing",
    "connectors": "/connectors",
    "connector_return": "/connectors?connector={connector}",
    "marketplace": "/connectors?tab=marketplace",
}

# Link kinds an instance may DECLARE that our own dashboard does not have: no default
# path, so `link_for` returns None until the instance's `OTO_DASHBOARD_LINK_PATHS`
# names one — the "no pattern ⟹ no link" rule, applied to a view only some fronts
# have. `app` = an app opened full screen (`oto_apps`), `/org/{org}/apps/{id}` on
# Tulina's front.
DECLARED_ONLY_KINDS = frozenset({"app"})


class InstancePathsInvalid(ValueError):
    """`OTO_DASHBOARD_LINK_PATHS` cannot be read: not JSON, not an object of strings
    `{kind: path}`, or a kind that `DEFAULT_PATHS` does not know."""


def instance_paths() -> dict[str, str]:
    """The paths of this instance's dashboard: `DEFAULT_PATHS`, overridden kind by
    kind by `OTO_DASHBOARD_LINK_PATHS`.

    Read at each call (like `config.dashboard_url`), so a test or a restart sees the
    declaration as it stands. An unreadable declaration RAISES `InstancePathsInvalid`,
    and `verifier_reglages` reads it at startup so that the instance refuses to start
    rather than serve links built from a guess. A kind that `DEFAULT_PATHS` does not
    know is refused too: no caller would ever ask for it, so it can only be a typo."""
    raw = os.environ.get(ENV_INSTANCE_PATHS, "").strip()
    if not raw:
        return DEFAULT_PATHS
    try:
        declared = json.loads(raw)
    except ValueError as exc:
        raise InstancePathsInvalid(f"{ENV_INSTANCE_PATHS} is not JSON: {exc}") from exc
    if not isinstance(declared, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in declared.items()):
        raise InstancePathsInvalid(
            f"{ENV_INSTANCE_PATHS} must be a JSON object of strings {{kind: path}}")
    inconnus = sorted(set(declared) - set(DEFAULT_PATHS) - DECLARED_ONLY_KINDS)
    if inconnus:
        raise InstancePathsInvalid(
            f"{ENV_INSTANCE_PATHS} declares unknown link kind(s) {inconnus}; known: "
            f"{sorted(set(DEFAULT_PATHS) | DECLARED_ONLY_KINDS)}")
    return {**DEFAULT_PATHS, **declared}


def verifier_reglages() -> None:
    """The startup guard: `OTO_DASHBOARD_LINK_PATHS` is read once, and an unreadable
    declaration refuses to start, naming it — never a silent fallback to our paths."""
    instance_paths()


def _tenant_of(sub: Optional[str]):
    """The registry entry of this account's tenant, or None (primary tenant included)."""
    if not sub:
        return None
    try:
        from . import tenancy
        registre = tenancy.current()
        # `entry_for_slug` returns None for the primary tenant as for an unknown
        # slug — the distinction served no one here, and redoing it by
        # hand was the second copy of a lookup that has only one meaning.
        return registre.entry_for_slug(registre.tenant_of(sub))
    except Exception:  # noqa: BLE001 — a link never breaks a call
        logger.warning("tenant resolution impossible for a link (fail-open)",
                       exc_info=True)
        return None


def _render(base: str, path: str, params: dict) -> Optional[str]:
    """Assemble base + pattern. A missing parameter CANCELS the link rather than
    producing an address with holes (`/network//knowledge/12`), which would lead to an
    error page while passing itself off as a valid link."""
    try:
        rendu = path.format(**{k: v for k, v in params.items() if v is not None})
    except (KeyError, IndexError):
        logger.warning("link not rendered: pattern %r expects a missing parameter", path)
        return None
    if "{" in rendu or "//" in rendu.lstrip("https:").lstrip("/"):
        return None
    return f"{base}{rendu}" if rendu else base


def link_for(kind: str, *, sub: Optional[str] = None, **params: Any) -> Optional[str]:
    """The link of type `kind` to SHOW this account, or **None** if it does not exist
    on its side.

    `None` is not an error: it is the right answer when the user's product
    does not have this view. The caller then writes its answer without a link.
    """
    entry = _tenant_of(sub)
    if entry is None:
        chemin = instance_paths().get(kind)
        return None if chemin is None else _render(config.dashboard_url(), chemin, params)

    patrons = getattr(entry, "link_paths", None) or {}
    if kind not in patrons:
        return None                       # the tenant does not have this view: no link
    base = (entry.dashboard_url or "").rstrip("/")
    if not base:
        return None                       # a pattern without an address leads nowhere
    return _render(base, str(patrons[kind]), params)


def ou_poser_la_cle(sub: Optional[str], *, org: Any = None,
                    connecteur: Optional[str] = None) -> str:
    """The complement " at <connectors page> (connector X)" of a sentence that says WHERE
    to set a key — or an EMPTY string when the account's product declares no connectors
    page. The sentence stays true without it: "set your own key" rather than
    "set your own key at <a page that does not exist>".

    ⚠️ Lived on 2026-09-11 (oto-backend#935): credential refusals
    (`access/resolve.py`) and the connector card (`connectors/readiness.py`) glued
    `/account` — OUR path — under the account's tenant address. On the only
    declared third-party tenant, that page answers 404: it is exactly the dead link this module
    forbids. The path now comes from the tenant's `connectors` pattern; a tenant that
    does not declare it receives NO address, never ours.

    `org` is only read by a pattern that requires it (`/org/{org}/connectors`); when absent,
    the link is cancelled rather than rendered with holes (cf. `_render`)."""
    url = link_for("connectors", sub=sub, org=org)
    if not url:
        return ""
    return f" at {url}" + (f" (connector {connecteur.capitalize()})" if connecteur else "")



def connector_setup_link(connector: str, *, sub: Optional[str] = None, org: Any = None,
                         **params: Any) -> Optional[str]:
    """`<connectors page>/<connector>?<params>` — the page where a person fills in a
    connector's card themselves, pre-filled with NON-secret values only (the key is
    typed in the form, never carried by a link). Built on the account's own
    `connectors` pattern, so it lands in ITS product; `None` when that product
    declares no connectors page (cf. `link_for`). Empty params are dropped."""
    base = link_for("connectors", sub=sub, org=org)
    if not base:
        return None
    p = urlsplit(base)
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return urlunsplit((p.scheme, p.netloc, f"{p.path.rstrip('/')}/{quote(connector)}",
                       query, ""))

# The NAME of the object, to tell a human what has no address.
_NOMS = {"table": "table", "doc": "page", "project": "project",
         "public_doc": "public page", "connectors": "connectors"}


def table_lit_sa_vue(sub: Optional[str]) -> bool:
    """Does the table page this account opens read a filtered VIEW from its address
    (`?f=`, `q`, `sort`/`dir` — `datastore/vue_par_url.py`)?

    Only OUR dashboard's is known to: its table page reads them on load (oto-app,
    `DatastoreTable.vue::readTableQuery`). A tenant's product, or an instance that
    declares its own table path, may route the same parameters nowhere — and a link that
    claims a filter its page ignores shows the whole table while passing itself off as the
    view. So: no tenant entry, and the instance's table path is ours."""
    return (_tenant_of(sub) is None
            and instance_paths().get("table") == DEFAULT_PATHS["table"])


def _chemin_pour(kind: str, sub: Optional[str]) -> Optional[str]:
    entry = _tenant_of(sub)
    if entry is None:
        return instance_paths().get(kind)
    return (getattr(entry, "link_paths", None) or {}).get(kind)


def patron_reclame(kind: str, param: str, *, sub: Optional[str] = None) -> bool:
    """Does this account's link pattern for `kind` carry `{param}`?

    Used to pay for an expensive parameter ONLY when the product that will receive the link
    requires it: on our side a table opens by its id alone, and resolving the
    caller's org for nothing on every row of a list would be one query per row."""
    chemin = _chemin_pour(kind, sub)
    return bool(chemin) and "{" + param + "}" in str(chemin)


def raison_sans_lien(kind: str, *, sub: Optional[str] = None, **params: Any) -> Optional[str]:
    """WHY `link_for` returns nothing — `None` if it does return a link.

    `None` is not an error (cf. the head of this module), but a BARE `null` was
    indistinguishable from a not-found object (oto#63): the caller hunted for a failure that
    does not exist, or concluded the object does not exist. Same branches as
    `link_for`, in the same order — one registry read, two phrasings."""
    if link_for(kind, sub=sub, **params) is not None:
        return None
    nom = _NOMS.get(kind, kind)
    entry = _tenant_of(sub)
    if entry is None:
        chemin = instance_paths().get(kind)
        if chemin is None:
            return f"no {nom} page exists in this product"
    else:
        patrons = getattr(entry, "link_paths", None) or {}
        if kind not in patrons:
            return (f"this account's product declares no {nom} page: there is "
                    f"no address to give. The {nom} does exist — it is "
                    "the address that is missing, not the object")
        if not (entry.dashboard_url or "").strip():
            return (f"this account's product declares a {nom} page, but no "
                    "base address: no link can be built")
        chemin = str(patrons[kind])
    poses = {k for k, v in params.items() if v is not None}
    manquants = sorted(set(re.findall(r"\{(\w+)\}", str(chemin))) - poses)
    if manquants:
        return ("this product's address requires "
                + ", ".join(f"`{m}`" for m in manquants) + ", which this context does not carry"
                + (" — pass `_org=` to set it" if "org" in manquants else ""))
    return "no address could be built: the declared pattern is unreadable"


def redirect_for(kind: str, *, sub: Optional[str] = None, **params: Any) -> str:
    """The link of type `kind` we REDIRECT to. Always an address.

    Used on return from an OAuth consent: at that moment the browser MUST
    land somewhere. Without a pattern on the tenant, we serve ours — seeing our
    brand once beats a blank page in the middle of a connection.
    """
    return (link_for(kind, sub=sub, **params)
            or _render(config.dashboard_url(), instance_paths().get(kind, ""), params)
            or config.dashboard_url())
