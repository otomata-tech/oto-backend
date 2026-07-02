"""Doctrine & instructions d'ORG (ADR 0009) — domaine migré en capacités.

Miroir d'`groups_doctrine` au grain org. Une opération co-déclarée une fois, ses
deux faces (MCP + REST) dérivées par les adaptateurs → fin de la duplication
d'autz `_resolve_org_write` (MCP) vs `_active_org_edit` (REST).

Deux paliers, par combinateur d'autz (pas de branche `org_id` à la main) :
- **membre** : scopé à l'**org active** (`org_id` injecté depuis l'état serveur).
  Lecture = `ORG_MEMBER`/`SUB_ONLY` ; écriture = `ORG_ADMIN`. Chemins `/api/me/*`,
  outils `oto_get_doctrine`/`oto_list_doctrines`/`oto_set_doctrine`/`oto_delete_doctrine`.
- **admin** : org ciblée par `org_id` (cross-org = platform admin via l'escalade
  `roles`). Lecture = `ORG_MEMBER_OF` ; écriture = `ORG_ADMIN_OF`. Chemins
  `/api/admin/orgs/{id}/*`, outils `oto_admin_*_doctrine`.

Les handlers lisent `ctx.org_id` (injecté par l'autz) → **partagés** entre les
deux paliers. La doctrine de **groupe** est lisible en mode membre
(`scope="group"`, complément du département actif) ; son écriture reste dans
`groups_doctrine`. Modèle versionné (slug réservé `claude_md` = doctrine de base).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from pydantic import BaseModel

from .. import access, db, group_store, org_store, roles, slots as slots_mod, tool_registry
from ._authz import ORG_ADMIN, ORG_ADMIN_OF, ORG_MEMBER, ORG_MEMBER_OF, SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_OID = {"id": "org_id"}
_OID_SLUG = {"id": "org_id", "slug": "slug"}
_BASE = org_store.BASE_SLUG
# Outil MCP qui charge la doctrine (donc loggé dans `tool_calls`) → c'est lui que
# l'usage compte. UNE source pour le nom : sert de `mcp=` de la capacité de lecture
# ET de filtre dans `_instruction_usage` → plus de chaîne magique à dériver (le bug
# d'origine : un filtre sur un nom d'outil mort renvoyait toujours 0).
_DOCTRINE_GET_TOOL = "oto_get_doctrine"


# ── Inputs — palier membre (org active, pas d'org_id) ───────────────────────
class EmptyInput(BaseModel):
    pass


class DoctrineGetInput(BaseModel):
    slug: Optional[str] = None
    doctrine_id: Optional[int] = None   # lecture par ID STABLE (ADR 0032) — y compris une doctrine PARTAGÉE à ton org (grant read, livraison #52)
    scope: str = "org"
    version: Optional[int] = None
    with_history: bool = False


class DoctrineListInput(BaseModel):
    query: Optional[str] = None
    scope: Optional[str] = None


class InstrGetInput(BaseModel):
    slug: str
    version: Optional[int] = None


class SlugInput(BaseModel):
    slug: str


class InstrSetInput(BaseModel):
    slug: Optional[str] = None
    body_md: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    from_version: Optional[int] = None
    # ADR 0035 : entités requises déclarées [{name, type: tableau|connecteur|base,
    # description?, connector?}] — référencées <slot:name> dans la prose. None = conserver.
    slots: Optional[list] = None


class RevertInput(BaseModel):
    slug: str
    version: int


# ── Inputs — palier admin (org ciblée par org_id) ───────────────────────────
class AdminDoctrineGetInput(BaseModel):
    org_id: int
    slug: Optional[str] = None
    scope: str = "org"
    version: Optional[int] = None
    with_history: bool = False


class AdminDoctrineListInput(BaseModel):
    org_id: int
    query: Optional[str] = None
    scope: Optional[str] = None


class AdminInstrSetInput(BaseModel):
    org_id: int
    slug: Optional[str] = None
    body_md: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    from_version: Optional[int] = None
    slots: Optional[list] = None


class AdminSlugInput(BaseModel):
    org_id: int
    slug: str


def _project_instance(member_mode: bool) -> Optional[dict]:
    """Bloc « instance » du projet actif (ADR 0032 §5, B3) : les entités du projet
    (tableaux + connecteurs surchargés) contre lesquelles l'agent résout les
    placeholders de la procédure — « la procédure partage à 100 % les ressources du
    projet, pas de ressources propres ». None hors projet, ou en mode admin cross-org
    (le bracelet est une notion de session membre). Best-effort."""
    if not member_mode:
        return None
    pid = access.current_project()
    if pid is None:
        return None
    try:
        p = db.get_project_by_id(pid)
        if p is None:
            return None
        return {
            "project_id": pid,
            "name": p.get("name"),
            "entities": [
                {"target_type": l["target_type"], "target_ref": l["target_ref"],
                 "label": l.get("label"), "role": l.get("role"), "config": l.get("config") or {}}
                for l in db.list_project_links(pid)
            ],
        }
    except Exception:
        return None


# ── Handlers (core ; org_id depuis ctx → partagés membre/admin) ─────────────
async def _get_doctrine(ctx: ResolvedCtx, inp) -> dict:
    """Bundle session-start (slug omis) OU une doctrine nommée. En mode membre
    (`inp.org_id` absent) complète avec la doctrine du département actif."""
    org_id = ctx.org_id
    member_mode = getattr(inp, "org_id", None) is None
    slug = inp.slug
    scope = inp.scope
    version = inp.version

    # Lecture par ID STABLE (ADR 0032 « stop using slug ») — le chemin des liens de
    # projet ET des doctrines PARTAGÉES cross-org (grant read via oto_resource, #52) :
    # l'accès passe par le seam ownership (membre de l'org propriétaire ∪ grants),
    # pas par l'org active.
    doctrine_id = getattr(inp, "doctrine_id", None)
    if doctrine_id is not None:
        from .. import ownership   # import paresseux (miroir _authz, zéro cycle au boot)
        instr = org_store.get_instruction_by_id(int(doctrine_id))
        if not instr:
            raise AuthzDenied(404, "unknown_doctrine",
                              f"Aucune doctrine #{doctrine_id}.")
        if not ownership.can_access(ctx.sub, "doctrine", str(doctrine_id), "read"):
            raise AuthzDenied(403, "forbidden", "Accès refusé à cette doctrine.")
        if version is not None:
            versioned = org_store.get_instruction(instr["org_id"], instr["slug"], version)
            if not versioned:
                raise AuthzDenied(404, "unknown_doctrine",
                                  f"Doctrine #{doctrine_id} : pas de version {version}.")
            instr = {**versioned, "id": instr["id"]}
        return {"org_id": instr["org_id"], "doctrine_id": int(doctrine_id),
                "scope": "org", "slug": instr["slug"], "title": instr["title"],
                "description": instr["description"], "version": instr["version"],
                "body_md": instr["body_md"], "slots": instr.get("slots") or [],
                "referenced_tools": await tool_registry.manifest_for(instr["body_md"])}

    if slug is None:
        # Début de session : doctrine de base + index (vide gracieux si pas d'org).
        if org_id is None:
            return {"org_id": None, "org": None, "doctrine": "", "group_id": None,
                    "group": None, "group_doctrine": "", "doctrines": [], "referenced_tools": []}
        o = org_store.get_org(org_id)
        base = org_store.get_instruction(org_id, _BASE)
        index = [{"slug": i["slug"], "title": i["title"],
                  "description": i["description"], "scope": "org"}
                 for i in org_store.list_instructions(org_id)]
        group_id = access.current_group(ctx.sub) if member_mode else None
        group_name, group_doctrine = None, ""
        if group_id is not None:
            g = group_store.get_group(group_id)
            group_name = g["name"] if g else None
            gbase = group_store.get_group_instruction(group_id, _BASE)
            group_doctrine = (gbase or {}).get("body_md", "") or ""
            index += [{"slug": i["slug"], "title": i["title"],
                       "description": i["description"], "scope": "group"}
                      for i in group_store.list_group_instructions(group_id)]
        doctrine_body = (base or {}).get("body_md", "") or ""
        pi = _project_instance(member_mode)
        return {
            "org_id": org_id, "org": o["name"] if o else None, "doctrine": doctrine_body,
            "group_id": group_id, "group": group_name, "group_doctrine": group_doctrine,
            "doctrines": index,
            "referenced_tools": await tool_registry.manifest_for(doctrine_body, group_doctrine),
            **({"project_instance": pi} if pi else {}),
        }

    # Une doctrine nommée précise.
    if scope == "group" and member_mode:
        group_id = access.current_group(ctx.sub)
        if group_id is None:
            raise AuthzDenied(400, "no_active_group", "Pas de département actif — vois `oto_use_group`.")
        instr = group_store.get_group_instruction(group_id, slug, version)
        scope_ref: dict = {"group_id": group_id}
    else:
        if org_id is None:
            raise AuthzDenied(400, "no_active_org", "Pas d'org active — vois `oto_use_org`.")
        instr = org_store.get_instruction(org_id, slug, version)
        scope_ref = {"org_id": org_id}
    if not instr:
        raise AuthzDenied(404, "unknown_doctrine",
                          f"Aucune doctrine `{org_store.normalize_slug(slug)}` (scope {scope})"
                          + (f" en version {version}" if version is not None else "")
                          + ". Vois `oto_list_doctrines`.")
    out = {**scope_ref, "scope": scope, "slug": instr["slug"], "title": instr["title"],
           "description": instr["description"], "version": instr["version"],
           "body_md": instr["body_md"], "slots": instr.get("slots") or [],
           "referenced_tools": await tool_registry.manifest_for(instr["body_md"])}
    pi = _project_instance(member_mode)
    if pi:
        out["project_instance"] = pi
    if inp.with_history and "org_id" in scope_ref:
        out["versions"] = org_store.list_instruction_versions(org_id, slug)
    return out


def _list_doctrines(ctx: ResolvedCtx, inp) -> dict:
    """Catalogue des doctrines nommées (slug/title/description/version, sans corps)."""
    org_id = ctx.org_id
    member_mode = getattr(inp, "org_id", None) is None
    query = inp.query
    scope = inp.scope
    if org_id is None:
        return {"org_id": None, "doctrines": []}
    out: list = []
    if scope in (None, "org"):
        include_base = not member_mode  # la surface admin inclut la doctrine de base
        rows = (org_store.search_instructions(org_id, query, include_base=include_base) if query
                else org_store.list_instructions(org_id, include_base=include_base))
        out += [{**r, "scope": "org"} for r in rows]
    group_id = access.current_group(ctx.sub) if (member_mode and scope in (None, "group")) else None
    if group_id is not None:
        rows = (group_store.search_group_instructions(group_id, query) if query
                else group_store.list_group_instructions(group_id))
        out += [{**r, "scope": "group"} for r in rows]
    return {"org_id": org_id, "group_id": group_id, "doctrines": out}


async def _set_instruction(ctx: ResolvedCtx, inp) -> dict:
    """Crée/met à jour une instruction (incrémente la version + archive un snapshot).
    `from_version` = restaure une version passée comme nouvelle (revert MCP) — corps,
    métadonnées ET slots. `slots` (ADR 0035) : None = conserver l'existant."""
    org_id = ctx.org_id
    norm = org_store.normalize_slug(inp.slug) if inp.slug else _BASE
    if not norm:
        raise AuthzDenied(400, "invalid_slug", "slug invalide (attendu [a-z0-9_-]).")
    body_md, title, description = inp.body_md, inp.title, inp.description
    slots_in = getattr(inp, "slots", None)
    if inp.from_version is not None:
        old = org_store.get_instruction(org_id, norm, inp.from_version)
        if not old:
            raise AuthzDenied(404, "unknown_version", f"Pas de version {inp.from_version} pour `{norm}`.")
        body_md, title, description = old["body_md"], old["title"], old["description"]
        slots_in = old.get("slots") or []
    if slots_in is not None:
        try:
            slots_in = slots_mod.validate_slots(slots_in)
        except ValueError as e:
            raise AuthzDenied(400, "invalid_slots", str(e))
    body_md = (body_md or "").strip()
    if not body_md:
        raise AuthzDenied(400, "body_md_required", "body_md vide (ou fournis `from_version`).")
    # Injecté dans la doctrine de base servie à chaque session → caper la taille.
    if len(body_md.encode()) > 64 * 1024:
        raise AuthzDenied(400, "body_too_large", "body_md > 64 KB.")
    if norm == _BASE:
        version = org_store.set_instruction(org_id, _BASE, body_md, set_by=ctx.sub,
                                            slots=slots_in)
    else:
        version = org_store.set_instruction(org_id, norm, body_md, title=title,
                                            description=description, set_by=ctx.sub,
                                            slots=slots_in)
    # Slots EFFECTIFS après écriture (None = conservés → relire la row) pour le
    # check croisé <slot:name> ↔ déclaration (ADR 0035, non bloquant comme 0014).
    effective_slots = slots_in
    if effective_slots is None:
        cur = org_store.get_instruction(org_id, norm)
        effective_slots = (cur or {}).get("slots") or []
    return {"ok": True, "org_id": org_id, "slug": norm, "version": version, "set": True,
            **({"reverted_from": inp.from_version} if inp.from_version is not None else {}),
            **await tool_registry.write_check(body_md),
            **slots_mod.slots_check(body_md, effective_slots)}


def _delete_instruction(ctx: ResolvedCtx, inp) -> dict:
    norm = org_store.normalize_slug(inp.slug)
    if not norm:
        raise AuthzDenied(400, "invalid_slug", "slug requis.")
    deleted = org_store.delete_instruction(ctx.org_id, norm)
    if not deleted:
        raise AuthzDenied(404, "not_found", f"Instruction `{norm}` absente.")
    return {"ok": True, "org_id": ctx.org_id, "slug": norm, "deleted": True}


# ── Handlers REST-only (org active) ─────────────────────────────────────────
def _instructions_list(ctx: ResolvedCtx, inp: EmptyInput) -> dict:
    """Doctrine de base (meta) + index des instructions nommées de l'org active.
    Bundle vide en 200 si pas d'org active (consommé par l'overview)."""
    org_id = ctx.org_id
    if org_id is None:
        return {"org_id": None, "org_name": None, "can_edit": False,
                "doctrine": {"exists": False, "version": 0, "updated_at": None},
                "instructions": []}
    o = org_store.get_org(org_id)
    base = org_store.get_instruction(org_id, _BASE)
    return {
        "org_id": org_id,
        "org_name": o["name"] if o else None,
        "can_edit": roles.is_org_admin(ctx.sub, org_id),
        "doctrine": {
            "exists": base is not None,
            "version": base["version"] if base else 0,
            "updated_at": base["updated_at"] if base else None,
        },
        "instructions": org_store.list_instructions(org_id),
    }


def _instruction_get(ctx: ResolvedCtx, inp: InstrGetInput) -> dict:
    instr = org_store.get_instruction(ctx.org_id, inp.slug, version=inp.version)
    if not instr:
        raise AuthzDenied(404, "not_found", f"Instruction `{org_store.normalize_slug(inp.slug)}` absente.")
    return {
        "slug": instr["slug"], "title": instr["title"], "description": instr["description"],
        "version": instr["version"], "body_md": instr["body_md"],
        "slots": instr.get("slots") or [], "set_by": instr.get("set_by"),
        "created_at": instr.get("created_at"), "updated_at": instr.get("updated_at"),
    }


def _instruction_versions(ctx: ResolvedCtx, inp: SlugInput) -> dict:
    slug = org_store.normalize_slug(inp.slug)
    return {"slug": slug, "versions": org_store.list_instruction_versions(ctx.org_id, slug)}


def _instruction_revert(ctx: ResolvedCtx, inp: RevertInput) -> dict:
    slug = org_store.normalize_slug(inp.slug)
    old = org_store.get_instruction(ctx.org_id, slug, version=inp.version)
    if not old:
        raise AuthzDenied(404, "not_found", f"Pas de version {inp.version} pour `{slug}`.")
    version = org_store.set_instruction(ctx.org_id, slug, old["body_md"], title=old["title"],
                                        description=old["description"], set_by=ctx.sub,
                                        slots=old.get("slots") or [])
    return {"ok": True, "slug": slug, "version": version, "reverted_from": inp.version}


def _instruction_usage(ctx: ResolvedCtx, inp: SlugInput) -> dict:
    """Usage d'une doctrine (ADR 0014) : nb de chargements par l'agent, appelants,
    série journalière 30j — dérivé de `tool_calls` (`oto_get_doctrine`), scopé org."""
    slug = org_store.normalize_slug(inp.slug)
    subs = [m["sub"] for m in org_store.list_org_members(ctx.org_id)]
    slug_filter = None if slug == _BASE else slug
    u = db.instruction_usage(subs, _DOCTRINE_GET_TOOL, slug_filter, days=30)
    today = date.today()
    series = [u["daily"].get(str(today - timedelta(days=29 - i)), 0) for i in range(30)]
    return {"slug": slug, "count": u["count"], "callers": u["callers"], "series": series}


CAPABILITIES += [
    # ── Lectures membre (org active) ────────────────────────────────────────
    Capability(
        key="org.doctrine.get", handler=_get_doctrine, Input=DoctrineGetInput,
        authz=SUB_ONLY,
        description=("Operational doctrine of your active org. The base doctrine is now "
                     "INJECTED into your session instructions at connect — call this with "
                     "`slug` to load ONE named skill's full markdown (list skills with "
                     "oto_list_doctrines). No-arg returns base + index, e.g. to refresh "
                     "after switching org with oto_use_org. `scope=group` targets your "
                     "active department. `doctrine_id` loads a doctrine by its STABLE id "
                     "(project procedure links) — including one SHARED to you/your org "
                     "by another org (delivered project)."),
        mcp=_DOCTRINE_GET_TOOL,
        # Face REST par ID stable : résolution des liens `procedure` d'un projet côté
        # dashboard — y compris un projet LIVRÉ (doctrine d'une autre org, grant read).
        rest=RestBinding("GET", "/api/me/doctrines/{doctrine_id}"),
    ),
    Capability(
        key="org.doctrine.list", handler=_list_doctrines, Input=DoctrineListInput,
        authz=SUB_ONLY,
        description=("Catalog of your org's named doctrines (skills) + active department — "
                     "slug/title/description/version, no body. Load one with oto_get_doctrine(slug)."),
        mcp="oto_list_doctrines",
    ),
    Capability(
        key="org.instruction.list", handler=_instructions_list, Input=EmptyInput,
        authz=SUB_ONLY,
        rest=RestBinding("GET", "/api/me/instructions"),
    ),
    Capability(
        key="org.instruction.get", handler=_instruction_get, Input=InstrGetInput,
        authz=ORG_MEMBER,
        rest=RestBinding("GET", "/api/me/instructions/{slug}"),
    ),
    Capability(
        key="org.instruction.versions", handler=_instruction_versions, Input=SlugInput,
        authz=ORG_MEMBER,
        rest=RestBinding("GET", "/api/me/instructions/{slug}/versions"),
    ),
    Capability(
        key="org.instruction.usage", handler=_instruction_usage, Input=SlugInput,
        authz=ORG_MEMBER,
        rest=RestBinding("GET", "/api/me/instructions/{slug}/usage"),
    ),
    # ── Écritures membre (org active, org_admin) ────────────────────────────
    Capability(
        key="org.instruction.set", handler=_set_instruction, Input=InstrSetInput,
        authz=ORG_ADMIN,
        description=("Write your org's doctrine (org_admin). Each write bumps the version "
                     "and archives a snapshot. slug omitted = base doctrine; given = a named "
                     "skill. `from_version` restores a past version as a new one (revert). "
                     "`slots` = the procedure's REQUIRED ENTITIES [{name, type: tableau|"
                     "connecteur|base, description?, connector?}] — reference them BY NAME "
                     "in the prose as <slot:name> (never a hardcoded instance: the project "
                     "binds name→instance). Response returns cross-check warnings "
                     "(unresolved/unreferenced slots, suggestions)."),
        mcp="oto_set_doctrine",
        rest=RestBinding("PUT", "/api/me/instructions/{slug}"),
    ),
    Capability(
        key="org.instruction.delete", handler=_delete_instruction, Input=SlugInput,
        authz=ORG_ADMIN,
        description="Delete a doctrine and its history (org_admin). Pass the EXACT slug.",
        mcp="oto_delete_doctrine",
        rest=RestBinding("DELETE", "/api/me/instructions/{slug}"),
    ),
    Capability(
        key="org.instruction.revert", handler=_instruction_revert, Input=RevertInput,
        authz=ORG_ADMIN,
        rest=RestBinding("POST", "/api/me/instructions/{slug}/revert"),
    ),
    # ── Palier admin (org ciblée par org_id ; cross-org = platform admin) ────
    Capability(
        key="org.doctrine.admin_get", handler=_get_doctrine, Input=AdminDoctrineGetInput,
        authz=ORG_MEMBER_OF("org_id"),
        description="[ADMIN] Read another org's doctrine by id (base+index, or one skill).",
        mcp="oto_admin_get_doctrine",
        rest=RestBinding("GET", "/api/admin/orgs/{id}/instructions/{slug}", _OID_SLUG),
    ),
    Capability(
        key="org.doctrine.admin_list", handler=_list_doctrines, Input=AdminDoctrineListInput,
        authz=ORG_MEMBER_OF("org_id"),
        description="[ADMIN] List another org's named doctrines by id (incl. base doctrine).",
        mcp="oto_admin_list_doctrines",
        rest=RestBinding("GET", "/api/admin/orgs/{id}/instructions", _OID),
    ),
    Capability(
        key="org.instruction.admin_set", handler=_set_instruction, Input=AdminInstrSetInput,
        authz=ORG_ADMIN_OF("org_id"),
        description="[ADMIN] Write another org's doctrine by id (cross-org = platform admin).",
        mcp="oto_admin_set_doctrine",
        rest=RestBinding("PUT", "/api/admin/orgs/{id}/instructions/{slug}", _OID_SLUG),
    ),
    Capability(
        key="org.instruction.admin_delete", handler=_delete_instruction, Input=AdminSlugInput,
        authz=ORG_ADMIN_OF("org_id"),
        description="[ADMIN] Delete another org's doctrine by id and its history.",
        mcp="oto_admin_delete_doctrine",
        rest=RestBinding("DELETE", "/api/admin/orgs/{id}/instructions/{slug}", _OID_SLUG),
    ),
]
