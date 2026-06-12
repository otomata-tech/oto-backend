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

from .. import access, connectors, db, org_store
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


def _resolve_target_sub(target: str) -> str:
    """Email (d'un user déjà connecté au moins une fois) ou sub direct."""
    if "@" in target:
        user = db.get_user_by_email(target)
        if not user:
            raise _err(
                f"Aucun user connu avec l'email `{target}`. Il doit se connecter "
                f"une fois (magic link Logto) avant de pouvoir être ajouté."
            )
        return user["sub"]
    return target


def _resolve_org_for_user(sub: str, org: str) -> int:
    """Résout `org` (id numérique ou nom) parmi les orgs DU sub. Erreur si
    inconnu/ambigu — jamais de choix implicite (mauvaise org = mauvais secret)."""
    org = (org or "").strip()
    mine = org_store.list_orgs_for_user(sub)
    if org.isdigit():
        oid = int(org)
        if any(o["org_id"] == oid for o in mine):
            return oid
        raise _err(f"Tu n'es membre d'aucune org #{oid}. Vois `oto_list_orgs`.")
    matches = [o for o in mine if o["name"].lower() == org.lower()]
    if len(matches) == 1:
        return matches[0]["org_id"]
    if not matches:
        raise _err(f"Aucune de tes orgs ne s'appelle `{org}`. Vois `oto_list_orgs`.")
    raise _err(f"Plusieurs de tes orgs s'appellent `{org}` — utilise l'id (oto_list_orgs).")


def register(mcp: FastMCP) -> None:
    # --- user : voir / basculer son org active ------------------------------

    @mcp.tool()
    async def oto_list_orgs(ctx: Context) -> dict:
        """List the organizations you belong to and which one is active.

        Acting in an org means its shared secrets (org_secrets) resolve for you.
        Switch with `oto_use_org`.
        """
        sub = _require_sub()
        orgs = org_store.list_orgs_for_user(sub)
        active = next((o["org_id"] for o in orgs if o["is_active"]), None)
        return {
            "orgs": [
                {
                    "org_id": o["org_id"],
                    "name": o["name"],
                    "role": o["org_role"],
                    "active": o["is_active"],
                }
                for o in orgs
            ],
            "active_org": active,
        }

    @mcp.tool()
    async def oto_use_org(org: str, ctx: Context) -> dict:
        """Switch your active organization (by id or name).

        The active org decides which shared secrets resolve for your tool calls.
        It is global to your account (not per-session): other open sessions
        resolve secrets from the new org on their next call too.

        Args:
            org: org id (e.g. "3") or exact org name.
        """
        sub = _require_sub()
        org_id = _resolve_org_for_user(sub, org)
        if not org_store.set_active_org(sub, org_id):
            raise _err(f"Tu n'es pas membre de l'org #{org_id}.")
        o = org_store.get_org(org_id)
        return {"active_org": org_id, "name": o["name"] if o else None}

    @mcp.tool()
    async def get_claude_md(ctx: Context) -> dict:
        """Doctrine opératoire de ton organisation — appelle-la EN DÉBUT DE SESSION.

        Renvoie deux choses pour ton org active :
        - `doctrine` : la doctrine de base rédigée par le métier (workflows validés,
          l'ordre des outils, gardes-fous, vocabulaire). À suivre quand ton org
          pilote oto sans produit applicatif dédié.
        - `instructions` : l'index des instructions nommées (skills) de l'org —
          slug + titre + quand-l'utiliser. Charge le détail d'une skill à la demande
          avec `oto_get_instruction(slug)` (ou cherche avec `oto_search_instructions`).

        Tout est vide (et sans erreur) si tu n'as pas d'org active ou si elle n'a
        rien posé : continue normalement avec les instructions du serveur.
        """
        sub = _require_sub()
        org_id = org_store.get_active_org(sub)
        if org_id is None:
            return {"org_id": None, "org": None, "doctrine": "", "instructions": []}
        o = org_store.get_org(org_id)
        base = org_store.get_instruction(org_id, org_store.BASE_SLUG)
        index = org_store.list_instructions(org_id)
        return {
            "org_id": org_id,
            "org": o["name"] if o else None,
            "doctrine": (base or {}).get("body_md", "") or "",
            "instructions": [
                {"slug": i["slug"], "title": i["title"], "description": i["description"]}
                for i in index
            ],
        }

    @mcp.tool()
    async def oto_list_instructions(ctx: Context) -> dict:
        """List your active org's named instructions (skills) — slug/title/description/
        version, no body. Fetch one with `oto_get_instruction`. Excludes the base
        doctrine (that one is served by `get_claude_md`)."""
        sub = _require_sub()
        org_id = org_store.get_active_org(sub)
        if org_id is None:
            return {"org_id": None, "instructions": []}
        return {"org_id": org_id, "instructions": org_store.list_instructions(org_id)}

    @mcp.tool()
    async def oto_get_instruction(slug: str, ctx: Context, version: int | None = None) -> dict:
        """Full markdown of one named instruction (skill) of your active org.

        Args:
            slug: the instruction slug (see `oto_list_instructions` / the index in
                `get_claude_md`). `claude_md` returns the base doctrine.
            version: optional — a past version number (default = latest).
        """
        sub = _require_sub()
        org_id = org_store.get_active_org(sub)
        if org_id is None:
            raise _err("Pas d'org active — vois `oto_list_orgs`.")
        instr = org_store.get_instruction(org_id, slug, version)
        if not instr:
            raise _err(
                f"Aucune instruction `{org_store.normalize_slug(slug)}`"
                + (f" en version {version}" if version is not None else "")
                + " pour ton org. Vois `oto_list_instructions`."
            )
        return {
            "org_id": org_id,
            "slug": instr["slug"],
            "title": instr["title"],
            "description": instr["description"],
            "version": instr["version"],
            "body_md": instr["body_md"],
        }

    @mcp.tool()
    async def oto_search_instructions(query: str, ctx: Context) -> dict:
        """Search your active org's instructions (title/description/body). Returns
        matches with a snippet; fetch a full body with `oto_get_instruction`."""
        sub = _require_sub()
        org_id = org_store.get_active_org(sub)
        if org_id is None:
            return {"org_id": None, "results": []}
        return {"org_id": org_id, "results": org_store.search_instructions(org_id, query)}

    # --- platform_admin : provisioning --------------------------------------

    @mcp.tool()
    async def oto_admin_create_org(name: str, ctx: Context) -> dict:
        """[platform admin] Create an organization (perimeter). Returns its id."""
        admin = _require_admin()
        org_id = org_store.create_org(name, created_by=admin)
        return {"org_id": org_id, "name": name.strip()}

    @mcp.tool()
    async def oto_admin_list_orgs(ctx: Context) -> dict:
        """[platform admin] List all organizations."""
        _require_admin()
        return {"orgs": org_store.list_all_orgs()}

    @mcp.tool()
    async def oto_admin_add_org_member(
        org_id: int, target: str, ctx: Context, org_role: str = "org_member"
    ) -> dict:
        """[platform admin] Add a member to an org (org_member | org_admin).

        Auto-activates the org for the member if it's their first one.

        Args:
            org_id: target org.
            target: Logto `sub`, or email of a user who connected at least once.
            org_role: `org_member` (default) or `org_admin`.
        """
        _require_admin()
        if not org_store.get_org(org_id):
            raise _err(f"Org #{org_id} inconnue.")
        if org_role not in org_store.ORG_ROLES:
            raise _err(f"org_role invalide `{org_role}` (attendu: {org_store.ORG_ROLES}).")
        target_sub = _resolve_target_sub(target)
        org_store.add_org_member(org_id, target_sub, org_role)
        return {"org_id": org_id, "sub": target_sub, "role": org_role}

    @mcp.tool()
    async def oto_admin_remove_org_member(org_id: int, target: str, ctx: Context) -> dict:
        """[platform admin] Remove a member from an org.

        Note: an open MCP session of that member keeps resolving the org's
        secrets until its next handshake (revoke is lazy). For a hard cutoff,
        rotate the org secret at the provider.
        """
        _require_admin()
        target_sub = _resolve_target_sub(target)
        removed = org_store.remove_org_member(org_id, target_sub)
        return {"org_id": org_id, "sub": target_sub, "removed": removed}

    @mcp.tool()
    async def oto_admin_list_org_members(org_id: int, ctx: Context) -> dict:
        """[platform admin] List members of an org (sub, role, active flag)."""
        _require_admin()
        return {"org_id": org_id, "members": org_store.list_org_members(org_id)}

    @mcp.tool()
    async def oto_admin_set_org_secret(
        org_id: int, provider: str, api_key: str, ctx: Context,
        base_url: str | None = None,
    ) -> dict:
        """[platform admin] Set/rotate an org's shared account credential.

        Only org-shareable providers (account credentials usable by every
        member). Personal-session providers (slack/linkedin/google/whatsapp)
        are refused — they are physiologically per-user.

        Args:
            org_id: target org.
            provider: one of the org-shareable providers (attio, pennylane,
                serper, hunter, sirene, lemlist, kaspr, fullenrich) or a
                remote connector (mm).
            api_key: the credential (remote connector: the bridge M2M token —
                never the client system's own secret, which stays in the bridge).
            base_url: remote connectors only — bridge endpoint (required).
        """
        admin = _require_admin()
        if not org_store.get_org(org_id):
            raise _err(f"Org #{org_id} inconnue.")
        if provider not in ORG_SHAREABLE_PROVIDERS:
            raise _err(
                f"`{provider}` n'est pas org-partageable (credential de session "
                f"personnel ou inconnu). Partageables : {sorted(ORG_SHAREABLE_PROVIDERS)}."
            )
        c = connectors.connector_for_provider(provider)
        if c is not None and c.kind == "remote":
            if not base_url:
                raise _err(f"`{provider}` est un connecteur remote : `base_url` (endpoint du bridge) requis.")
            meta = {"base_url": base_url.rstrip("/")}
        else:
            if base_url:
                raise _err(f"`base_url` n'a de sens que pour un connecteur remote, pas `{provider}`.")
            meta = None
        org_store.set_org_secret(org_id, provider, api_key, set_by=admin, meta=meta)
        return {"org_id": org_id, "provider": provider, "set": True}

    @mcp.tool()
    async def oto_admin_delete_org_secret(org_id: int, provider: str, ctx: Context) -> dict:
        """[platform admin] Remove an org's shared secret for a provider."""
        _require_admin()
        deleted = org_store.delete_org_secret(org_id, provider)
        return {"org_id": org_id, "provider": provider, "deleted": deleted}

    @mcp.tool()
    async def oto_admin_list_org_secrets(org_id: int, ctx: Context) -> dict:
        """[platform admin] List providers an org has a shared secret for
        (never returns the keys themselves)."""
        _require_admin()
        return {"org_id": org_id, "secrets": org_store.list_org_secrets(org_id)}

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
        return {"org_id": org_id, "slug": org_store.BASE_SLUG, "version": version, "set": True}

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
        return {"org_id": org_id, "slug": norm, "version": version, "set": True}

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

    # --- entitlements : plafond plateforme -> org sur les namespaces gouvernés -

    @mcp.tool()
    async def oto_admin_grant_org_entitlement(org_id: int, namespace: str, ctx: Context) -> dict:
        """[platform admin] Entitle an org to a controlled (grant-only) namespace.

        Unlocks that namespace's tools for the org's members (e.g. `mm`,
        `gocardless`). Only controlled namespaces are accepted — by-right
        namespaces (attio, fr, serper…) need no entitlement.
        """
        admin = _require_admin()
        if not org_store.get_org(org_id):
            raise _err(f"Org #{org_id} inconnue.")
        if namespace not in ADMIN_GRANT_ONLY_NAMESPACES:
            raise _err(
                f"`{namespace}` n'est pas un namespace gouverné. "
                f"Gouvernés : {sorted(ADMIN_GRANT_ONLY_NAMESPACES)}. "
                f"Les autres sont visibles de droit (pas d'entitlement requis)."
            )
        org_store.grant_org_entitlement(org_id, namespace, granted_by=admin)
        return {"org_id": org_id, "namespace": namespace, "granted": True}

    @mcp.tool()
    async def oto_admin_revoke_org_entitlement(org_id: int, namespace: str, ctx: Context) -> dict:
        """[platform admin] Revoke an org's entitlement to a controlled namespace.

        Lazy on open sessions (members keep it until their next handshake)."""
        _require_admin()
        existed = org_store.revoke_org_entitlement(org_id, namespace)
        return {"org_id": org_id, "namespace": namespace, "revoked": existed}

    @mcp.tool()
    async def oto_admin_list_org_entitlements(org_id: int, ctx: Context) -> dict:
        """[platform admin] List an org's controlled-namespace entitlements."""
        _require_admin()
        return {"org_id": org_id, "entitlements": org_store.list_org_entitlements(org_id)}
