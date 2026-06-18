"""Meta-tools du palier organization — doctrine (prose métier versionnée).

Le CRUD/lectures orgs (create, members, secrets, entitlements, switch d'org
active) est migré en **capacités** `org.*` (ADR 0009) — fourni par les
adaptateurs MCP/REST. Ne restent ici que les outils **doctrine**.

Surface doctrine unifiée (4 outils, « moins d'outils plus d'args ») : un `org_id`
optionnel fond membre↔platform-admin :
- **absent** → ton **org active** (lecture = membre ; écriture = org_admin) ;
- **présent** → cette org par id, **réservé platform_admin** (l'opérateur
  provisionne/édite n'importe quelle org).

La doctrine de **groupe** (département, ADR 0012) est *lisible* (`scope="group"`) ;
son écriture reste dans sa capability dédiée.
"""
from __future__ import annotations

import logging

from fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, group_store, org_store, tool_registry

logger = logging.getLogger(__name__)


def _err(message: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=message))


def _require_sub() -> str:
    sub = access.current_user_sub_from_token()
    if not sub:
        raise _err("Auth requise — ces tools ne marchent que sur le transport HTTP authentifié.")
    return sub


def _is_platform_admin(sub: str) -> bool:
    # Agir sur la doctrine d'une org tierce (org_id explicite) = escalade en masse
    # → réservé au super_admin (pas à l'admin opérationnel).
    return access.is_super_admin(sub)


def _resolve_org_read(sub: str, org_id: int | None) -> int | None:
    """org_id absent → org active (lecture membre ; peut être None = perso/pas d'org) ;
    présent → cette org, **platform admin requis**."""
    if org_id is None:
        return org_store.get_active_org(sub)
    if not _is_platform_admin(sub):
        raise _err("`org_id` (lire la doctrine d'une autre org) est réservé au platform admin.")
    if not org_store.get_org(org_id):
        raise _err(f"Org #{org_id} inconnue.")
    return org_id


def _resolve_org_write(sub: str, org_id: int | None) -> int:
    """org_id absent → org active (**org_admin requis**) ; présent → cette org,
    **platform admin requis**."""
    if org_id is not None:
        if not _is_platform_admin(sub):
            raise _err("`org_id` (écrire la doctrine d'une autre org) est réservé au platform admin.")
        if not org_store.get_org(org_id):
            raise _err(f"Org #{org_id} inconnue.")
        return org_id
    oid = org_store.get_active_org(sub)
    if oid is None:
        raise _err("Pas d'org active — `oto_use_org` d'abord (ou passe `org_id` si platform admin).")
    if not _is_platform_admin(sub) and org_store.get_org_role(oid, sub) != "org_admin":
        raise _err("Écrire la doctrine de ton org requiert le rôle org_admin.")
    return oid


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def oto_get_doctrine(
        ctx: Context, slug: str | None = None, org_id: int | None = None,
        scope: str = "org", version: int | None = None, with_history: bool = False,
    ) -> dict:
        """Doctrine opératoire de ton organisation. APPELLE-LA EN DÉBUT DE SESSION
        (sans argument) : tu obtiens la doctrine de base (workflows validés, ordre
        des outils, gardes-fous, vocabulaire) + l'index des doctrines nommées
        (skills) à charger à la demande.

        Args:
            slug: omis = doctrine de BASE de ton org active (+ celle de ton
                département actif) + l'index des doctrines nommées. Donné = le
                markdown complet de cette doctrine nommée.
            org_id: [PLATFORM ADMIN] lire la doctrine d'une AUTRE org par id. Omis =
                ton org active.
            scope: `org` (défaut) ou `group` (ton département actif) — pour cibler
                une doctrine nommée d'un niveau précis.
            version: une version passée (défaut = la dernière).
            with_history: si vrai, ajoute la liste des versions de la doctrine ciblée
                (scope org).

        Vide (sans erreur) si tu n'as pas d'org active : continue avec les
        instructions du serveur.
        """
        sub = _require_sub()
        target = _resolve_org_read(sub, org_id)

        # Début de session : doctrine de base + index.
        if slug is None:
            if target is None:
                return {"org_id": None, "org": None, "doctrine": "", "group_id": None,
                        "group": None, "group_doctrine": "", "doctrines": [], "referenced_tools": []}
            o = org_store.get_org(target)
            base = org_store.get_instruction(target, org_store.BASE_SLUG)
            index = [{"slug": i["slug"], "title": i["title"],
                      "description": i["description"], "scope": "org"}
                     for i in org_store.list_instructions(target)]
            group_id = group_store.get_active_group(sub) if org_id is None else None
            group_name, group_doctrine = None, ""
            if group_id is not None:
                g = group_store.get_group(group_id)
                group_name = g["name"] if g else None
                gbase = group_store.get_group_instruction(group_id, org_store.BASE_SLUG)
                group_doctrine = (gbase or {}).get("body_md", "") or ""
                index += [{"slug": i["slug"], "title": i["title"],
                           "description": i["description"], "scope": "group"}
                          for i in group_store.list_group_instructions(group_id)]
            doctrine_body = (base or {}).get("body_md", "") or ""
            return {
                "org_id": target, "org": o["name"] if o else None, "doctrine": doctrine_body,
                "group_id": group_id, "group": group_name, "group_doctrine": group_doctrine,
                "doctrines": index,
                "referenced_tools": await tool_registry.manifest_for(mcp, doctrine_body, group_doctrine),
            }

        # Une doctrine nommée précise.
        if scope == "group" and org_id is None:
            group_id = group_store.get_active_group(sub)
            if group_id is None:
                raise _err("Pas de département actif — vois `oto_use_group`.")
            instr = group_store.get_group_instruction(group_id, slug, version)
            scope_ref: dict = {"group_id": group_id}
        else:
            if target is None:
                raise _err("Pas d'org active — vois `oto_use_org`.")
            instr = org_store.get_instruction(target, slug, version)
            scope_ref = {"org_id": target}
        if not instr:
            raise _err(f"Aucune doctrine `{org_store.normalize_slug(slug)}` (scope {scope})"
                       + (f" en version {version}" if version is not None else "")
                       + ". Vois `oto_list_doctrines`.")
        out = {**scope_ref, "scope": scope, "slug": instr["slug"], "title": instr["title"],
               "description": instr["description"], "version": instr["version"],
               "body_md": instr["body_md"],
               "referenced_tools": await tool_registry.manifest_for(mcp, instr["body_md"])}
        if with_history and "org_id" in scope_ref:
            out["versions"] = org_store.list_instruction_versions(target, slug)
        return out

    @mcp.tool()
    async def oto_list_doctrines(
        ctx: Context, query: str | None = None, org_id: int | None = None,
        scope: str | None = None,
    ) -> dict:
        """Catalogue des doctrines nommées (skills) de ton org active (+ département
        actif) — slug/title/description/version, sans le corps. Charge une doctrine
        avec `oto_get_doctrine(slug)`.

        Args:
            query: optionnel — recherche (titre/description/corps). Omis = tout le catalogue.
            org_id: [PLATFORM ADMIN] lister une AUTRE org par id (inclut sa doctrine de base).
            scope: `org` ou `group` pour restreindre ; omis = les deux.
        """
        sub = _require_sub()
        target = _resolve_org_read(sub, org_id)
        if target is None:
            return {"org_id": None, "doctrines": []}
        out: list = []
        if scope in (None, "org"):
            if query:
                rows = org_store.search_instructions(target, query, include_base=org_id is not None)
            else:
                rows = org_store.list_instructions(target, include_base=org_id is not None)
            out += [{**r, "scope": "org"} for r in rows]
        group_id = group_store.get_active_group(sub) if (org_id is None and scope in (None, "group")) else None
        if group_id is not None:
            rows = (group_store.search_group_instructions(group_id, query) if query
                    else group_store.list_group_instructions(group_id))
            out += [{**r, "scope": "group"} for r in rows]
        return {"org_id": target, "group_id": group_id, "doctrines": out}

    @mcp.tool()
    async def oto_set_doctrine(
        ctx: Context, body_md: str | None = None, slug: str | None = None,
        org_id: int | None = None, title: str | None = None,
        description: str | None = None, from_version: int | None = None,
    ) -> dict:
        """Écrit la doctrine de ton org (org_admin) — ou d'une autre org (platform
        admin via `org_id`). Chaque écriture incrémente la version + archive un
        snapshot (historique/revert).

        Args:
            body_md: le markdown. Requis SAUF si `from_version` est fourni.
            slug: omis = doctrine de BASE ; donné = une doctrine nommée (skill).
            org_id: [PLATFORM ADMIN] écrire une AUTRE org par id. Omis = ton org
                active (org_admin requis).
            title / description: pour une doctrine nommée (le `description` = le
                « quand l'utiliser » affiché dans l'index). Conservés si omis à l'update.
            from_version: restaure une version passée comme NOUVELLE version (revert)
                — `body_md` est alors ignoré.
        """
        sub = _require_sub()
        target = _resolve_org_write(sub, org_id)
        norm = org_store.normalize_slug(slug) if slug else org_store.BASE_SLUG
        if not norm:
            raise _err("slug invalide (attendu [a-z0-9_-]).")
        if from_version is not None:
            old = org_store.get_instruction(target, norm, from_version)
            if not old:
                raise _err(f"Pas de version {from_version} pour `{norm}` (org #{target}).")
            body_md, title, description = old["body_md"], old["title"], old["description"]
        if not (body_md or "").strip():
            raise _err("body_md vide (ou fournis `from_version`).")
        if norm == org_store.BASE_SLUG:
            version = org_store.set_instruction(target, org_store.BASE_SLUG, body_md, set_by=sub)
        else:
            version = org_store.set_instruction(target, norm, body_md, title=title,
                                                description=description, set_by=sub)
        return {"org_id": target, "slug": norm, "version": version, "set": True,
                **({"reverted_from": from_version} if from_version is not None else {}),
                **await tool_registry.write_check(mcp, body_md)}

    @mcp.tool()
    async def oto_delete_doctrine(slug: str, ctx: Context, org_id: int | None = None) -> dict:
        """Supprime une doctrine et son historique. Passe le slug EXACT (pour la
        doctrine de base, slug = la valeur réservée). Ton org active (org_admin) ou
        une autre org (platform admin via `org_id`)."""
        sub = _require_sub()
        target = _resolve_org_write(sub, org_id)
        norm = org_store.normalize_slug(slug)
        if not norm:
            raise _err("slug requis.")
        deleted = org_store.delete_instruction(target, norm)
        return {"org_id": target, "slug": norm, "deleted": deleted}
