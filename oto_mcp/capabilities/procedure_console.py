"""Console procédures MCP consolidée (ADR 0047, B2) — `oto_procedure`.

Réunit les 9 tools MCP du domaine doctrine/procédure membre en UN : lecture
(`get`/`list`, ex-`oto_get_doctrine`/`oto_list_doctrines`), écriture
(`set`/`delete`, org_admin, épinglable `org=` #69) et bibliothèque publique
(`library_list`/`library_get`/`publish`/`fork`/`unpublish`). Les handlers de
domaine (`orgs_instructions`, `doctrine_library`) sont réutilisés tels quels ;
leurs faces REST ne bougent pas.

⚠️ L'index des doctrines nommées (skills) est APPENDU à la description de CE
tool par `DynamicInstructionsMiddleware.on_list_tools` (via `_DOCTRINE_GET_TOOL`,
middleware.py) — les skills ne sont pas des outils, c'est leur seul canal de
découverte. Le filtre d'usage (`org.instruction.usage`) compte les appels sur ce
nom de tool.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from . import doctrine_library, orgs_instructions
from ._authz import BY_OP, ORG_ADMIN_OPT, ORG_MEMBER, SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx
from .registry import CAPABILITIES


def _need(val, code: str, msg: str):
    if val is None or (isinstance(val, str) and not val.strip()):
        raise AuthzDenied(400, code, msg)
    return val


class ProcedureInput(BaseModel):
    op: Literal["get", "list", "set", "delete",
                "library_list", "library_get", "publish", "fork", "unpublish"]
    slug: Optional[str] = None
    doctrine_id: Optional[int] = None      # get : lecture par ID STABLE (ADR 0032)
    scope: Optional[str] = None            # get/list : org (défaut) | group
    version: Optional[int] = None          # get
    with_history: bool = False             # get
    query: Optional[str] = None            # list / library_list
    body_md: Optional[str] = None          # set
    title: Optional[str] = None            # set / publish
    description: Optional[str] = None      # set / publish
    from_version: Optional[int] = None     # set (revert)
    slots: Optional[list] = None           # set (ADR 0035)
    org: Optional[int] = None              # set/delete : org explicite (#69)
    public_slug: Optional[str] = None      # publish
    category: Optional[str] = None         # publish / library_list
    tags: Optional[list] = None            # publish
    visibility: Optional[str] = None       # publish : public | unlisted
    new_slug: Optional[str] = None         # fork
    id: Optional[int] = None               # unpublish : id d'entrée bibliothèque
    author_kind: Optional[str] = None      # library_list : otomata | org
    limit: int = 100                       # library_list


async def _procedure(ctx: ResolvedCtx, inp: ProcedureInput) -> dict:
    oi, lib = orgs_instructions, doctrine_library
    if inp.op == "get":
        return await oi._get_doctrine(ctx, oi.DoctrineGetInput(
            slug=inp.slug, doctrine_id=inp.doctrine_id, scope=inp.scope or "org",
            version=inp.version, with_history=inp.with_history))
    if inp.op == "list":
        return oi._list_doctrines(ctx, oi.DoctrineListInput(query=inp.query, scope=inp.scope))
    if inp.op == "set":
        return await oi._set_instruction(ctx, oi.InstrSetInput(
            slug=inp.slug, body_md=inp.body_md, title=inp.title,
            description=inp.description, from_version=inp.from_version,
            slots=inp.slots, org=inp.org))
    if inp.op == "delete":
        return oi._delete_instruction(ctx, oi.DoctrineDeleteInput(
            slug=_need(inp.slug, "missing_slug", "`slug` requis pour delete."), org=inp.org))
    if inp.op == "library_list":
        return lib._list(ctx, lib.LibraryListInput(
            query=inp.query, category=inp.category, author_kind=inp.author_kind,
            limit=inp.limit))
    if inp.op == "library_get":
        return lib._get(ctx, lib.LibraryGetInput(
            slug=_need(inp.slug, "missing_slug", "`slug` (public) requis pour library_get.")))
    if inp.op == "publish":
        return lib._publish(ctx, lib.PublishInput(
            slug=_need(inp.slug, "missing_slug", "`slug` (skill d'org) requis pour publish."),
            public_slug=inp.public_slug, title=inp.title, description=inp.description,
            category=inp.category, tags=inp.tags, visibility=inp.visibility or "public"))
    if inp.op == "fork":
        return lib._fork(ctx, lib.ForkInput(
            slug=_need(inp.slug, "missing_slug", "`slug` (public) requis pour fork."),
            new_slug=inp.new_slug))
    return lib._unpublish(ctx, lib.UnpublishInput(
        id=_need(inp.id, "missing_id", "`id` (entrée bibliothèque) requis pour unpublish.")))


CAPABILITIES += [
    Capability(
        key="org.procedure.console", handler=_procedure, Input=ProcedureInput,
        authz=BY_OP({
            "get": SUB_ONLY, "list": SUB_ONLY,
            "set": ORG_ADMIN_OPT("org"), "delete": ORG_ADMIN_OPT("org"),
            "library_list": SUB_ONLY, "library_get": SUB_ONLY,
            "publish": ORG_MEMBER, "fork": ORG_MEMBER, "unpublish": SUB_ONLY,
        }),
        description=(
            "Your org's procedures (named doctrines / skills) + the public library. The base "
            "doctrine is INJECTED at connect — op=get with `slug` loads ONE skill's full "
            "markdown (`scope=group` targets your active department; `doctrine_id` loads by "
            "STABLE id, incl. one SHARED to your org) / list (catalog: slug/title/description, "
            "no body) / set (org_admin write: omit slug = base doctrine; `from_version` "
            "restores; `slots` = required entities referenced <slot:name> in the prose; `org` "
            "pins an explicit org id) / delete (exact `slug`) — and the PUBLIC library: "
            "op=library_list (browse/search, filter category/author_kind) / library_get (full "
            "body by public slug) / publish (share one of your org's skills; visibility="
            "public|unlisted) / fork (copy a public entry into your org, optional `new_slug`) "
            "/ unpublish (`id`)."),
        mcp=orgs_instructions._DOCTRINE_GET_TOOL,
    ),
]
