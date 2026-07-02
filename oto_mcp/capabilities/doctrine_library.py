"""Capacités « bibliothèque publique de doctrines » (marketplace de skills).

Un catalogue cherchable et partageable de doctrines PUBLIÉES, chaque entrée
portant un AUTEUR : **Otomata** (la plateforme) ou un **créateur privé** (une
org). Co-déclarées MCP + REST (ADR 0009) :

- lecture (`library.list`/`library.get`) = tout user authentifié (`SUB_ONLY`) ;
  la surface ANONYME pour la vitrine est servie à part par des routes écrites à
  la main dans `api_routes` (l'adaptateur REst des capacités authentifie toujours).
- publication / fork = org_admin de l'**org active** (injectée par `ORG_MEMBER`,
  jamais d'un param client → verrou IDOR) ; un publieur **platform-operator**
  publie au nom d'**Otomata**.
- dépublication = l'auteur (org_admin de l'org auteur) ou un admin plateforme.

Handlers SYNC (les adaptateurs n'awaitent pas). Le fork réutilise
`org_store.set_instruction` → la doctrine forkée devient un skill d'org versionné.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from .. import access, org_store, roles
from ._authz import ORG_MEMBER, SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class LibraryListInput(BaseModel):
    query: Optional[str] = None
    category: Optional[str] = None
    author_kind: Optional[str] = None    # 'otomata' | 'org'
    limit: int = 100


class LibraryGetInput(BaseModel):
    slug: str


class PublishInput(BaseModel):
    slug: str                            # le slug du skill d'org à publier
    public_slug: Optional[str] = None    # slug public (défaut = slug source)
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[list] = None
    visibility: str = "public"           # 'public' | 'unlisted'


class ForkInput(BaseModel):
    slug: str                            # slug public de l'entrée à forker
    new_slug: Optional[str] = None


class UnpublishInput(BaseModel):
    id: int


def _require_org_admin(ctx: ResolvedCtx, what: str) -> None:
    """Gate org_admin de l'org active (escalade platform_admin incluse)."""
    if access.is_platform_operator(ctx.sub):
        return
    if ctx.org_id is None or not roles.is_org_admin(ctx.sub, ctx.org_id):
        raise AuthzDenied(403, "forbidden", f"{what} requiert org_admin de ton org active.")


def _author_for(ctx: ResolvedCtx) -> tuple[str, Optional[int], str]:
    """Auteur affiché : platform-operator → Otomata ; sinon l'org active."""
    if access.is_platform_operator(ctx.sub):
        return "otomata", None, "Otomata"
    o = org_store.get_org(ctx.org_id) if ctx.org_id else None
    return "org", ctx.org_id, ((o or {}).get("name") or "")


def _list(ctx: ResolvedCtx, inp: LibraryListInput) -> dict:
    return {"doctrines": org_store.list_library(
        query=inp.query, category=inp.category, author_kind=inp.author_kind,
        include_unlisted=False, limit=inp.limit)}


def _get(ctx: ResolvedCtx, inp: LibraryGetInput) -> dict:
    # Sémantique `unlisted` = **lien non listé** (style YouTube), choix assumé :
    # une entrée `unlisted` est servie par SLUG EXACT à tout user authentifié
    # (`include_unlisted=True`), mais n'apparaît JAMAIS dans le catalogue
    # (`_list` force `include_unlisted=False`) ni sur la surface anonyme. C'est un
    # partage par lien — pas un secret d'org. Une doctrine vraiment sensible ne se
    # publie pas (reste un skill d'org privé). Cf. CLAUDE.md §Bibliothèque.
    entry = org_store.get_library_entry(slug=inp.slug, include_unlisted=True)
    if not entry:
        raise AuthzDenied(404, "unknown_entry", f"Doctrine publique `{inp.slug}` inconnue.")
    return entry


def _publish(ctx: ResolvedCtx, inp: PublishInput) -> dict:
    _require_org_admin(ctx, "Publier")
    src = org_store.get_instruction(ctx.org_id, inp.slug)
    if not src:
        raise AuthzDenied(404, "unknown_doctrine",
                          f"Doctrine `{inp.slug}` absente de ton org active.")
    kind, author_org_id, display = _author_for(ctx)
    row = org_store.publish_doctrine(
        slug=inp.public_slug or inp.slug,
        title=inp.title if inp.title is not None else (src.get("title") or ""),
        description=inp.description if inp.description is not None else (src.get("description") or ""),
        body_md=src["body_md"], author_kind=kind, author_org_id=author_org_id,
        author_display=display, category=inp.category or "", tags=inp.tags or [],
        visibility=inp.visibility, source_org_id=ctx.org_id, source_slug=inp.slug,
        published_by=ctx.sub, slots=src.get("slots") or [],
    )
    return {"published": True, "id": row["id"], "slug": row["slug"],
            "version": row["version"], "visibility": row["visibility"]}


def _fork(ctx: ResolvedCtx, inp: ForkInput) -> dict:
    _require_org_admin(ctx, "Forker")
    entry = org_store.get_library_entry(slug=inp.slug, include_unlisted=True)
    if not entry:
        raise AuthzDenied(404, "unknown_entry", f"Doctrine publique `{inp.slug}` inconnue.")
    res = org_store.fork_into_org(entry_id=entry["id"], org_id=ctx.org_id,
                                  new_slug=inp.new_slug, set_by=ctx.sub)
    return {"forked": True, **res}


def _unpublish(ctx: ResolvedCtx, inp: UnpublishInput) -> dict:
    entry = org_store.get_library_entry(entry_id=inp.id, include_unlisted=True)
    if not entry:
        raise AuthzDenied(404, "unknown_entry", "Entrée inconnue.")
    is_author = (entry["author_kind"] == "org" and entry.get("author_org_id") is not None
                 and roles.is_org_admin(ctx.sub, entry["author_org_id"]))
    if not (is_author or access.is_platform_operator(ctx.sub)):
        raise AuthzDenied(403, "forbidden", "Réservé à l'auteur ou à un admin plateforme.")
    return {"unpublished": org_store.unpublish_doctrine(inp.id)}


CAPABILITIES += [
    Capability(
        key="library.list", handler=_list, Input=LibraryListInput, authz=SUB_ONLY,
        description="Browse/search the PUBLIC doctrine library (a marketplace of skills/"
                    "templates). Each entry has an author (Otomata or a private creator). "
                    "Filter by query / category / author_kind (otomata|org). Returns metadata "
                    "+ snippet, not the full body — use oto_get_library_doctrine for that.",
        mcp="oto_list_library", rest=RestBinding("GET", "/api/me/doctrines/library"),
    ),
    Capability(
        key="library.get", handler=_get, Input=LibraryGetInput, authz=SUB_ONLY,
        description="Read one public-library doctrine in full (markdown body) by its public "
                    "slug — preview before forking it into your org with oto_fork_doctrine. "
                    "Also serves `unlisted` entries by exact slug (unlisted = shared by link, "
                    "never in the catalog), not a private-org secret.",
        mcp="oto_get_library_doctrine",
        rest=RestBinding("GET", "/api/me/doctrines/library/{slug}"),
    ),
    Capability(
        key="library.publish", handler=_publish, Input=PublishInput, authz=ORG_MEMBER,
        description="Publish one of your org's named doctrines (skills) to the PUBLIC library "
                    "so others can find and fork it. Requires org_admin of your active org. "
                    "slug = the org skill to publish ; visibility = public | unlisted.",
        mcp="oto_publish_doctrine", rest=RestBinding("POST", "/api/me/doctrines/publish"),
    ),
    Capability(
        key="library.fork", handler=_fork, Input=ForkInput, authz=ORG_MEMBER,
        description="Fork (copy) a public-library doctrine into your active org as a new "
                    "versioned skill. Requires org_admin of your active org. slug = the public "
                    "entry ; new_slug optional (defaults to source slug, de-duplicated).",
        mcp="oto_fork_doctrine", rest=RestBinding("POST", "/api/me/doctrines/fork"),
    ),
    Capability(
        key="library.unpublish", handler=_unpublish, Input=UnpublishInput, authz=SUB_ONLY,
        description="Remove a doctrine you published from the public library (author org_admin "
                    "or platform admin). id = the library entry id.",
        mcp="oto_unpublish_doctrine",
        rest=RestBinding("DELETE", "/api/me/doctrines/library/{id}"),
    ),
]
