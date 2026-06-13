"""Routes REST `/api/*` du palier organization — consommé par le SPA `account/`
(repo oto-app : sections `/org` self-service + `/admin`).

Projection HTTP fine au-dessus des fonctions de service déjà derrière les
meta-tools MCP `oto_admin_*org*` / `oto_list_orgs` (`org_store`, `db`,
`credentials_store`). **Aucune logique métier nouvelle** : seulement l'adaptateur
HTTP + le gating. Le SPA ne parle pas MCP, il parle REST + bearer Logto.

Deux surfaces :

- **self-service** (`/api/me/orgs`, `/api/orgs/{id}/*`) : un membre voit son org,
  un `org_admin` gère membres + secrets de SON org.
- **platform admin** (`/api/admin/orgs/*`, `/api/admin/namespace-grants*`) :
  provisionne tout (créer une org, entitlements, namespace grants per-user).

Deux systèmes de rôles distincts : plateforme (`guest|member|admin`) vs org
(`org_member|org_admin`). Le platform admin est toujours autorisé sur les
surfaces org (il provisionne).

Le listing des secrets réutilise `org_store.list_org_secrets` (qui lit le coffre
canonique `credentials_store`, jamais la clé, et porte le `base_url` des
connecteurs remote depuis `meta`).
"""
from __future__ import annotations

from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import access, connectors, db, org_store
from .tool_visibility import ADMIN_GRANT_ONLY_NAMESPACES

AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
) -> list[Route]:

    # --- projections (enrichissement email/base_url, jamais le secret) --------

    def _members(org_id: int) -> list[dict]:
        """Membres enrichis de l'email/nom (depuis `users`)."""
        out = []
        for m in org_store.list_org_members(org_id):
            u = db.get_user(m["sub"]) or {}
            out.append({
                "sub": m["sub"],
                "email": u.get("email"),
                "name": u.get("name"),
                "role": m["org_role"],
                "active": m["is_active"],
            })
        return out

    def _org_brief(org: dict, *, my_role: str | None = None) -> dict:
        brief = {
            "id": org["id"],
            "name": org["name"],
            "member_count": len(org_store.list_org_members(org["id"])),
        }
        # Côté self-service, le SPA gate l'édition sur `org.my_role` du détail
        # (OrgView.canManage). Présent seulement quand le requérant est membre.
        if my_role is not None:
            brief["my_role"] = my_role
        return brief

    def _org_detail(org: dict, *, with_entitlements: bool, my_role: str | None = None) -> dict:
        payload = {
            "org": _org_brief(org, my_role=my_role),
            "members": _members(org["id"]),
            "secrets": org_store.list_org_secrets(org["id"]),
        }
        if with_entitlements:
            payload["entitlements"] = [
                {"namespace": e["namespace"], "granted_at": e["granted_at"]}
                for e in org_store.list_org_entitlements(org["id"])
            ]
        return payload

    # --- gating ---------------------------------------------------------------

    def _is_platform_admin(sub: str) -> bool:
        return access.get_user_role(sub) == access.ADMIN

    def _is_org_admin(sub: str, org_id: int) -> bool:
        # Le platform admin est toujours org_admin (il provisionne tout).
        if _is_platform_admin(sub):
            return True
        return org_store.get_org_role(org_id, sub) == "org_admin"

    def _is_org_member(sub: str, org_id: int) -> bool:
        if _is_platform_admin(sub):
            return True
        return org_store.get_org_role(org_id, sub) is not None

    def _path_org_id(request: Request) -> int | None:
        try:
            return int(request.path_params["id"])
        except (ValueError, KeyError):
            return None

    def _resolve_target(request: Request, target: str) -> tuple[str | None, JSONResponse | None]:
        """Email (d'un user déjà connecté) ou sub direct → sub."""
        target = (target or "").strip()
        if not target:
            return None, json_error(request, 400, "missing_target")
        if "@" in target:
            u = db.get_user_by_email(target)
            if not u:
                return None, json_error(request, 404, "unknown_target_email")
            return u["sub"], None
        return target, None

    async def _body(request: Request) -> dict | None:
        try:
            body = await request.json()
        except Exception:
            return None
        return body if isinstance(body, dict) else None

    # === self-service ========================================================

    async def me_orgs(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        orgs = []
        for o in org_store.list_orgs_for_user(sub):
            orgs.append({
                "id": o["org_id"],
                "name": o["name"],
                "member_count": len(org_store.list_org_members(o["org_id"])),
                "my_role": o["org_role"],
            })
        return json_response(request, {"orgs": orgs})

    async def org_get(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        org_id = _path_org_id(request)
        if org_id is None:
            return json_error(request, 400, "invalid_id")
        if not _is_org_member(sub, org_id):
            return json_error(request, 403, "forbidden")
        org = org_store.get_org(org_id)
        if not org:
            return json_error(request, 404, "unknown_org")
        my_role = org_store.get_org_role(org_id, sub)
        # with_entitlements=True : les namespaces débloqués pour l'org sont une
        # info légitime pour un membre (carte « entitlements » du dashboard) —
        # rien de sensible (juste namespace + date), gating membre déjà passé.
        return json_response(request, _org_detail(org, with_entitlements=True, my_role=my_role))

    async def org_member_add(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        org_id = _path_org_id(request)
        if org_id is None:
            return json_error(request, 400, "invalid_id")
        if not _is_org_admin(sub, org_id):
            return json_error(request, 403, "forbidden")
        return await _do_member_add(request, org_id)

    async def org_member_role(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        org_id = _path_org_id(request)
        if org_id is None:
            return json_error(request, 400, "invalid_id")
        if not _is_org_admin(sub, org_id):
            return json_error(request, 403, "forbidden")
        return await _do_member_role(request, org_id)

    async def org_member_remove(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        org_id = _path_org_id(request)
        if org_id is None:
            return json_error(request, 400, "invalid_id")
        if not _is_org_admin(sub, org_id):
            return json_error(request, 403, "forbidden")
        return await _do_member_remove(request, org_id)

    async def org_secret_put(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        org_id = _path_org_id(request)
        if org_id is None:
            return json_error(request, 400, "invalid_id")
        if not _is_org_admin(sub, org_id):
            return json_error(request, 403, "forbidden")
        return await _do_secret_put(request, org_id, set_by=sub)

    async def org_secret_delete(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        org_id = _path_org_id(request)
        if org_id is None:
            return json_error(request, 400, "invalid_id")
        if not _is_org_admin(sub, org_id):
            return json_error(request, 403, "forbidden")
        return _do_secret_delete(request, org_id)

    # === platform admin ======================================================

    async def admin_orgs_list(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        if not _is_platform_admin(sub):
            return json_error(request, 403, "forbidden")
        orgs = [
            {"id": o["id"], "name": o["name"],
             "member_count": len(org_store.list_org_members(o["id"]))}
            for o in org_store.list_all_orgs()
        ]
        return json_response(request, {"orgs": orgs})

    async def admin_org_create(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        if not _is_platform_admin(sub):
            return json_error(request, 403, "forbidden")
        body = await _body(request)
        if body is None:
            return json_error(request, 400, "invalid_body")
        name = (body.get("name") or "").strip()
        if not name:
            return json_error(request, 400, "missing_name")
        org_id = org_store.create_org(name, created_by=sub)
        return json_response(request, {"id": org_id})

    async def admin_org_get(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        if not _is_platform_admin(sub):
            return json_error(request, 403, "forbidden")
        org_id = _path_org_id(request)
        if org_id is None:
            return json_error(request, 400, "invalid_id")
        org = org_store.get_org(org_id)
        if not org:
            return json_error(request, 404, "unknown_org")
        return json_response(request, _org_detail(org, with_entitlements=True))

    async def admin_member_add(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        if not _is_platform_admin(sub):
            return json_error(request, 403, "forbidden")
        org_id = _path_org_id(request)
        if org_id is None:
            return json_error(request, 400, "invalid_id")
        return await _do_member_add(request, org_id)

    async def admin_member_role(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        if not _is_platform_admin(sub):
            return json_error(request, 403, "forbidden")
        org_id = _path_org_id(request)
        if org_id is None:
            return json_error(request, 400, "invalid_id")
        return await _do_member_role(request, org_id)

    async def admin_member_remove(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        if not _is_platform_admin(sub):
            return json_error(request, 403, "forbidden")
        org_id = _path_org_id(request)
        if org_id is None:
            return json_error(request, 400, "invalid_id")
        return await _do_member_remove(request, org_id)

    async def admin_secret_put(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        if not _is_platform_admin(sub):
            return json_error(request, 403, "forbidden")
        org_id = _path_org_id(request)
        if org_id is None:
            return json_error(request, 400, "invalid_id")
        return await _do_secret_put(request, org_id, set_by=sub)

    async def admin_secret_delete(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        if not _is_platform_admin(sub):
            return json_error(request, 403, "forbidden")
        org_id = _path_org_id(request)
        if org_id is None:
            return json_error(request, 400, "invalid_id")
        return _do_secret_delete(request, org_id)

    async def admin_entitlement_grant(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        if not _is_platform_admin(sub):
            return json_error(request, 403, "forbidden")
        org_id = _path_org_id(request)
        if org_id is None:
            return json_error(request, 400, "invalid_id")
        namespace = request.path_params["namespace"]
        if namespace not in ADMIN_GRANT_ONLY_NAMESPACES:
            return json_error(request, 400, "namespace_not_controlled")
        if not org_store.get_org(org_id):
            return json_error(request, 404, "unknown_org")
        org_store.grant_org_entitlement(org_id, namespace, granted_by=sub)
        return json_response(request, {"ok": True, "org_id": org_id, "namespace": namespace})

    async def admin_entitlement_revoke(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        if not _is_platform_admin(sub):
            return json_error(request, 403, "forbidden")
        org_id = _path_org_id(request)
        if org_id is None:
            return json_error(request, 400, "invalid_id")
        namespace = request.path_params["namespace"]
        existed = org_store.revoke_org_entitlement(org_id, namespace)
        return json_response(request, {"ok": True, "org_id": org_id,
                                       "namespace": namespace, "existed": existed})

    async def admin_namespace_grants_list(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        if not _is_platform_admin(sub):
            return json_error(request, 403, "forbidden")
        grants = []
        for g in db.list_namespace_grants():
            u = db.get_user(g["sub"]) or {}
            grants.append({
                "sub": g["sub"],
                "email": u.get("email"),
                "name": u.get("name"),
                "namespace": g["namespace"],
                "granted_at": g["granted_at"],
            })
        return json_response(request, {"grants": grants})

    async def admin_namespace_grant(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        if not _is_platform_admin(sub):
            return json_error(request, 403, "forbidden")
        target_sub = request.path_params["sub"]
        namespace = request.path_params["namespace"]
        if namespace not in ADMIN_GRANT_ONLY_NAMESPACES:
            return json_error(request, 400, "namespace_not_controlled")
        if not db.get_user(target_sub):
            return json_error(request, 404, "unknown_user")
        db.grant_namespace(target_sub, namespace, granted_by=sub)
        return json_response(request, {"ok": True, "sub": target_sub, "namespace": namespace})

    async def admin_namespace_revoke(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        if not _is_platform_admin(sub):
            return json_error(request, 403, "forbidden")
        target_sub = request.path_params["sub"]
        namespace = request.path_params["namespace"]
        existed = db.revoke_namespace(target_sub, namespace)
        return json_response(request, {"ok": True, "sub": target_sub,
                                       "namespace": namespace, "existed": existed})

    # --- corps partagés (self-service ↔ admin, gating déjà appliqué) ----------

    async def _do_member_add(request: Request, org_id: int) -> JSONResponse:
        if not org_store.get_org(org_id):
            return json_error(request, 404, "unknown_org")
        body = await _body(request)
        if body is None:
            return json_error(request, 400, "invalid_body")
        role = (body.get("role") or "org_member").strip()
        if role not in org_store.ORG_ROLES:
            return json_error(request, 400, "invalid_role")
        target_sub, terr = _resolve_target(request, body.get("target") or "")
        if terr:
            return terr
        org_store.add_org_member(org_id, target_sub, role)
        return json_response(request, {"ok": True, "org_id": org_id,
                                       "sub": target_sub, "role": role})

    async def _do_member_role(request: Request, org_id: int) -> JSONResponse:
        if not org_store.get_org(org_id):
            return json_error(request, 404, "unknown_org")
        target_sub = request.path_params["sub"]
        body = await _body(request)
        if body is None:
            return json_error(request, 400, "invalid_body")
        role = (body.get("role") or "").strip()
        if role not in org_store.ORG_ROLES:
            return json_error(request, 400, "invalid_role")
        if org_store.get_org_role(org_id, target_sub) is None:
            return json_error(request, 404, "not_a_member")
        org_store.add_org_member(org_id, target_sub, role)
        return json_response(request, {"ok": True, "org_id": org_id,
                                       "sub": target_sub, "role": role})

    async def _do_member_remove(request: Request, org_id: int) -> JSONResponse:
        target_sub = request.path_params["sub"]
        removed = org_store.remove_org_member(org_id, target_sub)
        if not removed:
            return json_error(request, 404, "not_a_member")
        return json_response(request, {"ok": True, "org_id": org_id,
                                       "sub": target_sub, "removed": True})

    async def _do_secret_put(request: Request, org_id: int, *, set_by: str) -> JSONResponse:
        if not org_store.get_org(org_id):
            return json_error(request, 404, "unknown_org")
        provider = request.path_params["provider"]
        body = await _body(request)
        if body is None:
            return json_error(request, 400, "invalid_body")
        api_key = (body.get("api_key") or "").strip()
        if not api_key:
            return json_error(request, 400, "empty_api_key")
        base_url = (body.get("base_url") or "").strip() or None
        meta, code = connectors.org_secret_meta(provider, base_url)
        if code:
            return json_error(request, 400, code)
        org_store.set_org_secret(org_id, provider, api_key, set_by=set_by, meta=meta)
        return json_response(request, {"ok": True, "org_id": org_id, "provider": provider})

    def _do_secret_delete(request: Request, org_id: int) -> JSONResponse:
        provider = request.path_params["provider"]
        deleted = org_store.delete_org_secret(org_id, provider)
        return json_response(request, {"ok": True, "org_id": org_id,
                                       "provider": provider, "deleted": deleted})

    # --- table de routage -----------------------------------------------------

    return [
        # self-service
        Route("/api/me/orgs", me_orgs, methods=["GET"]),
        Route("/api/me/orgs", options_handler, methods=["OPTIONS"]),
        Route("/api/orgs/{id}", org_get, methods=["GET"]),
        Route("/api/orgs/{id}", options_handler, methods=["OPTIONS"]),
        Route("/api/orgs/{id}/members", org_member_add, methods=["POST"]),
        Route("/api/orgs/{id}/members", options_handler, methods=["OPTIONS"]),
        Route("/api/orgs/{id}/members/{sub}", org_member_role, methods=["POST"]),
        Route("/api/orgs/{id}/members/{sub}", org_member_remove, methods=["DELETE"]),
        Route("/api/orgs/{id}/members/{sub}", options_handler, methods=["OPTIONS"]),
        Route("/api/orgs/{id}/secrets/{provider}", org_secret_put, methods=["PUT"]),
        Route("/api/orgs/{id}/secrets/{provider}", org_secret_delete, methods=["DELETE"]),
        Route("/api/orgs/{id}/secrets/{provider}", options_handler, methods=["OPTIONS"]),
        # platform admin
        Route("/api/admin/orgs", admin_orgs_list, methods=["GET"]),
        Route("/api/admin/orgs", admin_org_create, methods=["POST"]),
        Route("/api/admin/orgs", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/orgs/{id}", admin_org_get, methods=["GET"]),
        Route("/api/admin/orgs/{id}", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/orgs/{id}/members", admin_member_add, methods=["POST"]),
        Route("/api/admin/orgs/{id}/members", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/orgs/{id}/members/{sub}", admin_member_role, methods=["POST"]),
        Route("/api/admin/orgs/{id}/members/{sub}", admin_member_remove, methods=["DELETE"]),
        Route("/api/admin/orgs/{id}/members/{sub}", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/orgs/{id}/secrets/{provider}", admin_secret_put, methods=["PUT"]),
        Route("/api/admin/orgs/{id}/secrets/{provider}", admin_secret_delete, methods=["DELETE"]),
        Route("/api/admin/orgs/{id}/secrets/{provider}", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/orgs/{id}/entitlements/{namespace}", admin_entitlement_grant, methods=["POST"]),
        Route("/api/admin/orgs/{id}/entitlements/{namespace}", admin_entitlement_revoke, methods=["DELETE"]),
        Route("/api/admin/orgs/{id}/entitlements/{namespace}", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/namespace-grants", admin_namespace_grants_list, methods=["GET"]),
        Route("/api/admin/namespace-grants", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/users/{sub}/namespace-grants/{namespace}", admin_namespace_grant, methods=["POST"]),
        Route("/api/admin/users/{sub}/namespace-grants/{namespace}", admin_namespace_revoke, methods=["DELETE"]),
        Route("/api/admin/users/{sub}/namespace-grants/{namespace}", options_handler, methods=["OPTIONS"]),
    ]
