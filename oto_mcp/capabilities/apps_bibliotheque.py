"""La bibliothèque d'APPS de la communauté : publier une app, l'installer dans son org.

Sœur de la bibliothèque de procédures (`guide_library.py`), sur la même mécanique
commune (`communaute.py` : contrôle automatique, modération, compteurs, vitrine) :

- **publier** (`apps.library.publish`) = l'admin de l'org de l'app (ou un super_admin,
  au nom d'Otomata) prend un INSTANTANÉ de la version VIVANTE : HTML, titre,
  description, vignette, manifeste RÉDUIT (les alias de tableaux et ce que l'app y lit
  et écrit), et le SCHÉMA de chaque tableau déclaré. **Jamais une ligne, jamais un
  agent** (leurs numéros ne veulent rien dire ailleurs, et leur configuration est
  celle de l'org), jamais un numéro de tableau, jamais un compte de connecteur. Un lien
  (`ref`) vers un autre tableau de l'app est récrit en alias ; vers un tableau que
  l'app ne déclare pas, il devient un texte. Le contrôle automatique juge le HTML, les
  métadonnées et les schémas (libellés, options) : une app qui embarque une adresse,
  un téléphone, une clé est refusée (400 `contenu_refuse`).
- **installer** (`apps.library.install`) = tout membre de l'org active : chaque tableau
  naît VIDE depuis son schéma (personnel à l'installeur, dans l'org active), ou se
  branche sur un tableau à lui (`map_tables`, jugé comme un manifeste qu'il
  écrirait) ; l'app naît avec le HTML de l'instantané, PRIVÉE à l'installeur (le
  partage par défaut de l'org ne s'applique pas : c'est le code d'un tiers), rangée
  dans `project_id` si demandé. Une copie, sans lien vivant. ⚠️ Installer le HTML d'une
  autre org fait tourner SON code sur les données de l'installeur : `reviewed=true` est
  exigé pour une entrée signée par une autre org (400 `review_required`) — voir
  `docs/apps.md` §Communauté pour ce que le cadre isolé laisse sortir.
"""
from __future__ import annotations

import copy
import json
import logging
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .. import access, db, org_store, ownership, roles
from ..db import app_library as store
from ..db import communaute as db_communaute
from . import apps as apps_cap
from . import communaute
from ._authz import (LIBRARY_PUBLISHER, ORG_MEMBER, SUB_ONLY, _refus_publication_bibliotheque,
                     peut_publier_bibliotheque)
from .apps_manifeste import forme, org_de_l_app
from .guide_library import _author_for
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)

KIND = store.KIND

#: Les slugs publics qu'aucune app publiée ne porte : les modèles d'app du front
#: (`app-outcomes.ts`, `app-templates.ts`), dont la page vit à la même adresse
#: (`/community/apps/<id>`) — une app publiée sous ce nom masquerait le modèle.
SLUGS_RESERVES = frozenset({
    "crm", "sales", "outreach", "seo", "finance", "abm",                 # modèles « résultat »
    "dashboard", "review", "kanban", "form", "dropzone", "blank",        # modèles « bloc »
})
Visibility = Literal["public", "unlisted"]


# ── l'instantané ────────────────────────────────────────────────────────────────

def _champs(schema: Optional[dict]) -> list:
    return [f for f in (schema or {}).get("fields") or [] if isinstance(f, dict)]


def _recrire_liens(noeud, cible):
    """Parcourt un schéma ; `cible(target) -> nouvelle cible | None`. None = le lien
    n'a pas d'équivalent : le champ devient un texte (même clé, même libellé)."""
    if isinstance(noeud, dict):
        if noeud.get("type") == "ref":
            t = cible(noeud.get("target"))
            if t is None:
                return {k: v for k, v in noeud.items() if k not in ("type", "target")} | \
                    {"type": "text"}
            return {**noeud, "target": t}
        return {k: _recrire_liens(v, cible) for k, v in noeud.items()}
    if isinstance(noeud, list):
        return [_recrire_liens(v, cible) for v in noeud]
    return noeud


def _a_des_liens(noeud) -> bool:
    if isinstance(noeud, dict):
        return noeud.get("type") == "ref" or any(_a_des_liens(v) for v in noeud.values())
    if isinstance(noeud, list):
        return any(_a_des_liens(v) for v in noeud)
    return False


def instantane(sub: str, app: dict) -> tuple[dict, dict, int]:
    """`(manifeste réduit, tableaux, agents retirés)` de la version vivante d'`app`,
    lue avec les droits de `sub` (le schéma d'un tableau qu'il ne lit pas = refus)."""
    from ..datastore.core import DatastoreNotFound, make_store
    from ..datastore.errors import DatastoreForbidden

    clean, problems = forme(app.get("manifest") or {})
    if problems:
        raise AuthzDenied(400, "invalid_manifest",
                          "The app's manifest isn't valid: " + " ".join(problems))
    s = make_store(sub)
    alias_de = {str(t["table"]): a for a, t in clean["tables"].items()}
    tables: dict = {}
    for alias, t in clean["tables"].items():
        try:
            schema = s.get_schema(str(t["table"]))
        except (DatastoreNotFound, DatastoreForbidden) as e:
            raise AuthzDenied(403, "table_not_readable",
                              f"tables.{alias}: you can't read this table, so it can't "
                              "be published.") from e
        nom = (db.get_datastore_by_id(int(t["table"])) or {}).get("datastore") or alias
        schema = _recrire_liens(copy.deepcopy(schema or {}),
                                lambda tg: f"@{alias_de[str(tg)]}" if str(tg) in alias_de
                                else None)
        tables[alias] = {"name": nom, "schema": schema}
    manifeste = {"title": clean.get("title"), "height": clean["height"],
                 "files": clean["files"],
                 "tables": {a: {"fields": t["fields"], "set": t["set"], "add": t["add"]}
                            for a, t in clean["tables"].items()}}
    return manifeste, tables, len(clean["agents"])


# ── entrées ─────────────────────────────────────────────────────────────────────

class AppLibraryListInput(BaseModel):
    query: Optional[str] = None
    category: Optional[str] = None
    author_kind: Optional[Literal["otomata", "org"]] = None
    sort: Optional[Literal["trending", "most_added", "new"]] = None
    include_removed: bool = False
    limit: int = 100

    @field_validator("limit")
    @classmethod
    def _cap(cls, v):
        return max(1, min(int(v), 200))


class AppLibraryGetInput(BaseModel):
    slug: str


class AppLibraryPublishInput(BaseModel):
    app_id: int
    public_slug: Optional[str] = None     # défaut = le titre de l'app, normalisé
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[list[str]] = None
    visibility: Visibility = "public"


class AppLibraryInstallInput(BaseModel):
    slug: str
    project_id: Optional[int] = None
    title: Optional[str] = None
    # alias → numéro d'un tableau À SOI, au lieu d'en créer un vide.
    map_tables: Optional[dict[str, int]] = None
    # « J'ai relu cette app » : exigé pour une entrée signée par une autre org.
    reviewed: bool = False


class AppLibraryUnpublishInput(BaseModel):
    id: int


# ── sorties ─────────────────────────────────────────────────────────────────────

class AppLibraryTable(BaseModel):
    """Un tableau que l'app déclare, tel que l'instantané l'emporte."""
    alias: str = Field(description="Le nom par lequel le HTML le désigne (`oto.rows('alias')`).")
    name: str = Field(description="Le nom du tableau chez l'auteur — celui que prend le "
                                  "tableau créé à l'installation (suffixé si pris).")
    fields: int = Field(description="Nombre de colonnes de son schéma.")


class AppLibrarySummary(communaute.ChampsCommunaute):
    """Une app du catalogue : métadonnées seules, jamais le HTML."""
    id: int = Field(description="Identifiant de l'entrée (clé de unpublish/moderate).")
    slug: str = Field(description="Slug public : clé de get et install.")
    title: str
    description: str
    category: str
    tags: list = Field(default_factory=list)
    thumbnail: Optional[dict] = Field(default=None, description="`{image?, color?, emoji?}`.")
    tables: list[AppLibraryTable] = Field(default_factory=list,
                                          description="Ce que l'installation créera.")
    author_kind: str = Field(description="'otomata' | 'org'.")
    author_display: str = Field(description="Nom qui signe l'entrée, figé à la publication.")
    visibility: Visibility
    version: int
    created_at: str
    updated_at: str


class AppLibraryList(BaseModel):
    """Catalogue des apps, trié par `sort` (absent = dernière publication d'abord)."""
    apps: list[AppLibrarySummary]


class AppLibraryEntry(AppLibrarySummary):
    """Une app de la bibliothèque ENTIÈRE : de quoi la prévisualiser avant de
    l'installer (sur des données d'exemple, jamais celles de l'installeur)."""
    html: str = Field(description="Le HTML de l'instantané.")
    manifest: dict = Field(description="Manifeste réduit : `tables` par alias (`fields`, "
                                       "`set`, `add`), `files`, `height` — aucun numéro "
                                       "de tableau, aucun agent.")
    schemas: dict = Field(description="`{alias: schéma}` de chaque tableau ; un lien vers "
                                      "un autre tableau de l'app y vaut `@alias`.")


class AppLibraryPublishResult(BaseModel):
    """Accusé de publication. Republier le sien remplace l'instantané (version + 1)."""
    published: bool
    id: int
    slug: str
    version: int
    visibility: Visibility
    author_kind: str
    author_display: str
    moderation_state: communaute.ModerationState = "listed"
    agents_dropped: int = Field(description="Agents que le manifeste déclarait et que "
                                            "l'instantané n'emporte pas.")


class AppLibraryInstallResult(BaseModel):
    """Accusé d'installation : une app NEUVE, privée à l'installeur, et ses tableaux."""
    installed: bool
    app_id: int = Field(description="L'app créée (à ouvrir dans `/apps/{app_id}`).")
    org_id: int
    project_id: Optional[int] = None
    tables: dict = Field(description="`{alias: {table, name, created}}` — `created` = "
                                     "tableau neuf et vide ; sinon celui de `map_tables`.")
    forked_from: int = Field(description="L'entrée de bibliothèque d'origine.")


class AppLibraryUnpublishResult(BaseModel):
    unpublished: bool


# ── handlers ────────────────────────────────────────────────────────────────────

def _resume(r: dict) -> dict:
    d = {k: v for k, v in r.items() if k not in ("html", "manifest", "tables")}
    d["tables"] = [{"alias": a, "name": (t or {}).get("name") or a,
                    "fields": len(_champs((t or {}).get("schema")))}
                   for a, t in (r.get("tables") or {}).items()]
    return d


def _list(ctx: ResolvedCtx, inp: AppLibraryListInput) -> dict:
    operateur = access.is_platform_operator(ctx.sub)
    org_retirees = (ctx.org_id if inp.include_removed and not operateur
                    and ctx.org_id is not None and roles.is_org_admin(ctx.sub, ctx.org_id)
                    else None)
    rows = store.lister(query=inp.query, category=inp.category, author_kind=inp.author_kind,
                        sort=inp.sort, limit=inp.limit, removed_for_org=org_retirees,
                        include_all_removed=bool(inp.include_removed and operateur))
    return {"apps": [communaute.projeter(ctx.sub, _resume(r), privilegie=operateur or (
        org_retirees is not None and r.get("author_org_id") == org_retirees)) for r in rows]}


def _visible(ctx: ResolvedCtx, slug: str) -> dict:
    e = store.lire(slug=org_store.normalize_slug(slug), include_unlisted=True,
                   include_removed=True)
    if not e or (e.get("moderation_state") == "removed"
                 and not communaute.peut_voir_retiree(ctx.sub, e)):
        raise AuthzDenied(404, "unknown_entry", f"App `{slug}` inconnue dans la communauté.")
    return e


def _get(ctx: ResolvedCtx, inp: AppLibraryGetInput) -> dict:
    e = _visible(ctx, inp.slug)
    out = communaute.projeter(ctx.sub, _resume(e))
    out.update(html=e["html"], manifest=e["manifest"],
               schemas={a: (t or {}).get("schema") or {} for a, t in (e["tables"] or {}).items()})
    return out


def _publish(ctx: ResolvedCtx, inp: AppLibraryPublishInput) -> dict:
    if ctx.org_id is None:
        raise AuthzDenied(400, "no_active_org",
                          "Publier demande une org active — choisis-en une avec oto_use_org.")
    if not peut_publier_bibliotheque(ctx.sub, ctx.org_id):
        raise _refus_publication_bibliotheque()
    app = apps_cap._app_or_404(ctx.sub, inp.app_id, "write")
    if org_de_l_app(app) != ctx.org_id:
        raise AuthzDenied(400, "app_outside_active_org",
                          "Cette app vit dans une autre org : publie-la depuis son org.")
    manifeste, tables, agents = instantane(ctx.sub, app)
    title = (inp.title if inp.title is not None else app.get("title") or "").strip()
    description = (inp.description or "").strip()
    slug = org_store.normalize_slug(inp.public_slug or title)
    if not slug:
        raise AuthzDenied(400, "missing_slug", "Donne un `public_slug` (ou un titre).")
    if slug in SLUGS_RESERVES:
        # Même refus qu'un nom pris : c'est l'adresse d'un modèle d'app.
        raise AuthzDenied(409, "slug_taken", f"Le nom `{slug}` n'est pas disponible — "
                          "publie sous un autre `public_slug`.")
    communaute.refuser_si_contenu({
        "title": title, "description": description, "category": inp.category or "",
        "tags": " ".join(inp.tags or []), "html": app.get("html") or "",
        "tables": json.dumps(tables, ensure_ascii=False),
        "manifest": json.dumps(manifeste, ensure_ascii=False)})
    kind, author_org_id, display = _author_for(ctx)
    vignette = {k: v for k, v in (app.get("thumbnail") or {}).items()
                if k in ("image", "color", "emoji")} or None
    try:
        row = store.publier(slug=slug, title=title, description=description,
                            category=inp.category or "", tags=inp.tags or [],
                            thumbnail=vignette, html=app.get("html") or "",
                            manifest=manifeste, tables=tables, author_kind=kind,
                            author_org_id=author_org_id, author_display=display,
                            visibility=inp.visibility, source_app_id=int(app["id"]),
                            published_by=ctx.sub)
    except store.SlugPris:
        raise AuthzDenied(409, "slug_taken", f"Le nom `{slug}` n'est pas disponible — "
                          "publie sous un autre `public_slug`.")
    etat = db_communaute.etat(KIND, row["id"])["state"]
    return {"published": True, "id": row["id"], "slug": row["slug"],
            "version": row["version"], "visibility": row["visibility"],
            "author_kind": kind, "author_display": display, "moderation_state": etat,
            "agents_dropped": agents}


def _nom_libre(s, base: str, schema: dict) -> int:
    from ..datastore.errors import DatastoreExists
    for n in range(1, 50):
        nom = base if n == 1 else f"{base}-{n}"
        try:
            return int(s.create_datastore(nom, schema=schema)["id"])
        except DatastoreExists:
            continue
    raise AuthzDenied(409, "table_name_taken", f"Aucun nom libre pour le tableau `{base}`.")


def _install(ctx: ResolvedCtx, inp: AppLibraryInstallInput) -> dict:
    from ..datastore.core import make_store
    from ..datastore.errors import SchemaDefinitionError

    if ctx.org_id is None:
        raise AuthzDenied(400, "no_active_org",
                          "Installer demande une org active — choisis-en une avec oto_use_org.")
    org_id = int(ctx.org_id)
    e = _visible(ctx, inp.slug)
    if e["author_kind"] == "org" and e.get("author_org_id") != org_id and not inp.reviewed:
        raise AuthzDenied(400, "review_required",
                          "Cette app a été écrite par une autre org : son code tournera sur "
                          "tes données. Relis-la (aperçu sur données d'exemple), puis "
                          "installe avec `reviewed=true`.")
    if inp.project_id is not None and not apps_cap._can_project(ctx.sub, int(inp.project_id),
                                                                "write"):
        raise AuthzDenied(403, "project_not_writable",
                          f"Projet #{inp.project_id} introuvable ou non modifiable par toi : "
                          "rien n'a été installé.")
    tables = e.get("tables") or {}
    carte = {a: int(n) for a, n in (inp.map_tables or {}).items()}
    inconnus = sorted(set(carte) - set(tables))
    if inconnus:
        raise AuthzDenied(400, "unknown_alias",
                          f"`map_tables` nomme des tableaux que l'app ne déclare pas : "
                          f"{', '.join(inconnus)}.")
    m = e.get("manifest") or {}

    def _manifeste(numeros: dict) -> dict:
        return {"title": m.get("title"), "height": m.get("height"),
                "files": m.get("files") is True, "agents": {},
                "tables": {a: {"table": str(numeros[a]), **(m.get("tables") or {}).get(a, {})}
                           for a in numeros}}

    # Les tableaux BRANCHÉS se jugent avant toute création : un tableau que l'installeur
    # ne lit pas, ou hors de son org, refuse sans rien laisser derrière.
    if carte:
        apps_cap._checked_manifest(ctx, org_id, _manifeste(carte))
    s = make_store(ctx.sub)
    crees: dict[str, int] = {}
    try:
        for alias, t in tables.items():
            if alias in carte:
                continue
            sans_liens = _recrire_liens(copy.deepcopy((t or {}).get("schema") or {}),
                                        lambda tg: None)
            base = org_store.normalize_slug(f"{(t or {}).get('name') or alias}")[:60] or alias
            crees[alias] = _nom_libre(s, base, sans_liens)
        numero = {**carte, **crees}
        for alias in crees:
            schema = (tables[alias] or {}).get("schema") or {}
            if _a_des_liens(schema):
                s.set_schema(str(crees[alias]), _recrire_liens(
                    copy.deepcopy(schema),
                    lambda tg: numero.get(str(tg)[1:]) if str(tg).startswith("@") else None))
        verifie = apps_cap._checked_manifest(ctx, org_id, _manifeste(numero))
        row = db.apps.create(("user", ctx.sub), org_id,
                             title=(inp.title or e["title"] or e["slug"]).strip()[:200],
                             manifest=verifie, html=e["html"],
                             thumbnail=apps_cap._thumbnail(e.get("thumbnail")),
                             by=ctx.sub, project_id=inp.project_id)
    except Exception as err:
        # Une installation est un seul geste : un refus ne laisse pas de tableau vide.
        for ns in crees.values():
            try:
                db.delete_datastore_by_id(ns)
            except Exception:
                logger.exception("tableau %s laissé après une installation refusée", ns)
        if isinstance(err, (SchemaDefinitionError, ValueError)) and not isinstance(
                err, AuthzDenied):
            # Un schéma de l'instantané que la pose refuse : un refus nommé, pas un 500.
            raise AuthzDenied(400, "invalid_schema",
                              f"Un tableau de cette app ne peut pas être créé : {err}") from None
        raise
    try:
        db_communaute.noter_installation(KIND, e["id"], org_id=org_id, sub=ctx.sub)
    except Exception:
        logger.exception("installation non notée (app %s, org %s)", e["id"], org_id)
    return {"installed": True, "app_id": row["id"], "org_id": org_id,
            "project_id": inp.project_id, "forked_from": e["id"],
            "tables": {a: {"table": numero[a], "created": a in crees,
                           "name": (tables[a] or {}).get("name") or a} for a in tables}}


def _unpublish(ctx: ResolvedCtx, inp: AppLibraryUnpublishInput) -> dict:
    e = store.lire(entry_id=inp.id, include_unlisted=True, include_removed=True)
    if not e or (e.get("moderation_state") == "removed"
                 and not communaute.peut_voir_retiree(ctx.sub, e)):
        raise AuthzDenied(404, "unknown_entry", "Entrée inconnue.")
    auteur = (e["author_kind"] == "org" and e.get("author_org_id") is not None
              and roles.is_org_admin(ctx.sub, int(e["author_org_id"])))
    if not (auteur or access.is_platform_operator(ctx.sub)):
        raise AuthzDenied(403, "forbidden", "Réservé à l'auteur ou à un admin plateforme.")
    communaute.refuser_si_retiree(ctx.sub, e)
    return {"unpublished": store.depublier(inp.id)}


CAPABILITIES += [
    Capability(
        key="apps.library.list", handler=_list, Input=AppLibraryListInput, authz=SUB_ONLY,
        description="Browse the COMMUNITY apps (apps other orgs published). Metadata only "
                    "(title, author, thumbnail, tables it will create, installs_total / "
                    "installs_30d = distinct orgs), never the HTML. sort = trending | "
                    "most_added | new (default: latest published first).",
        Output=AppLibraryList, rest=RestBinding("GET", "/api/me/app-library"),
    ),
    Capability(
        key="apps.library.get", handler=_get, Input=AppLibraryGetInput, authz=SUB_ONLY,
        description="Read one community app in full (HTML, reduced manifest, table schemas) "
                    "to preview it on sample data before installing it.",
        Output=AppLibraryEntry, rest=RestBinding("GET", "/api/me/app-library/{slug}"),
    ),
    Capability(
        key="apps.library.publish", handler=_publish, Input=AppLibraryPublishInput,
        authz=LIBRARY_PUBLISHER,
        description="Publish a snapshot of one of your org's apps to the COMMUNITY (org "
                    "admins; signed with your org's name, Otomata for a platform "
                    "super_admin): its live HTML, title, description, thumbnail, the "
                    "aliases of its tables and their SCHEMAS. Never rows, agents, table "
                    "numbers or connector accounts. Same automatic content check as "
                    "processes (400 contenu_refuse). Re-publishing your own slug bumps the "
                    "version; another author's slug is refused (409 slug_taken).",
        Output=AppLibraryPublishResult,
        rest=RestBinding("POST", "/api/me/app-library/publish", allow_api_token=False),
    ),
    Capability(
        key="apps.library.install", handler=_install, Input=AppLibraryInstallInput,
        authz=ORG_MEMBER,
        description="Install a community app in your active org: each of its tables is "
                    "created EMPTY from its schema (yours, private) unless `map_tables` "
                    "points an alias at a table of yours; the app is created with the "
                    "snapshot's HTML, private to you, filed in `project_id` if given. A "
                    "copy, no live link; counted as one install. An app written by another "
                    "org runs ITS code on your data: `reviewed=true` is required (400 "
                    "review_required).",
        Output=AppLibraryInstallResult,
        rest=RestBinding("POST", "/api/me/app-library/install", allow_api_token=False),
    ),
    Capability(
        key="apps.library.unpublish", handler=_unpublish, Input=AppLibraryUnpublishInput,
        authz=SUB_ONLY,
        description="Remove an app you published from the community (author org admin or "
                    "platform admin). Installed copies stay. An app removed by platform "
                    "staff can't be unpublished by its author (403 entree_retiree).",
        Output=AppLibraryUnpublishResult,
        rest=RestBinding("DELETE", "/api/me/app-library/{id}"),
    ),
]
