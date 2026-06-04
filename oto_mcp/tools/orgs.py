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

from .. import access, db, org_store
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
        org_id: int, provider: str, api_key: str, ctx: Context
    ) -> dict:
        """[platform admin] Set/rotate an org's shared account credential.

        Only org-shareable providers (account credentials usable by every
        member). Personal-session providers (slack/linkedin/google/whatsapp)
        are refused — they are physiologically per-user.

        Args:
            org_id: target org.
            provider: one of the org-shareable providers (attio, pennylane,
                serper, hunter, sirene, lemlist, kaspr, fullenrich).
            api_key: the credential.
        """
        admin = _require_admin()
        if not org_store.get_org(org_id):
            raise _err(f"Org #{org_id} inconnue.")
        if provider not in ORG_SHAREABLE_PROVIDERS:
            raise _err(
                f"`{provider}` n'est pas org-partageable (credential de session "
                f"personnel ou inconnu). Partageables : {sorted(ORG_SHAREABLE_PROVIDERS)}."
            )
        org_store.set_org_secret(org_id, provider, api_key, set_by=admin)
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
