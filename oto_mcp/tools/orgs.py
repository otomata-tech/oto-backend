"""Meta-tools du palier organization (= périmètre / store serveur).

Deux niveaux de surface :

- **user** (`oto_list_orgs`, `oto_use_org`) : voir ses orgs et basculer l'org
  active. La résolution des secrets (`resolve_api_key`) vise l'org active du
  sub — bascule = changer quels `org_secrets` répondent. Comme c'est un état
  serveur par sub (le token MCP ne porte pas l'org), le switch est résolu
  live à chaque appel ; pas de claim dans le JWT.
- **platform_admin** (`oto_admin_*`) : provisionne tout (créer une org, ajouter
  des membres, poser les secrets partagés). En v1, seul le platform_admin écrit
  — pas de self-service org_admin (les opérateurs sont peu nombreux et ajoutés
  par `sub` après leur 1ère connexion). Le rôle `org_admin` est stocké mais pas
  encore utilisé pour autoriser (viendra avec le self-service).

barreau 3 : crée les orgs/secrets. Tant qu'aucune org n'existe, inerte. La
visibilité par entitlement (org_entitlements) est câblée au barreau 4.
"""
from __future__ import annotations

import logging

from fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, connectors, db, group_store, org_store, tool_registry
from ..access import ORG_SHAREABLE_PROVIDERS
from ..tool_visibility import ADMIN_GRANT_ONLY_NAMESPACES

logger = logging.getLogger(__name__)


def _err(message: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=message))


def _require_sub() -> str:
    sub = access.current_user_sub_from_token()
    if not sub:
        raise _err("Auth requise — ces tools ne marchent que sur le transport HTTP authentifié.")
    return sub


def _require_admin() -> str:
    sub = _require_sub()
    if access.get_user_role(sub) != access.ADMIN:
        raise _err("Réservé au platform admin.")
    return sub


def register(mcp: FastMCP) -> None:
    # orgs (list/use/admin CRUD + reads) : migrés en capacités org.* (ADR 0009,
    # barreaux 1→2d) — fournis par les adaptateurs MCP/REST. Ne restent ici que
    # la doctrine + les instructions (skills), hors scope couche capacité.

    @mcp.tool()
    async def get_claude_md(ctx: Context) -> dict:
        """Doctrine opératoire de ton organisation — appelle-la EN DÉBUT DE SESSION.

        Renvoie, pour ton org active ET ton groupe actif (département) le cas échéant :
        - `doctrine` : la doctrine de base de l'ORG (workflows validés, l'ordre des
          outils, gardes-fous, vocabulaire).
        - `group_doctrine` : la doctrine du GROUPE actif (département), à appliquer
          EN COMPLÉMENT de celle de l'org. Vide si pas de groupe actif.
        - `instructions` : l'index unifié des instructions nommées (skills) — chaque
          entrée porte `slug`, `title`, `description` et `scope` (`org`|`group`).
          Charge le détail d'une skill à la demande avec
          `oto_get_instruction(slug, scope=…)` (ou cherche avec `oto_search_instructions`).

        Tout est vide (et sans erreur) si tu n'as ni org ni groupe actifs, ou si rien
        n'a été posé : continue normalement avec les instructions du serveur.
        """
        sub = _require_sub()
        org_id = org_store.get_active_org(sub)
        if org_id is None:
            return {"org_id": None, "org": None, "doctrine": "",
                    "group_id": None, "group": None, "group_doctrine": "",
                    "instructions": [], "referenced_tools": []}
        o = org_store.get_org(org_id)
        base = org_store.get_instruction(org_id, org_store.BASE_SLUG)
        index = [
            {"slug": i["slug"], "title": i["title"], "description": i["description"], "scope": "org"}
            for i in org_store.list_instructions(org_id)
        ]
        group_id = group_store.get_active_group(sub)
        group_name, group_doctrine = None, ""
        if group_id is not None:
            g = group_store.get_group(group_id)
            group_name = g["name"] if g else None
            gbase = group_store.get_group_instruction(group_id, org_store.BASE_SLUG)
            group_doctrine = (gbase or {}).get("body_md", "") or ""
            index += [
                {"slug": i["slug"], "title": i["title"], "description": i["description"],
                 "scope": "group"}
                for i in group_store.list_group_instructions(group_id)
            ]
        doctrine_body = (base or {}).get("body_md", "") or ""
        return {
            "org_id": org_id,
            "org": o["name"] if o else None,
            "doctrine": doctrine_body,
            "group_id": group_id,
            "group": group_name,
            "group_doctrine": group_doctrine,
            "instructions": index,
            # Manifeste résolu des outils cités par la doctrine de base + celle du
            # groupe (ADR 0014) : noms canoniques, descriptions tirées des outils,
            # et drift signalé (`status=missing`). Vide si rien n'est cité.
            "referenced_tools": await tool_registry.manifest_for(mcp, doctrine_body, group_doctrine),
        }

    @mcp.tool()
    async def oto_list_instructions(ctx: Context) -> dict:
        """List your active org's AND active group's named instructions (skills) —
        slug/title/description/version/scope, no body. Fetch one with
        `oto_get_instruction(slug, scope)`. Excludes the base doctrine (served by
        `get_claude_md`)."""
        sub = _require_sub()
        org_id = org_store.get_active_org(sub)
        if org_id is None:
            return {"org_id": None, "instructions": []}
        out = [{**i, "scope": "org"} for i in org_store.list_instructions(org_id)]
        group_id = group_store.get_active_group(sub)
        if group_id is not None:
            out += [{**i, "scope": "group"}
                    for i in group_store.list_group_instructions(group_id)]
        return {"org_id": org_id, "group_id": group_id, "instructions": out}

    @mcp.tool()
    async def oto_get_instruction(
        slug: str, ctx: Context, version: int | None = None, scope: str = "org"
    ) -> dict:
        """Full markdown of one named instruction (skill) of your active org or group.

        Args:
            slug: the instruction slug (see `oto_list_instructions` / the index in
                `get_claude_md`). `claude_md` returns the base doctrine.
            version: optional — a past version number (default = latest).
            scope: `org` (default) or `group` — which level to read from. The index
                returned by `get_claude_md` tags each skill with its scope.
        """
        sub = _require_sub()
        if scope == "group":
            group_id = group_store.get_active_group(sub)
            if group_id is None:
                raise _err("Pas de groupe actif — vois `oto_list_orgs`/`oto_use_group`.")
            instr = group_store.get_group_instruction(group_id, slug, version)
            scope_id = {"group_id": group_id}
        else:
            org_id = org_store.get_active_org(sub)
            if org_id is None:
                raise _err("Pas d'org active — vois `oto_list_orgs`.")
            instr = org_store.get_instruction(org_id, slug, version)
            scope_id = {"org_id": org_id}
        if not instr:
            raise _err(
                f"Aucune instruction `{org_store.normalize_slug(slug)}` (scope {scope})"
                + (f" en version {version}" if version is not None else "")
                + ". Vois `oto_list_instructions`."
            )
        return {
            **scope_id, "scope": scope,
            "slug": instr["slug"], "title": instr["title"],
            "description": instr["description"], "version": instr["version"],
            "body_md": instr["body_md"],
            # Manifeste résolu des outils cités par ce skill (ADR 0014).
            "referenced_tools": await tool_registry.manifest_for(mcp, instr["body_md"]),
        }

    @mcp.tool()
    async def oto_search_instructions(query: str, ctx: Context) -> dict:
        """Search your active org's AND active group's instructions (title/description/
        body). Returns matches with a snippet + `scope`; fetch a full body with
        `oto_get_instruction(slug, scope)`."""
        sub = _require_sub()
        org_id = org_store.get_active_org(sub)
        if org_id is None:
            return {"org_id": None, "results": []}
        results = [{**r, "scope": "org"} for r in org_store.search_instructions(org_id, query)]
        group_id = group_store.get_active_group(sub)
        if group_id is not None:
            results += [{**r, "scope": "group"}
                        for r in group_store.search_group_instructions(group_id, query)]
        return {"org_id": org_id, "group_id": group_id, "results": results}

    # --- platform_admin : provisioning --------------------------------------

    # Tout le CRUD + lectures orgs (create, members, secrets, entitlements,
    # list/get) : migrés en capacités org.* (ADR 0009, barreaux 2→2d).

    # --- doctrine + instructions : prose métier versionnée (get_claude_md) -----

    @mcp.tool()
    async def oto_admin_set_doctrine(org_id: int, body_md: str, ctx: Context) -> dict:
        """[platform admin] Set/replace an org's BASE doctrine (slug `claude_md`).

        Served verbatim to the org's members by `get_claude_md()` at session start:
        validated workflows, vocabulary, business rules — for orgs that drive oto
        without a dedicated app (e.g. an accounting workflow over gocardless/
        pennylane/mm). Named skills go through `oto_admin_set_instruction`. Each
        write bumps the version and archives a snapshot (history/revert).

        Args:
            org_id: target org.
            body_md: the full doctrine markdown (replaces the current one).
        """
        admin = _require_admin()
        if not org_store.get_org(org_id):
            raise _err(f"Org #{org_id} inconnue.")
        if not (body_md or "").strip():
            raise _err("body_md vide.")
        version = org_store.set_instruction(org_id, org_store.BASE_SLUG, body_md, set_by=admin)
        return {"org_id": org_id, "slug": org_store.BASE_SLUG, "version": version, "set": True,
                **await tool_registry.write_check(mcp, body_md)}

    @mcp.tool()
    async def oto_admin_set_instruction(
        org_id: int, slug: str, body_md: str, ctx: Context,
        title: str | None = None, description: str | None = None,
    ) -> dict:
        """[platform admin] Create/update a NAMED instruction (skill) for an org.

        Like a skill: members see it in the `get_claude_md` index and load it with
        `oto_get_instruction(slug)`. Each write bumps the version and archives a
        snapshot. Re-posting the same slug updates it (keep title/description by
        leaving them None).

        Args:
            org_id: target org.
            slug: identifier (normalized to [a-z0-9_-]); `claude_md` is reserved
                for the base doctrine (use `oto_admin_set_doctrine`).
            body_md: the instruction markdown.
            title: short human title (optional; kept if omitted on update).
            description: the "when to use" surfaced in the index (optional).
        """
        admin = _require_admin()
        if not org_store.get_org(org_id):
            raise _err(f"Org #{org_id} inconnue.")
        norm = org_store.normalize_slug(slug)
        if not norm:
            raise _err("slug vide ou invalide (attendu [a-z0-9_-]).")
        if norm == org_store.BASE_SLUG:
            raise _err("`claude_md` = doctrine de base : utilise `oto_admin_set_doctrine`.")
        if not (body_md or "").strip():
            raise _err("body_md vide.")
        version = org_store.set_instruction(
            org_id, norm, body_md, title=title, description=description, set_by=admin)
        return {"org_id": org_id, "slug": norm, "version": version, "set": True,
                **await tool_registry.write_check(mcp, body_md)}

    @mcp.tool()
    async def oto_admin_list_instructions(org_id: int, ctx: Context) -> dict:
        """[platform admin] List all of an org's instructions INCLUDING the base
        doctrine (slug/title/description/version, no body)."""
        _require_admin()
        return {
            "org_id": org_id,
            "instructions": org_store.list_instructions(org_id, include_base=True),
        }

    @mcp.tool()
    async def oto_admin_get_instruction(
        org_id: int, slug: str, ctx: Context, version: int | None = None
    ) -> dict:
        """[platform admin] Read back an org's instruction at any version (body +
        metadata). slug `claude_md` = base doctrine. Default = latest version."""
        _require_admin()
        instr = org_store.get_instruction(org_id, slug, version)
        if not instr:
            raise _err(
                f"Aucune instruction `{org_store.normalize_slug(slug)}`"
                + (f" en version {version}" if version is not None else "")
                + f" pour l'org #{org_id}."
            )
        return {"org_id": org_id, **instr}

    @mcp.tool()
    async def oto_admin_list_instruction_versions(org_id: int, slug: str, ctx: Context) -> dict:
        """[platform admin] Version history of one instruction (metadata per
        version, latest first). slug `claude_md` = base doctrine."""
        _require_admin()
        return {
            "org_id": org_id,
            "slug": org_store.normalize_slug(slug),
            "versions": org_store.list_instruction_versions(org_id, slug),
        }

    @mcp.tool()
    async def oto_admin_revert_instruction(
        org_id: int, slug: str, version: int, ctx: Context
    ) -> dict:
        """[platform admin] Restore an older version as a NEW version (history kept).

        Args:
            org_id: target org.
            slug: the instruction (`claude_md` = base doctrine).
            version: the past version number to restore (see
                `oto_admin_list_instruction_versions`).
        """
        admin = _require_admin()
        old = org_store.get_instruction(org_id, slug, version)
        if not old:
            raise _err(
                f"Pas de version {version} pour `{org_store.normalize_slug(slug)}` "
                f"(org #{org_id})."
            )
        new_version = org_store.set_instruction(
            org_id, slug, old["body_md"], title=old["title"],
            description=old["description"], set_by=admin)
        return {
            "org_id": org_id, "slug": org_store.normalize_slug(slug),
            "version": new_version, "reverted_from": version,
        }

    @mcp.tool()
    async def oto_admin_delete_instruction(org_id: int, slug: str, ctx: Context) -> dict:
        """[platform admin] Delete an instruction and its history. slug `claude_md`
        = base doctrine (get_claude_md then serves an empty doctrine)."""
        _require_admin()
        deleted = org_store.delete_instruction(org_id, slug)
        return {"org_id": org_id, "slug": org_store.normalize_slug(slug), "deleted": deleted}

    # entitlements (grant/revoke/list) : migrés en capacités org.entitlement.*
    # (ADR 0009 barreaux 2c/2d).
