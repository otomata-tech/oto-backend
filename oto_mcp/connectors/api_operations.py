"""Where the `api` connector's described operations live, and where an import reads from.

The operations of one `api` credential live in `connector_instances.config
["operations"]` of THE instance that names its vault row — designated by the vault's
own quadruplet (owner type, owner id, `api`, account), never by anything else. The
two faces go through here: the agent's tools (`tools/api_ops.py`, the row the
cascade resolved for the call) and the org's REST routes
(`capabilities/connectors/api_operations.py`, the org's row for a named account).

Nothing here resolves a credential: the caller hands over the row (and, to read the
API's `doc_path`, its fields). The format and its validation are `custom_ops`; the
OpenAPI conversion is `custom_ops_openapi`.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import requests

from .. import (credentials_store, custom_ops, custom_ops_definition, custom_ops_openapi,
               egress, providers)
from ..db import connector_instances

logger = logging.getLogger(__name__)

CONNECTOR = "api"
CONFIG_KEY = "operations"
# An API description worth importing fits well under this; beyond, it is not read.
MAX_DOC_BYTES = 2 * 1024 * 1024
# (connect, read) for a PUBLIC document URL — bounded at this level (#892).
URL_TIMEOUT = (5, 20)


class InstanceMissing(LookupError):
    """No live instance names this vault row — no credential, or it was removed."""


class ImportFailed(ValueError):
    """The source could not be read as an OpenAPI/Swagger JSON document."""


#: The connector's name until 12/11/2026 (`providers.CONNECTEURS_RENOMMES`).
ANCIEN_NOM = "http"


def copier_depuis_http() -> dict:
    """Boot step, and `oto-mcp maintenance copies-du-coffre` (a target's gate plays it
    BEFORE migrating, `maintenance.copies_declarees`): COPIES the vault rows of the old
    name to `api`, each new instance
    keeping the old row's sharing and taking its instance's definition
    (`credentials_store.copy_connector_rows`, `renamed=True`). Additive — the old rows
    stay: prod and preprod share the base, and the code served before the tag still
    reads `http`; removing them is a later lot (`oto-mcp maintenance orphelins`).
    Idempotent: a copied row is marked. The rest — selections, kits, tool lists, and
    what designates an instance by its id (account rules, `grants`, project links) —
    moves in the data revision `20261112_1200_connecteur_api`, which raises if this copy
    has not been played."""
    bilan = credentials_store.copy_connector_rows(ANCIEN_NOM, CONNECTOR, renamed=True)
    if any(bilan.values()):
        logger.info("api: vault copied from %s — %s", ANCIEN_NOM, bilan)
    return bilan


def load(owner_type: str, owner_id: str, account: str = "") -> list[dict]:
    """The described operations of this vault row's instance (`[]` if none yet)."""
    inst = connector_instances.live_instance_config(owner_type, owner_id, CONNECTOR,
                                                    account)
    if inst is None:
        raise InstanceMissing(f"no live `api` instance for account `{account}`")
    ops = (inst["config"] or {}).get(CONFIG_KEY)
    return ops if isinstance(ops, list) else []


def _instance(owner_type: str, owner_id: str, account: str) -> dict:
    inst = connector_instances.live_instance_config(owner_type, owner_id, CONNECTOR,
                                                    account)
    if inst is None:
        raise InstanceMissing(f"no live `api` instance for account `{account}`")
    return inst


def _write(owner_type: str, owner_id: str, account: str, patch: dict) -> None:
    inst = _instance(owner_type, owner_id, account)
    if not connector_instances.merge_instance_config(inst["id"], patch):
        raise InstanceMissing(f"no live `api` instance for account `{account}`")


def save(owner_type: str, owner_id: str, account: str, ops: Any) -> list[dict]:
    """Validate (`custom_ops.OperationsInvalid` on refusal), then REPLACE the list."""
    clean = custom_ops.validate_operations(ops)
    _write(owner_type, owner_id, account, {CONFIG_KEY: clean})
    return clean


def definition_of(config: dict) -> dict:
    """The saved definition read from an instance's config, every key present."""
    config = config or {}
    ops = config.get(CONFIG_KEY)
    return {"label": config.get("label"), "description": config.get("description"),
            "logo_domain": config.get("logo_domain"),
            "headers": config.get("headers") or {},
            "variables": config.get("variables") or {},
            "operations": ops if isinstance(ops, list) else []}


def load_definition(owner_type: str, owner_id: str, account: str = "") -> dict:
    return definition_of(_instance(owner_type, owner_id, account)["config"])


def save_definition(owner_type: str, owner_id: str, account: str, definition: Any, *,
                    auth_header: Optional[str] = None) -> dict:
    """Validate the keys given (`custom_ops.OperationsInvalid`), write them — the
    keys NOT given stay as they are — and return the whole definition."""
    patch = custom_ops_definition.validate_definition(definition, auth_header=auth_header)
    if patch:
        _write(owner_type, owner_id, account, patch)
    return load_definition(owner_type, owner_id, account)


def _summary(account: str, config: dict, meta_label: Optional[str] = None) -> dict:
    d = definition_of(config)
    return {"account": account,
            "label": d["label"] or meta_label or account or "Custom API",
            "description": d["description"], "logo_domain": d["logo_domain"],
            "actions_count": len(d["operations"])}


#: Where a custom connector's key lives, as the lists say it: `member` = the CALLER's own
#: key in this org (personal tier, `byo_user`), `org` = the org's, shared by its members.
SCOPE_MEMBER, SCOPE_ORG = credentials_store.MEMBER, credentials_store.ORG


def _owner_connectors(owner_type: str, owner_id: str, scope: str) -> list[dict]:
    """One owner's custom connectors, one per `api` account. One query when the owner
    has no account, two otherwise; no decryption."""
    metas = {r["account"]: (r.get("meta") or {}).get("label") for r in
             credentials_store.list_accounts(owner_type, owner_id, CONNECTOR)}
    if not metas:
        return []
    out = []
    for inst in connector_instances.live_instance_configs(owner_type, owner_id, CONNECTOR):
        if inst["account"] not in metas:
            continue
        row = _summary(inst["account"], inst["config"], metas[inst["account"]])
        row["scope"] = scope
        row["operations"] = definition_of(inst["config"])["operations"]
        out.append(row)
    return out


def org_connectors(org_id: int, sub: Optional[str]) -> list[dict]:
    """The custom connectors the caller `sub` reaches in org `org_id`, in the
    resolution order: the caller's OWN keys first (`scope: "member"`, read only when the
    card declares `byo_user`), then the org's (`scope: "org"`). Each: `{account, label,
    description, logo_domain, actions_count, scope}` — and `operations` (the full list),
    for the caller that resolves an action by name. The member row is derived from
    `sub`, never from an argument: no caller lists a colleague's key. An account present
    at both scopes is listed twice; the personal one is the one a call uses. The label
    is the definition's, else the card's (`meta.label`), else the account."""
    if not org_id:
        return []
    mine = (_owner_connectors(credentials_store.MEMBER,
                              credentials_store.member_id(org_id, sub), SCOPE_MEMBER)
            if sub and providers.is_byo_user(CONNECTOR) else [])
    return mine + _owner_connectors(credentials_store.ORG, str(org_id), SCOPE_ORG)


def for_context(org_id: int, sub: Optional[str], connectors: list[str]) -> list[dict]:
    """The caller's custom connectors for the agent's context (`instructions`, « Ton
    contexte oto ») — a tool configured through Custom API, that the agent would not
    find in the catalogue: the caller's own, then the org's. At most 10; `[]` when
    `api` is not active. Fail-open, logged."""
    if CONNECTOR not in connectors or not org_id:
        return []
    try:
        return org_connectors(org_id, sub)[:10]
    except Exception:
        logger.warning("contexte : connecteurs personnalisés illisibles (omis)",
                       exc_info=True)
        return []


def context_line(c: dict) -> str:
    """One line of the agent's context per custom connector — the caller's own key
    says so: it is theirs alone, and the one used before the org's."""
    whose = (" (your personal key, used before the org's)"
             if c.get("scope") == SCOPE_MEMBER else "")
    return (f"- Custom connector{whose}: {c['label']} — "
            f"`api(op=\"list\", _account=\"{c['account']}\")`")


def match_org_connector(org_id: int, sub: Optional[str], name: str) -> Optional[dict]:
    """The caller's custom connector whose label (or account) IS `name`, case and
    surrounding spaces ignored — the caller's own first, then the org's — what
    `oto_connector(name=…)` returns for a tool configured through Custom API instead of
    "unknown connector"."""
    q = " ".join((name or "").lower().split())
    if not q or not org_id:
        return None
    for c in org_connectors(org_id, sub):
        if q in {" ".join(str(c["label"]).lower().split()), c["account"].lower()}:
            return {"connector": CONNECTOR, "account": c["account"], "label": c["label"],
                    "description": c["description"], "logo_domain": c["logo_domain"],
                    "actions_count": c["actions_count"], "scope": c["scope"],
                    "how_to_call": (f"api(op=\"list\", _account=\"{c['account']}\") "
                                    "lists its actions; api_act(op=\"call\", "
                                    "operation=…, args={…}) calls one.")
                                   + (" Your personal key: yours alone, used before "
                                      "the org's." if c["scope"] == SCOPE_MEMBER else "")}
    return None


def summaries_for_instances(ids: list[int]) -> dict[int, dict]:
    """`{instance id: {label, description, logo_domain, actions_count}}` — what the
    front's instance roster shows per custom connector. One query."""
    return {iid: {k: v for k, v in _summary("", cfg).items() if k != "account"}
            for iid, cfg in connector_instances.configs_for_instance_ids(ids).items()}


def with_constant_headers(client, headers: Optional[dict]):
    """Apply the definition's constant headers to an oto-core client.

    oto-core's `HttpConnectorClient` takes no `headers=` (follow-up: add that
    parameter in oto-core, then drop this). Until then the headers go on the
    client's session when it is first prepared — AFTER the auth configured it, and
    a definition can never name an authenticating header (`custom_ops_definition`),
    so the auth always wins. Coupled to one private hook, `_ready`, wrapped rather
    than replaced."""
    if not headers:
        return client
    ready = client._ready

    def _ready_with_headers():
        session = ready()
        for name, value in headers.items():
            session.headers.setdefault(name, value)
        return session
    client._ready = _ready_with_headers
    return client


def _parse(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        raise ImportFailed("the document is not JSON (YAML is not read: export the "
                           "OpenAPI description as JSON)") from None


def fetch_public_document(url: str) -> Any:
    """GET a PUBLIC document URL: no auth sent, egress guard without the named
    exceptions (an address typed by a person or an agent — `egress.check_url`),
    no redirect followed, size and time bounded."""
    try:
        egress.check_url(url, connector=CONNECTOR, field="url",
                         exceptions_declarees=False)
    except egress.EgressRefused as e:
        raise ImportFailed(str(e)) from None
    try:
        with requests.get(url, stream=True, timeout=URL_TIMEOUT, allow_redirects=False,
                          headers={"Accept": "application/json"}) as r:
            if 300 <= r.status_code < 400:
                raise ImportFailed(f"the URL redirects ({r.status_code}) — give the "
                                   "final address of the document")
            if r.status_code >= 400:
                raise ImportFailed(f"the URL answered HTTP {r.status_code}")
            data = bytearray()
            for chunk in r.iter_content(64 * 1024):
                data += chunk
                if len(data) > MAX_DOC_BYTES:
                    raise ImportFailed(f"the document exceeds {MAX_DOC_BYTES // 1024 // 1024} MB")
    except requests.RequestException as e:
        raise ImportFailed(f"the URL could not be read ({type(e).__name__})") from None
    return _parse(bytes(data))


def fetch_doc_path_document(fields: dict, row: Optional[tuple[str, str, str]] = None) -> Any:
    """The API's own description at the card's `doc_path`, read WITH the
    credential's auth through the same client as every call. `row` = the card's vault
    row, which a card in session mode needs (its cookie jar lives there)."""
    doc_path = (fields.get("doc_path") or "").strip()
    if not doc_path.startswith("/"):
        raise ImportFailed("no `doc_path` on this account's Custom API card (e.g. "
                           "/openapi.json) — use a public URL or paste the document")
    try:
        # The client comes from its single constructor, in the tool module, where the
        # egress guard of every free-host connector lives (`tests/test_egress_guard.py`).
        # Imported here, not at the top: `tools/api` imports this module.
        from ..tools.api import client_for
        raw, _ = client_for(fields, row=row).get_raw(doc_path, max_bytes=MAX_DOC_BYTES)
    except egress.EgressRefused as e:
        raise ImportFailed(str(e)) from None
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        raise ImportFailed(f"`{doc_path}` answered HTTP {status}") from None
    except requests.RequestException as e:
        raise ImportFailed(f"`{doc_path}` could not be read ({type(e).__name__})") from None
    except ValueError as e:      # size cap, redirect refused, invalid card
        raise ImportFailed(f"`{doc_path}`: {e}") from None
    return _parse(raw)


def import_candidates(source: str, *, url: Optional[str] = None,
                      document: Any = None,
                      fields: Optional[dict] = None,
                      row: Optional[tuple[str, str, str]] = None,
                      ) -> tuple[list[dict], list[dict]]:
    """Candidate operations from `doc_path` | `url` | `inline`. Saves nothing."""
    if source == "doc_path":
        doc = fetch_doc_path_document(fields or {}, row)
    elif source == "url":
        if not url:
            raise ImportFailed("`url` is required with source=url")
        doc = fetch_public_document(url)
    elif source == "inline":
        if isinstance(document, str):
            doc = _parse(document.encode())
        else:
            doc = document
        if len(json.dumps(doc or {})) > MAX_DOC_BYTES:
            raise ImportFailed("the document exceeds 2 MB")
    else:
        raise ImportFailed("`source` must be doc_path, url or inline")
    try:
        return custom_ops_openapi.from_openapi(doc)
    except custom_ops_openapi.NotAnOpenApiDocument as e:
        raise ImportFailed(str(e)) from None
