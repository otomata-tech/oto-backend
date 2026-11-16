"""Capabilities "Custom API operations" — the org's REST face of the `api` connector's
described operations (`custom_ops`), for the dashboard's Custom API page.

Two rows, chosen by `scope`. `org` (default): the org's vault row for the named
`account` (`""` = the single account) — same permission split as the org's credential
itself (`orgs/secrets.py`): any member READS the operations (they are what the agent
calls), only an org admin WRITES or IMPORTS them, the same `ORG_ADMIN_OF` as setting the
key. `member`: the CALLER's own key in this org (`byo_user`, the member tier), read and
written by the caller alone — the row is derived from the caller's identity, never from
an argument, so no route reaches another member's key. Without the row, 404: operations
describe a connected API, never an absent one.

The agent's face is `tools/api_ops.py` (row resolved by the cascade for the call);
both go through `connectors/api_operations.py`, where the list is stored.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ... import credentials_store, custom_ops, providers
from ...connectors import api_operations as store
from .._authz import BY_OP, ORG_ADMIN_OF, ORG_MEMBER_OF
from .._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES

_ID = {"id": "org_id"}
_PATH = "/api/orgs/{id}/connectors/api/operations"
_DEF_PATH = "/api/orgs/{id}/connectors/api/definition"
_DOC_ACCOUNT = ("Custom API account of the org (`\"\"` = the single account). "
                "404 `http_credential_not_found` when the org has no such account.")
_DOC_SCOPE = ("Whose key: `org` (default) = the org's, `member` = the caller's own key in "
              "this org (personal tier). A member key is only ever the caller's own.")
Scope = Literal["org", "member"]
# Reads: any member of the org, whatever the scope (a `member` row is the caller's own).
# Writes: the org's row is the org admin's, the caller's own row is the caller's.
_READ = BY_OP({"org": ORG_MEMBER_OF("org_id"), "member": ORG_MEMBER_OF("org_id")},
              fields=("scope",))
_WRITE = BY_OP({"org": ORG_ADMIN_OF("org_id"), "member": ORG_MEMBER_OF("org_id")},
               fields=("scope",))


class HttpOperations(BaseModel):
    """The described operations of one Custom API account of the org. Each:
    `name`, `description`, `method`, `path` (relative, `{param}` placeholders),
    `input` (JSON schema object), optional `in` {argument: path|query|body}."""
    account: str
    operations: list[dict]


class HttpOperationsImport(BaseModel):
    """CANDIDATES converted from an OpenAPI/Swagger JSON document — nothing is saved:
    the page reviews them, then `PUT`s the list it keeps. `skipped` = what the
    format cannot express, with the reason (`{path, method, reason}`)."""
    account: str
    operations: list[dict]
    skipped: list[dict]


class GetInput(BaseModel):
    org_id: int
    account: str = Field(default="", description=_DOC_ACCOUNT)
    scope: Scope = Field(default="org", description=_DOC_SCOPE)


class SetInput(BaseModel):
    org_id: int
    account: str = Field(default="", description=_DOC_ACCOUNT)
    scope: Scope = Field(default="org", description=_DOC_SCOPE)
    operations: list[Any] = Field(description=(
        "The FULL list (replaces, never merges; `[]` clears). At most 100, unique "
        "snake_case names. Invalid ⇒ 400 `invalid_operations`, `details.errors` lists "
        "every problem."))


class ImportInput(BaseModel):
    org_id: int
    account: str = Field(default="", description=_DOC_ACCOUNT)
    scope: Scope = Field(default="org", description=_DOC_SCOPE)
    source: Literal["doc_path", "url", "inline"] = Field(description=(
        "`doc_path` = the API's own document at the card's doc route, read with the "
        "account's auth; `url` = a PUBLIC JSON document (no auth sent, 2 MB max); "
        "`inline` = the document in `document`."))
    url: Optional[str] = None
    document: Optional[Any] = None


Row = tuple[str, str, str]     # (entity_type, entity_id, account) of a vault row


def _row(ctx: ResolvedCtx, inp) -> Row:
    """The vault row the input designates, once it is known to exist: the org's
    (`scope=org`) or the CALLER's own in this org (`scope=member`, `ctx.sub`)."""
    if inp.scope == "member":
        owner = (credentials_store.MEMBER,
                 credentials_store.member_id(inp.org_id, ctx.sub))
        whose = f"You have no personal Custom API account in org #{inp.org_id}"
    else:
        owner = (credentials_store.ORG, str(inp.org_id))
        whose = f"Org #{inp.org_id} has no Custom API account"
    account = (inp.account or "").strip()
    accounts = {r["account"] for r in credentials_store.list_accounts(
        *owner, store.CONNECTOR)}
    if account not in accounts:
        raise AuthzDenied(404, "http_credential_not_found", (
            f"{whose} `{account}`"
            + (f" (accounts: {', '.join(repr(a) for a in sorted(accounts))})."
               if accounts else " — connect the API first.")))
    return (*owner, account)


def _load(row: Row) -> list[dict]:
    try:
        return store.load(*row)
    except store.InstanceMissing:
        # The vault row exists (checked by `_row`) but no live instance names it:
        # nothing was ever described on it, and nothing can be until it is named.
        return []


def _get(ctx: ResolvedCtx, inp: GetInput) -> dict:
    row = _row(ctx, inp)
    return {"account": row[2], "operations": _load(row)}


def _set(ctx: ResolvedCtx, inp: SetInput) -> dict:
    row = _row(ctx, inp)
    try:
        saved = store.save(*row, inp.operations)
    except custom_ops.OperationsInvalid as e:
        raise AuthzDenied(400, "invalid_operations",
                          f"{len(e.errors)} problem(s) in the operations: {e}",
                          # A dict, like every `details` (`Erreur` in /openapi.json).
                          details={"errors": e.errors})
    except store.InstanceMissing:
        raise AuthzDenied(409, "http_instance_missing", (
            "The account's credential has no live instance to carry its operations — "
            "set the key again on the Custom API card, then retry."))
    return {"account": row[2], "operations": saved}


def _import(ctx: ResolvedCtx, inp: ImportInput) -> dict:
    row = _row(ctx, inp)
    fields = None
    if inp.source == "doc_path":
        cred = credentials_store.get_credential_with_meta(
            row[0], row[1], store.CONNECTOR, row[2])
        fields = credentials_store.unpack_secret(store.CONNECTOR, cred["secret"]) if cred else {}
    try:
        cands, skipped = store.import_candidates(
            inp.source, url=inp.url, document=inp.document, fields=fields, row=row)
    except store.ImportFailed as e:
        raise AuthzDenied(400, "import_failed", str(e))
    return {"account": row[2], "operations": cands, "skipped": skipped}


# The card's NON-secret fields a definition export carries — those that let another
# org rebuild the same connector. Filtered AGAIN by the registry's `secret` flag at
# export: a field turned secret tomorrow leaves the export by itself.
_CARD_FIELDS = ("label", "base_url", "auth_mode", "header_name", "query_param",
                "doc_path", "token_url", "scope")


class HttpDefinition(BaseModel):
    """A custom connector's definition, NEVER a secret: the card's non-secret fields
    (`label`, `base_url`, `auth_mode`, `header_name`, `query_param`, `doc_path`,
    `token_url`, `scope` — present when set) and the saved definition
    (`description`, `logo_domain`, `headers`, `variables` with their values,
    `operations`). Export it from one org, `PUT` its definition part in another."""
    model_config = ConfigDict(extra="allow")
    account: str
    label: Optional[str] = None
    description: Optional[str] = None
    logo_domain: Optional[str] = None
    headers: dict
    variables: dict
    operations: list[dict]


class DefinitionSetInput(BaseModel):
    org_id: int
    account: str = Field(default="", description=_DOC_ACCOUNT)
    scope: Scope = Field(default="org", description=_DOC_SCOPE)
    label: Optional[str] = None
    description: Optional[str] = None
    logo_domain: Optional[str] = None
    headers: Optional[dict] = None
    variables: Optional[dict] = None
    operations: Optional[list[Any]] = Field(default=None, description=(
        "When given, REPLACES the action list. Parts not given stay as they are."))


def _card(row: Row) -> dict:
    """The card's non-secret fields — decrypted in-process, secret ones dropped."""
    cred = credentials_store.get_credential_with_meta(row[0], row[1], store.CONNECTOR,
                                                      row[2])
    if not cred:
        return {}
    fields = {**credentials_store.unpack_secret(store.CONNECTOR, cred["secret"]),
              **credentials_store.public_meta(cred.get("meta"))}
    secrets = {f.name for f in providers.REGISTRY[store.CONNECTOR].secret_fields if f.secret}
    return {k: fields[k] for k in _CARD_FIELDS
            if k not in secrets and fields.get(k) not in (None, "")}


def _definition_out(row: Row) -> dict:
    try:
        d = store.load_definition(*row)
    except store.InstanceMissing:
        d = store.definition_of({})
    card = _card(row)
    return {"account": row[2], **card, **{k: v for k, v in d.items()
                                          if v is not None or k not in card}}


def _get_definition(ctx: ResolvedCtx, inp: GetInput) -> dict:
    return _definition_out(_row(ctx, inp))


def _set_definition(ctx: ResolvedCtx, inp: DefinitionSetInput) -> dict:
    row = _row(ctx, inp)
    given = {k: getattr(inp, k) for k in ("label", "description", "logo_domain",
                                          "headers", "variables", "operations")
             if getattr(inp, k) is not None}
    card = _card(row)
    auth_header = card.get("header_name") if card.get("auth_mode") == "header" else None
    try:
        store.save_definition(*row, given, auth_header=auth_header)
    except custom_ops.OperationsInvalid as e:
        raise AuthzDenied(400, "invalid_definition",
                          f"{len(e.errors)} problem(s) in the definition: {e}",
                          details={"errors": e.errors})
    except store.InstanceMissing:
        raise AuthzDenied(409, "http_instance_missing", (
            "The account's credential has no live instance to carry its definition — "
            "set the key again on the Custom API card, then retry."))
    return _definition_out(row)


_NOT_FOUND = DeclaredError(404, "http_credential_not_found",
                           "no Custom API (`api`) credential for this account at this "
                           "scope (the org's, or the caller's own)")

CAPABILITIES += [
    Capability(
        key="custom_api.operations.get", handler=_get, Input=GetInput,
        authz=_READ, Output=HttpOperations,
        description="Read the described operations of an org's Custom API account.",
        errors=(_NOT_FOUND,),
        rest=RestBinding("GET", _PATH, _ID),
    ),
    Capability(
        key="custom_api.operations.set", handler=_set, Input=SetInput,
        authz=_WRITE, Output=HttpOperations,
        description=("Replace the described operations of an org's Custom API account "
                     "(validated whole; org admin for the org's key, the caller for "
                     "their own)."),
        errors=(_NOT_FOUND,
                DeclaredError(400, "invalid_operations",
                              "the list breaks the format — `details.errors` names each problem"),
                DeclaredError(409, "http_instance_missing",
                              "the credential has no live instance to carry the list")),
        rest=RestBinding("PUT", _PATH, _ID),
    ),
    Capability(
        key="custom_api.operations.import", handler=_import, Input=ImportInput,
        authz=_WRITE, Output=HttpOperationsImport,
        description=("Convert an OpenAPI/Swagger JSON document into candidate operations "
                     "for an org's Custom API account, without saving (org admin, or "
                     "the caller for their own key)."),
        errors=(_NOT_FOUND,
                DeclaredError(400, "import_failed",
                              "the source cannot be read as an OpenAPI/Swagger JSON "
                              "document (unreachable, refused address, too large, not JSON)")),
        rest=RestBinding("POST", _PATH + "/import", _ID),
    ),
    Capability(
        key="custom_api.definition.get", handler=_get_definition, Input=GetInput,
        authz=_READ, Output=HttpDefinition,
        description=("Export the definition of an org's Custom API connector — the "
                     "card's non-secret fields and the saved definition, never a secret."),
        errors=(_NOT_FOUND,),
        rest=RestBinding("GET", _DEF_PATH, _ID),
    ),
    Capability(
        key="custom_api.definition.set", handler=_set_definition,
        Input=DefinitionSetInput, authz=_WRITE, Output=HttpDefinition,
        description=("Save the definition of an org's Custom API connector (label, "
                     "description, logo domain, constant headers, variables, actions; "
                     "only the parts given; org admin, or the caller for their own key)."),
        errors=(_NOT_FOUND,
                DeclaredError(400, "invalid_definition",
                              "the definition breaks the format — `details.errors` names "
                              "each problem"),
                DeclaredError(409, "http_instance_missing",
                              "the credential has no live instance to carry the definition")),
        rest=RestBinding("PUT", _DEF_PATH, _ID),
    ),
]


# The routes of the connector's OLD name (`http`, until 12/11/2026) are not capabilities:
# they answer 308 to these ones, derived from them (`deprecations._alias_connecteur_api`).
