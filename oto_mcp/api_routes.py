"""REST API consommée par le frontend oto.ninja (page de gestion de compte).

Endpoints (ce fichier — gestion compte, LinkedIn, Crunchbase, providers,
tools, admin, WhatsApp) :
- `GET    /api/me`                            → infos user + rôle + statut keys
- `GET    /api/settings/linkedin`             → renvoie cookie + UA + set_at (propriétaire only)
- `POST   /api/settings/linkedin`             → enregistre cookie li_at + UA
- `DELETE /api/settings/linkedin`             → efface
- `GET    /api/settings/crunchbase`           → renvoie cookies + UA + set_at
- `POST   /api/settings/crunchbase`           → cookies + UA
- `DELETE /api/settings/crunchbase`
- `GET    /api/settings/api-keys/{provider}`  → état/clé (tout connecteur byo_user à secret simple)
- `POST   /api/settings/api-keys/{provider}`  → pose le credential : `api_key`→`{key}` ; `basic_auth`→`{email,password}`
- `DELETE /api/settings/api-keys/{provider}`  → efface
- `GET    /api/me/tools` + `POST/DELETE /api/me/tools/{name}` → toggle tools per-user
- `GET    /api/admin/*`                       → admin (users, platform-keys, grants, tokens)
- `GET    /api/whatsapp/*`                    → WhatsApp pairing

Endpoints datastore / Google OAuth / API tokens : voir `api_routes_datastore.py`.
Endpoints SIRENE stock : voir `api_routes_sirene.py`.
Endpoints organisation (`/api/me/orgs`, `/api/orgs/*`, `/api/admin/orgs/*`,
`/api/admin/namespace-grants*`) : voir `api_routes_orgs.py` — projection REST du
palier org (mêmes fonctions de service que les meta-tools MCP `oto_admin_*org*`).

Auth : Bearer JWT Logto **ou** API token long-lived (préfixe `oto_`), vérifié
via `_authenticate`. Le frontend obtient le token Logto via `@logto/vue`. La
CLI utilise un API token issu sur `/account` (stocké en SOPS sous `OTO_API_KEY`).

CORS : limité aux origines oto.ninja (+ localhost en dev).
"""
from __future__ import annotations

import os
from typing import Iterable

import asyncio
import json

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from . import access, api_routes_connectors, api_routes_datastore, api_routes_memento, api_routes_orgs, api_routes_scout, api_routes_sirene, connector_activation, connectors, db, linkedin_pairing, org_store, pairing
from .capabilities import _rest_adapter as _cap_rest_adapter
from .capabilities import registry as _cap_registry
from .tool_visibility import is_default_hidden, is_entitled, is_grant_only, namespace_of


def _allowed_origins() -> list[str]:
    raw = os.environ.get("OTO_MCP_CORS_ORIGINS")
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return [
        "https://oto.ninja",
        "https://www.oto.ninja",
        "https://app.oto.ninja",
        "http://localhost:5173",
        "http://localhost:4173",
        "http://localhost:5182",
        "http://localhost:5184",
        "http://localhost:5192",            # oto-dashboard dev (ADR 0007)
        "https://dashboard.otoninja.dev",   # oto-dashboard via Caddy local
        "https://dashboard.oto.ninja",      # oto-dashboard prod
    ]


def _cors_headers(origin: str | None) -> dict[str, str]:
    if origin and origin in _allowed_origins():
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }
    return {}


async def _authenticate(
    request: Request,
    verifier: JWTVerifier,
    *,
    allow_query_token: bool = False,
) -> tuple[str | None, JSONResponse | None]:
    auth = request.headers.get("authorization", "")
    token: str | None = None
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    elif allow_query_token:
        # Fallback pour SSE via EventSource (qui n'autorise pas les headers).
        token = request.query_params.get("token")
    if not token:
        return None, _json_error(request, 401, "missing_bearer")

    # API token long-lived (CLI) : préfixe `oto_` → lookup hash en DB.
    # Pas de upsert_user ici : la FK CASCADE garantit que si la row user a
    # été supprimée, le token a été supprimé avec.
    if token.startswith("oto_"):
        sub = db.verify_api_token(token)
        if not sub:
            return None, _json_error(request, 401, "invalid_api_token")
        return sub, None

    # Sinon, JWT Logto.
    access_token = await verifier.verify_token(token)
    if not access_token or not getattr(access_token, "claims", None):
        return None, _json_error(request, 401, "invalid_token")
    sub = access_token.claims.get("sub")
    if not sub:
        return None, _json_error(request, 401, "missing_sub")
    db.upsert_user(sub, email=access_token.claims.get("email"), name=access_token.claims.get("name"))
    return sub, None


def _json_error(request: Request, status: int, code: str) -> JSONResponse:
    return JSONResponse(
        {"error": code},
        status_code=status,
        headers=_cors_headers(request.headers.get("origin")),
    )


def _json(request: Request, payload: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(
        payload, status_code=status, headers=_cors_headers(request.headers.get("origin"))
    )


def make_routes(verifier: JWTVerifier, mcp_instance=None) -> Iterable:
    from starlette.routing import Route

    async def options_handler(request: Request) -> Response:
        return Response(status_code=204, headers=_cors_headers(request.headers.get("origin")))

    async def mcp_catalog(request: Request) -> JSONResponse:
        """Liste publique des tools MCP exposés — alimente l'autodoc oto.ninja.

        Pas d'auth : la doc des tools (nom, description, schémas) est de toute
        façon découvrable via tools/list du protocole MCP. CORS large pour
        permettre fetch côté oto.ninja.
        """
        if mcp_instance is None:
            return _json(request, {"tools": []})
        try:
            tools = await mcp_instance.list_tools(run_middleware=False)
        except Exception as e:
            return _json_error(request, 500, f"list_tools_failed:{e}")
        payload = []
        for t in tools:
            # Doctrine deny-by-default : les namespaces grant-only (connecteurs
            # client-sensibles type bridge, ADR 0003) n'apparaissent JAMAIS dans
            # l'autodoc publique — elle alimente les pages marketing oto.ninja.
            if is_grant_only(t.name):
                continue
            # Tool object exposes name, description, parameters (input schema),
            # output_schema. Some attributes may be None depending on the type.
            payload.append({
                "name": t.name,
                "description": (t.description or "").strip(),
                "input_schema": getattr(t, "parameters", None),
                "output_schema": getattr(t, "output_schema", None),
            })
        return _json(request, {"tools": payload, "count": len(payload)})

    async def connectors_catalog(request: Request) -> JSONResponse:
        """Catalogue des connecteurs (registre source unique), auth optionnelle.

        Cran d'activation (ADR 0010) filtré EN AMONT de la visibilité : un
        connecteur non activé (master global OFF sans override d'org ON) n'apparaît
        pas dans la vue PRODUIT (anonyme + non-admin). L'**admin voit tout le
        registre** — sa vue de gouvernance sert justement à activer/désactiver.
        Ensuite, visibilité : anonyme → self-serve seuls (les `platform_granted`,
        dont les bridges client-sensibles ADR 0003, sont deny-by-default comme sur
        la face MCP) ; non-admin authentifié → + ceux dont un namespace est entitled
        pour le sub (override d'org appliqué via son org active).
        """
        cat = connectors.public_catalog()
        if not request.headers.get("authorization"):
            exposed = connector_activation.exposed_connectors(None)
            cat = [c for c in cat if c["name"] in exposed]
            cat = [c for c in cat if c["availability"] != "platform_granted"]
            return _json(request, {"connectors": cat})
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if access.get_user_role(sub) != access.ADMIN:
            exposed = connector_activation.exposed_connectors(org_store.get_active_org(sub))
            cat = [c for c in cat if c["name"] in exposed]
            granted = access.granted_namespaces_for(sub)
            cat = [c for c in cat
                   if c["availability"] != "platform_granted"
                   or any(ns in granted for ns in c["namespaces"])]
        return _json(request, {"connectors": cat})

    async def me(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        user = db.get_user(sub) or {}
        status = access.status_for(sub)
        li = db.get_linkedin_status(sub)        # coffre (folding), sans déchiffrer
        cb = db.get_crunchbase_status(sub)
        active_org = org_store.get_active_org(sub)
        active_org_name = None
        org_role = None
        if active_org is not None:
            o = org_store.get_org(active_org)
            active_org_name = o["name"] if o else None
            org_role = org_store.get_org_role(active_org, sub)
        return _json(request, {
            "sub": sub,
            "email": user.get("email"),
            "name": user.get("name"),
            "role": status["role"],
            "active_org": active_org,
            "active_org_name": active_org_name,
            "org_role": org_role,
            "linkedin": {
                "configured": li is not None,
                "set_at": li["set_at"] if li else None,
                "user_agent": li["user_agent"] if li else None,
                "browser_profile": linkedin_pairing.has_profile(sub),
            },
            "crunchbase": {
                "configured": cb is not None,
                "set_at": cb["set_at"] if cb else None,
                "user_agent": cb["user_agent"] if cb else None,
            },
            "providers": status["providers"],
        })

    async def linkedin_save(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return _json_error(request, 400, "invalid_json")
        if not isinstance(body, dict):
            return _json_error(request, 400, "invalid_body")
        cookie = (body.get("cookie") or "").strip()
        user_agent = (body.get("user_agent") or "").strip() or None
        if not cookie:
            return _json_error(request, 400, "empty_cookie")
        db.set_linkedin_cookie(sub, cookie, user_agent=user_agent)
        return _json(request, {"ok": True})

    async def linkedin_clear(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        db.clear_linkedin_cookie(sub)
        return _json(request, {"ok": True})

    async def linkedin_get(request: Request) -> JSONResponse:
        # Renvoie la valeur réelle du cookie LinkedIn au propriétaire authentifié.
        # Permet au CLI (`oto ninja secrets get LINKEDIN_COOKIE`) de récupérer
        # le secret stocké côté DB plutôt que de maintenir une copie SOPS.
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        sess = db.get_linkedin_session(sub)
        if not sess:
            return _json_error(request, 404, "not_configured")
        return _json(request, {
            "cookie": sess["cookie"],
            "user_agent": sess.get("user_agent"),
            "set_at": sess.get("set_at"),
        })

    async def crunchbase_save(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return _json_error(request, 400, "invalid_json")
        if not isinstance(body, dict):
            return _json_error(request, 400, "invalid_body")
        cookies = body.get("cookies")
        if not isinstance(cookies, list) or not cookies:
            return _json_error(request, 400, "cookies_must_be_non_empty_list")
        # Sérialise tel quel — la lib browser attend une liste de dicts
        # avec a minima `name`, `value`, `domain`.
        for c in cookies:
            if not isinstance(c, dict) or not c.get("name") or "value" not in c:
                return _json_error(request, 400, "cookie_missing_name_or_value")
        user_agent = (body.get("user_agent") or "").strip() or None
        db.set_crunchbase_session(sub, json.dumps(cookies), user_agent=user_agent)
        return _json(request, {"ok": True, "count": len(cookies)})

    async def crunchbase_clear(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        db.clear_crunchbase_session(sub)
        return _json(request, {"ok": True})

    async def crunchbase_get(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        sess = db.get_crunchbase_session(sub)
        if not sess:
            return _json_error(request, 404, "not_configured")
        return _json(request, {
            "cookies": sess["cookies"],
            "user_agent": sess.get("user_agent"),
            "set_at": sess.get("set_at"),
        })

    # Saisie de credential per-user, GÉNÉRIQUE (dérivée du registre, pas une liste
    # hardcodée) : tout connecteur `byo_user` dont le secret est un "secret simple"
    # — `api_key` (la clé) ou `basic_auth` (base64("email:password"), ex. planity).
    # cookie/oauth ont des flows dédiés (linkedin / google / memento) → exclus ici.
    _SETTABLE_KINDS = {"api_key", "basic_auth"}

    def _credentialable(provider: str):
        c = connectors.connector_for_provider(provider)
        if c is None or not connectors.is_byo_user(provider) or c.secret_kind not in _SETTABLE_KINDS:
            return None
        return c

    async def api_key_save(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        provider = request.path_params["provider"]
        c = _credentialable(provider)
        if c is None:
            return _json_error(request, 404, "unknown_provider")
        try:
            body = await request.json()
        except Exception:
            return _json_error(request, 400, "invalid_json")
        if not isinstance(body, dict):
            return _json_error(request, 400, "invalid_body")
        if c.secret_kind == "basic_auth":
            import base64
            email = (body.get("email") or "").strip()
            password = body.get("password") or ""
            if not email or not password:
                return _json_error(request, 400, "missing_credentials")
            secret = base64.b64encode(f"{email}:{password}".encode()).decode()
        else:  # api_key
            secret = (body.get("key") or "").strip()
            if not secret:
                return _json_error(request, 400, "empty_key")
        from . import credentials_store
        db.upsert_user(sub)
        credentials_store.set_credential("user", sub, provider, secret, set_by=sub)
        return _json(request, {"ok": True, "provider": provider})

    async def api_key_clear(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        provider = request.path_params["provider"]
        if _credentialable(provider) is None:
            return _json_error(request, 404, "unknown_provider")
        from . import credentials_store
        credentials_store.clear_credential("user", sub, provider)
        return _json(request, {"ok": True, "provider": provider})

    async def api_key_get(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        provider = request.path_params["provider"]
        c = _credentialable(provider)
        if c is None:
            return _json_error(request, 404, "unknown_provider")
        from . import credentials_store
        secret = credentials_store.get_credential("user", sub, provider)
        if not secret:
            return _json_error(request, 404, "not_configured")
        if c.secret_kind == "basic_auth":
            # Ne JAMAIS renvoyer le mot de passe — juste l'état + l'email saisi.
            import base64
            try:
                email = base64.b64decode(secret).decode().split(":", 1)[0]
            except Exception:
                email = None
            return _json(request, {"provider": provider, "configured": True, "email": email})
        return _json(request, {"provider": provider, "key": secret})

    async def admin_users(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if access.get_user_role(sub) != access.ADMIN:
            return _json_error(request, 403, "forbidden")
        # Inclut les grants pour la matrice users × keys côté UI.
        users = db.list_users_with_grants()
        # Surface le rôle "effectif" (qui peut être promu via OTO_MCP_ADMIN_SUB).
        for u in users:
            u["effective_role"] = access.get_user_role(u["sub"])
        return _json(request, {"users": users})

    async def admin_user_detail(request: Request) -> JSONResponse:
        """Fiche complète d'un user (admin) : identité + accès EFFECTIF par
        provider (own key / org / platform+quota / aucun, via status_for) +
        grants de clé plateforme + namespaces débloqués."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if access.get_user_role(sub) != access.ADMIN:
            return _json_error(request, 403, "forbidden")
        target = request.path_params["sub"]
        u = db.get_user(target)
        if not u:
            return _json_error(request, 404, "unknown_user")
        status = access.status_for(target)
        ns = [g for g in db.list_namespace_grants() if g["sub"] == target]
        return _json(request, {
            "sub": target, "email": u.get("email"), "name": u.get("name"),
            "role": status["role"], "active_org": status.get("active_org"),
            "orgs": org_store.list_orgs_for_user(target),
            "providers": status["providers"],
            "grants": db.list_grants_for_user(target),
            "namespace_grants": ns,
        })

    async def admin_platform_keys_list(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if access.get_user_role(sub) != access.ADMIN:
            return _json_error(request, 403, "forbidden")
        # On ne renvoie JAMAIS l'api_key brute — masque + 4 derniers chars.
        keys = []
        for k in db.list_platform_keys():
            ak = k.get("api_key") or ""
            keys.append({
                "id": k["id"],
                "provider": k["provider"],
                "label": k["label"],
                "api_key_tail": ak[-4:] if len(ak) >= 4 else "",
                "created_at": k["created_at"],
            })
        return _json(request, {"platform_keys": keys})

    async def admin_platform_key_create(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if access.get_user_role(sub) != access.ADMIN:
            return _json_error(request, 403, "forbidden")
        try:
            body = await request.json()
        except Exception:
            return _json_error(request, 400, "invalid_json")
        if not isinstance(body, dict):
            return _json_error(request, 400, "invalid_body")
        provider = (body.get("provider") or "").strip()
        label = (body.get("label") or "").strip()
        api_key = (body.get("api_key") or "").strip()
        if provider not in db.KEY_PROVIDERS:
            return _json_error(request, 400, "invalid_provider")
        if not label or not api_key:
            return _json_error(request, 400, "missing_fields")
        try:
            key_id = db.create_platform_key(provider, label, api_key)
        except ValueError:
            return _json_error(request, 409, "duplicate_label")
        return _json(request, {"id": key_id, "provider": provider, "label": label})

    async def admin_platform_key_delete(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if access.get_user_role(sub) != access.ADMIN:
            return _json_error(request, 403, "forbidden")
        try:
            key_id = int(request.path_params["key_id"])
        except (ValueError, KeyError):
            return _json_error(request, 400, "invalid_id")
        if not db.get_platform_key(key_id):
            return _json_error(request, 404, "unknown_key")
        db.delete_platform_key(key_id)
        return _json(request, {"ok": True, "id": key_id})

    async def admin_grant(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if access.get_user_role(sub) != access.ADMIN:
            return _json_error(request, 403, "forbidden")
        target_sub = request.path_params["sub"]
        try:
            key_id = int(request.path_params["key_id"])
        except ValueError:
            return _json_error(request, 400, "invalid_id")
        if not db.get_user(target_sub):
            return _json_error(request, 404, "unknown_user")
        if not db.get_platform_key(key_id):
            return _json_error(request, 404, "unknown_key")
        daily_quota: int | None = None
        try:
            body = await request.json()
            raw = body.get("daily_quota")
            if raw is not None:
                daily_quota = max(1, int(raw))
        except Exception:
            pass
        db.grant_platform_key(target_sub, key_id, granted_by=sub, daily_quota=daily_quota)
        return _json(request, {"ok": True, "sub": target_sub, "platform_key_id": key_id, "daily_quota": daily_quota})

    async def admin_revoke(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if access.get_user_role(sub) != access.ADMIN:
            return _json_error(request, 403, "forbidden")
        target_sub = request.path_params["sub"]
        try:
            key_id = int(request.path_params["key_id"])
        except ValueError:
            return _json_error(request, 400, "invalid_id")
        db.revoke_platform_key(target_sub, key_id)
        return _json(request, {"ok": True, "sub": target_sub, "platform_key_id": key_id})

    async def admin_tokens_list(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if access.get_user_role(sub) != access.ADMIN:
            return _json_error(request, 403, "forbidden")
        target_sub = request.path_params["sub"]
        if not db.get_user(target_sub):
            return _json_error(request, 404, "unknown_user")
        return _json(request, {"tokens": db.list_api_tokens(target_sub)})

    async def admin_tokens_create(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if access.get_user_role(sub) != access.ADMIN:
            return _json_error(request, 403, "forbidden")
        target_sub = request.path_params["sub"]
        if not db.get_user(target_sub):
            return _json_error(request, 404, "unknown_user")
        try:
            body = await request.json()
        except Exception:
            body = {}
        label = (body or {}).get("label") or "cli"
        ttl_raw = (body or {}).get("ttl_days")
        ttl_days = int(ttl_raw) if isinstance(ttl_raw, (int, str)) and str(ttl_raw).isdigit() else None
        token = db.create_api_token(target_sub, label=label.strip()[:32], ttl_days=ttl_days)
        return _json(request, {"token": token, "label": label, "ttl_days": ttl_days})

    async def admin_tokens_delete(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if access.get_user_role(sub) != access.ADMIN:
            return _json_error(request, 403, "forbidden")
        target_sub = request.path_params["sub"]
        try:
            token_id = int(request.path_params["token_id"])
        except ValueError:
            return _json_error(request, 400, "invalid_id")
        ok = db.delete_api_token(target_sub, token_id)
        if not ok:
            return _json_error(request, 404, "unknown_token")
        return _json(request, {"ok": True, "id": token_id})

    async def admin_set_role(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if access.get_user_role(sub) != access.ADMIN:
            return _json_error(request, 403, "forbidden")
        target_sub = request.path_params["sub"]
        try:
            body = await request.json()
        except Exception:
            return _json_error(request, 400, "invalid_json")
        role = (body or {}).get("role")
        if role not in access.ROLES:
            return _json_error(request, 400, "invalid_role")
        if not db.get_user(target_sub):
            return _json_error(request, 404, "unknown_user")
        db.set_user_role(target_sub, role)
        return _json(request, {"ok": True, "sub": target_sub, "role": role})

    async def admin_monitoring_summary(request: Request) -> JSONResponse:
        """Agrégats des appels MCP (total / échecs / par tool / par user / par
        jour) sur une fenêtre `?days=` (défaut 7). Admin only."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if access.get_user_role(sub) != access.ADMIN:
            return _json_error(request, 403, "forbidden")
        try:
            days = int(request.query_params.get("days", "7"))
        except ValueError:
            days = 7
        return _json(request, db.tool_call_stats(since_days=days))

    async def admin_monitoring_calls(request: Request) -> JSONResponse:
        """Derniers appels MCP (journal brut), récent d'abord. Filtres :
        `?limit=` (défaut 200, max 1000), `?sub=`, `?tool=`, `?errors=1`,
        `?days=`. Admin only."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if access.get_user_role(sub) != access.ADMIN:
            return _json_error(request, 403, "forbidden")
        qp = request.query_params
        try:
            limit = int(qp.get("limit", "200"))
        except ValueError:
            limit = 200
        since_days: int | None = None
        if qp.get("days"):
            try:
                since_days = int(qp["days"])
            except ValueError:
                since_days = None
        calls = db.list_tool_calls(
            limit=limit,
            sub=qp.get("sub") or None,
            tool_name=qp.get("tool") or None,
            errors_only=qp.get("errors") in ("1", "true"),
            since_days=since_days,
        )
        return _json(request, {"calls": calls})

    async def my_calls(request: Request) -> JSONResponse:
        """Journal des appels MCP de l'utilisateur courant (sa propre activité).
        Filtres `?limit=`/`?tool=`/`?errors=1`/`?days=`. Toujours scopé au sub
        du token — un user ne voit QUE ses propres appels (≠ /api/admin/monitoring
        qui agrège tout le monde et reste admin-only)."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        qp = request.query_params
        try:
            limit = int(qp.get("limit", "200"))
        except ValueError:
            limit = 200
        since_days: int | None = None
        if qp.get("days"):
            try:
                since_days = int(qp["days"])
            except ValueError:
                since_days = None
        calls = db.list_tool_calls(
            limit=limit,
            sub=sub,
            tool_name=qp.get("tool") or None,
            errors_only=qp.get("errors") in ("1", "true"),
            since_days=since_days,
        )
        return _json(request, {"calls": calls})

    async def my_tools_list(request: Request) -> JSONResponse:
        """Liste tous les tools du serveur avec l'état (enabled/disabled)
        pour l'utilisateur courant.
        """
        sub, err = await _authenticate(request, verifier)
        if err:
            return err

        all_names: set[str] = set()
        if mcp_instance is not None:
            # run_middleware=False : appelé hors session MCP (contexte REST), la
            # chaîne de middleware n'a pas de Context FastMCP et lèverait → on
            # veut la liste statique complète, le filtrage disabled est fait
            # juste après via `disabled`. (cf. _list_all_tool_names)
            tools = await mcp_instance.list_tools(run_middleware=False)
            all_names = {t.name for t in tools}

        disabled = set(db.list_user_disabled_tools(sub))
        # Le middleware retire déjà les disabled de `list_tools` selon le sub
        # courant (celui de la requête REST = même token). On ré-ajoute donc
        # les disabled pour avoir la vue complète.
        all_names |= disabled

        return _json(request, {
            "tools": [
                {"name": n, "enabled": n not in disabled}
                for n in sorted(all_names)
            ],
        })

    async def my_tools_disable(request: Request) -> JSONResponse:
        """Désactive un tool pour l'utilisateur courant (live)."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        name = request.path_params["name"]
        db.add_user_disabled_tool(sub, name)
        db.remove_user_enabled_tool(sub, name)  # lève un éventuel override positif
        return _json(request, {"ok": True, "name": name, "enabled": False})

    async def my_tools_enable(request: Request) -> JSONResponse:
        """Réactive un tool pour l'utilisateur courant (live).

        Refuse l'activation d'un tool d'un namespace gouverné (grant-only) si
        l'user n'y a pas droit — même barrière que le meta-tool MCP
        `oto_enable_tool`, sinon le plafond org serait contournable via /account.
        """
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        name = request.path_params["name"]
        granted = access.granted_namespaces_for(sub)
        is_admin = access.get_user_role(sub) == access.ADMIN
        if is_grant_only(name) and not is_admin and namespace_of(name) not in granted:
            return _json(request, {
                "error": "forbidden",
                "name": name,
                "detail": f"namespace `{namespace_of(name)}` non accordé",
            }, status_code=403)
        db.remove_user_disabled_tool(sub, name)
        # Override positif requis pour rendre visible un masqué-par-défaut, ou un
        # grant-only côté admin — même logique que le meta-tool oto_enable_tool.
        if is_default_hidden(name) or (is_grant_only(name) and is_admin):
            db.add_user_enabled_tool(sub, name)
        return _json(request, {"ok": True, "name": name, "enabled": True})

    # --- presets ------------------------------------------------------------

    _PROTECTED_TOOLS = {"oto_enable_tool", "oto_list_my_tools", "oto_apply_preset"}

    async def _list_all_tool_names() -> set[str]:
        if mcp_instance is None:
            return set()
        tools = await mcp_instance.list_tools(run_middleware=False)
        return {t.name for t in tools}

    async def my_presets_list(request: Request) -> JSONResponse:
        """Liste les presets sauvés du user."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        presets = db.list_user_presets(sub)
        return _json(request, {
            "presets": [
                {
                    "name": p["name"],
                    "tool_count": len(p["enabled_tools"]),
                    "updated_at": str(p["updated_at"]) if p["updated_at"] else None,
                }
                for p in presets
            ],
        })

    async def my_preset_get(request: Request) -> JSONResponse:
        """Récupère le détail d'un preset (liste exhaustive de enabled_tools)."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        name = request.path_params["name"]
        preset = db.get_user_preset(sub, name)
        if not preset:
            return _json(request, {"error": "not_found", "name": name}, status_code=404)
        return _json(request, {
            "name": preset["name"],
            "enabled_tools": preset["enabled_tools"],
            "updated_at": str(preset["updated_at"]) if preset["updated_at"] else None,
        })

    async def my_preset_save(request: Request) -> JSONResponse:
        """Snapshot l'état courant sous ce nom, OU sauve une liste explicite
        si le body contient `{"enabled_tools": [...]}`. Utile pour
        provisionner un preset sans altérer l'état courant.
        """
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        name = request.path_params["name"]
        all_names = await _list_all_tool_names()

        explicit: list[str] | None = None
        # Body optionnel — un POST sans body garde le comportement snapshot
        try:
            body = await request.json()
            if isinstance(body, dict) and isinstance(body.get("enabled_tools"), list):
                explicit = [str(t) for t in body["enabled_tools"]]
        except Exception:
            pass

        if explicit is not None:
            unknown = sorted(set(explicit) - all_names)
            if unknown:
                return _json(request, {
                    "error": "unknown_tools",
                    "unknown": unknown,
                }, status_code=400)
            enabled = sorted(set(explicit))
        else:
            disabled = set(db.list_user_disabled_tools(sub))
            enabled = sorted(all_names - disabled)

        db.save_user_preset(sub, name, enabled)
        return _json(request, {"ok": True, "name": name, "enabled_count": len(enabled)})

    async def my_preset_apply(request: Request) -> JSONResponse:
        """Bascule user_disabled_tools selon le preset. Ne notifie pas les
        sessions MCP en cours — elles verront le nouvel état au prochain
        handshake (le hook on_initialize relit la DB).
        """
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        name = request.path_params["name"]
        preset = db.get_user_preset(sub, name)
        if not preset:
            return _json(request, {"error": "not_found", "name": name}, status_code=404)
        all_names = await _list_all_tool_names()
        granted = access.granted_namespaces_for(sub)
        is_admin = access.get_user_role(sub) == access.ADMIN
        requested = (set(preset["enabled_tools"]) | _PROTECTED_TOOLS) & all_names
        # Un preset ne peut pas révéler un grant-only non autorisé (anti-escalade,
        # miroir de oto_apply_preset côté MCP).
        enabled = {n for n in requested if is_entitled(n, granted, is_admin)}
        disabled = sorted(all_names - enabled)
        db.replace_user_disabled_tools(sub, disabled)
        return _json(request, {
            "ok": True,
            "applied": name,
            "enabled_count": len(enabled),
            "disabled_count": len(disabled),
        })

    async def my_preset_delete(request: Request) -> JSONResponse:
        """Supprime un preset par nom."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        name = request.path_params["name"]
        deleted = db.delete_user_preset(sub, name)
        if not deleted:
            return _json(request, {"error": "not_found", "name": name}, status_code=404)
        return _json(request, {"ok": True, "name": name, "deleted": True})

    # ── Doctrine & instructions de l'org active ─────────────────────
    # Surface self-service scopée à l'ORG ACTIVE du caller (résolue depuis le
    # token, comme les org_secrets). Lecture = tout membre ; écriture = org_admin
    # de l'org (ou platform admin). Le store gère le versioning + l'historique.

    def _active_org_edit(sub: str) -> tuple[int | None, str | None, bool]:
        """(org_id, org_role, can_edit) pour l'org active du caller."""
        org_id = org_store.get_active_org(sub)
        if org_id is None:
            return None, None, False
        role = org_store.get_org_role(org_id, sub)
        can_edit = role == "org_admin" or access.get_user_role(sub) == access.ADMIN
        return org_id, role, can_edit

    async def my_instructions_list(request: Request) -> JSONResponse:
        """Doctrine de base (meta) + index des instructions nommées de l'org active."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        org_id, role, can_edit = _active_org_edit(sub)
        if org_id is None:
            return _json(request, {
                "org_id": None, "org_name": None, "can_edit": False,
                "doctrine": {"exists": False, "version": 0, "updated_at": None},
                "instructions": [],
            })
        o = org_store.get_org(org_id)
        base = org_store.get_instruction(org_id, org_store.BASE_SLUG)
        return _json(request, {
            "org_id": org_id,
            "org_name": o["name"] if o else None,
            "can_edit": can_edit,
            "doctrine": {
                "exists": base is not None,
                "version": base["version"] if base else 0,
                "updated_at": base["updated_at"] if base else None,
            },
            "instructions": org_store.list_instructions(org_id),
        })

    async def my_instruction_get(request: Request) -> JSONResponse:
        """Corps complet d'une instruction (slug `claude_md` = doctrine de base).
        `?version=` optionnel pour relire une version archivée."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        org_id, role, _ = _active_org_edit(sub)
        if org_id is None:
            return _json_error(request, 404, "no_active_org")
        if role is None and access.get_user_role(sub) != access.ADMIN:
            return _json_error(request, 403, "forbidden")
        slug = request.path_params["slug"]
        version = request.query_params.get("version")
        try:
            version = int(version) if version else None
        except (TypeError, ValueError):
            return _json_error(request, 400, "invalid_version")
        instr = org_store.get_instruction(org_id, slug, version=version)
        if not instr:
            return _json(request, {"error": "not_found", "slug": slug}, status_code=404)
        return _json(request, {
            "slug": instr["slug"],
            "title": instr["title"],
            "description": instr["description"],
            "version": instr["version"],
            "body_md": instr["body_md"],
            "set_by": instr.get("set_by"),
            "created_at": instr.get("created_at"),
            "updated_at": instr.get("updated_at"),
        })

    async def my_instruction_save(request: Request) -> JSONResponse:
        """Crée/met à jour une instruction (org_admin). Incrémente la version."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        org_id, _, can_edit = _active_org_edit(sub)
        if org_id is None:
            return _json_error(request, 404, "no_active_org")
        if not can_edit:
            return _json_error(request, 403, "forbidden")
        slug = org_store.normalize_slug(request.path_params["slug"])
        if not slug:
            return _json_error(request, 400, "invalid_slug")
        try:
            body = await request.json()
        except Exception:
            return _json_error(request, 400, "invalid_json")
        if not isinstance(body, dict):
            return _json_error(request, 400, "invalid_body")
        body_md = (body.get("body_md") or "").strip()
        if not body_md:
            return _json_error(request, 400, "body_md_required")
        # Injecté dans get_claude_md() à chaque session MCP → caper pour ne pas
        # saturer le contexte du modèle.
        if len(body_md.encode()) > 64 * 1024:
            return _json_error(request, 400, "body_too_large")
        title = body.get("title")
        description = body.get("description")
        version = org_store.set_instruction(
            org_id, slug, body_md,
            title=title if title is None else str(title),
            description=description if description is None else str(description),
            set_by=sub,
        )
        return _json(request, {"ok": True, "slug": slug, "version": version})

    async def my_instruction_delete(request: Request) -> JSONResponse:
        """Supprime une instruction et son historique (org_admin)."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        org_id, _, can_edit = _active_org_edit(sub)
        if org_id is None:
            return _json_error(request, 404, "no_active_org")
        if not can_edit:
            return _json_error(request, 403, "forbidden")
        slug = org_store.normalize_slug(request.path_params["slug"])
        deleted = org_store.delete_instruction(org_id, slug)
        if not deleted:
            return _json_error(request, 404, "not_found")
        return _json(request, {"ok": True, "slug": slug, "deleted": True})

    async def my_instruction_versions(request: Request) -> JSONResponse:
        """Historique d'une instruction (metadata par version, récent d'abord)."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        org_id, role, _ = _active_org_edit(sub)
        if org_id is None:
            return _json_error(request, 404, "no_active_org")
        if role is None and access.get_user_role(sub) != access.ADMIN:
            return _json_error(request, 403, "forbidden")
        slug = org_store.normalize_slug(request.path_params["slug"])
        return _json(request, {
            "slug": slug,
            "versions": org_store.list_instruction_versions(org_id, slug),
        })

    async def my_instruction_revert(request: Request) -> JSONResponse:
        """Restaure une version archivée comme NOUVELLE version (org_admin)."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        org_id, _, can_edit = _active_org_edit(sub)
        if org_id is None:
            return _json_error(request, 404, "no_active_org")
        if not can_edit:
            return _json_error(request, 403, "forbidden")
        slug = org_store.normalize_slug(request.path_params["slug"])
        try:
            body = await request.json()
        except Exception:
            return _json_error(request, 400, "invalid_json")
        try:
            target = int((body or {}).get("version"))
        except (TypeError, ValueError):
            return _json_error(request, 400, "invalid_version")
        old = org_store.get_instruction(org_id, slug, version=target)
        if not old:
            return _json(request, {"error": "not_found", "slug": slug, "version": target}, status_code=404)
        version = org_store.set_instruction(
            org_id, slug, old["body_md"], title=old["title"],
            description=old["description"], set_by=sub)
        return _json(request, {"ok": True, "slug": slug, "version": version, "reverted_from": target})

    # ── LinkedIn browser-session pairing (VNC) ──────────────────────

    async def linkedin_browser_status(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        active = linkedin_pairing.get_active_for_sub(sub)
        return _json(request, {
            "has_profile": linkedin_pairing.has_profile(sub),
            "active_pairing": {
                "session_id": active.session_id,
                "status": active.status,
            } if active else None,
        })

    async def linkedin_browser_start(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        loop = asyncio.get_running_loop()
        session = linkedin_pairing.start(sub, loop)
        return _json(request, {"session_id": session.session_id, "status": session.status})

    async def linkedin_browser_cancel(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        active = linkedin_pairing.get_active_for_sub(sub)
        if not active:
            return _json_error(request, 404, "no_active_pairing")
        active.cancel()
        return _json(request, {"ok": True})

    async def linkedin_browser_stream(request: Request) -> Response:
        sub, err = await _authenticate(request, verifier, allow_query_token=True)
        if err:
            return err
        session_id = request.query_params.get("session_id", "")
        session = linkedin_pairing.get_session(session_id)
        if not session or session.sub != sub:
            return _json_error(request, 404, "unknown_session")

        async def event_stream():
            yield f": ok\ndata: {json.dumps({'type': 'connected', 'status': session.status})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(session.queue.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event is None:
                    break
                yield f"data: {json.dumps(event)}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                **_cors_headers(request.headers.get("origin")),
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ── WhatsApp pairing ──────────────────────────────────────────

    async def whatsapp_status(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        active = pairing.get_active_for_sub(sub)
        return _json(request, {
            "paired": pairing.is_paired(sub),
            "active_pairing": {
                "session_id": active.session_id,
                "status": active.status,
            } if active else None,
        })

    async def whatsapp_pair_start(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        loop = asyncio.get_running_loop()
        session = pairing.start(sub, loop)
        return _json(request, {"session_id": session.session_id, "status": session.status})

    async def whatsapp_pair_cancel(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        active = pairing.get_active_for_sub(sub)
        if not active:
            return _json_error(request, 404, "no_active_pairing")
        active.cancel()
        return _json(request, {"ok": True})

    async def whatsapp_pair_stream(request: Request) -> Response:
        sub, err = await _authenticate(request, verifier, allow_query_token=True)
        if err:
            return err
        session_id = request.query_params.get("session_id", "")
        session = pairing.get_session(session_id)
        if not session or session.sub != sub:
            return _json_error(request, 404, "unknown_session")

        async def event_stream():
            # Initial hello so the client knows the stream is live.
            yield f": ok\ndata: {json.dumps({'type': 'connected', 'status': session.status})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(session.queue.get(), timeout=20)
                except asyncio.TimeoutError:
                    # Keepalive comment.
                    yield ": keepalive\n\n"
                    continue
                if event is None:
                    break
                yield f"data: {json.dumps(event)}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                **_cors_headers(request.headers.get("origin")),
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    datastore_routes = api_routes_datastore.make_routes(
        verifier=verifier,
        authenticate=_authenticate,
        json_response=_json,
        json_error=_json_error,
        cors_headers=_cors_headers,
        options_handler=options_handler,
    )

    sirene_routes = api_routes_sirene.make_routes(
        verifier=verifier,
        authenticate=_authenticate,
        json_response=_json,
        json_error=_json_error,
        options_handler=options_handler,
    )

    orgs_routes = api_routes_orgs.make_routes(
        verifier=verifier,
        authenticate=_authenticate,
        json_response=_json,
        json_error=_json_error,
        options_handler=options_handler,
    )

    memento_routes = api_routes_memento.make_routes(
        verifier=verifier,
        authenticate=_authenticate,
        json_response=_json,
        json_error=_json_error,
        options_handler=options_handler,
    )

    scout_routes = api_routes_scout.make_routes(
        verifier=verifier,
        authenticate=_authenticate,
        json_response=_json,
        json_error=_json_error,
        options_handler=options_handler,
    )

    # Couche capacité (ADR 0009) : routes REST dérivées du registre (no-op tant
    # qu'il est vide — canari). Même séquence autz→validation→handler que MCP.
    capability_routes = _cap_rest_adapter.make_routes(
        verifier, _authenticate, _json, _json_error, options_handler,
        _cap_registry.CAPABILITIES,
    )

    # Cran d'activation des connecteurs (ADR 0010, B4) — admin only.
    connectors_routes = api_routes_connectors.make_routes(
        verifier, _authenticate, _json, _json_error, options_handler,
    )

    return [
        Route("/api/mcp/catalog", mcp_catalog, methods=["GET"]),
        Route("/api/mcp/catalog", options_handler, methods=["OPTIONS"]),
        Route("/api/connectors", connectors_catalog, methods=["GET"]),
        Route("/api/connectors", options_handler, methods=["OPTIONS"]),
        Route("/api/me", me, methods=["GET"]),
        Route("/api/me", options_handler, methods=["OPTIONS"]),
        Route("/api/settings/linkedin", linkedin_get, methods=["GET"]),
        Route("/api/settings/linkedin", linkedin_save, methods=["POST"]),
        Route("/api/settings/linkedin", linkedin_clear, methods=["DELETE"]),
        Route("/api/settings/linkedin", options_handler, methods=["OPTIONS"]),
        Route("/api/settings/crunchbase", crunchbase_get, methods=["GET"]),
        Route("/api/settings/crunchbase", crunchbase_save, methods=["POST"]),
        Route("/api/settings/crunchbase", crunchbase_clear, methods=["DELETE"]),
        Route("/api/settings/crunchbase", options_handler, methods=["OPTIONS"]),
        Route("/api/me/calls", my_calls, methods=["GET"]),
        Route("/api/me/calls", options_handler, methods=["OPTIONS"]),
        Route("/api/me/tools", my_tools_list, methods=["GET"]),
        Route("/api/me/tools", options_handler, methods=["OPTIONS"]),
        Route("/api/me/tools/{name}", my_tools_disable, methods=["POST"]),
        Route("/api/me/tools/{name}", my_tools_enable, methods=["DELETE"]),
        Route("/api/me/tools/{name}", options_handler, methods=["OPTIONS"]),
        Route("/api/me/presets", my_presets_list, methods=["GET"]),
        Route("/api/me/presets", options_handler, methods=["OPTIONS"]),
        Route("/api/me/presets/{name}", my_preset_get, methods=["GET"]),
        Route("/api/me/presets/{name}", my_preset_save, methods=["POST"]),
        Route("/api/me/presets/{name}", my_preset_delete, methods=["DELETE"]),
        Route("/api/me/presets/{name}", options_handler, methods=["OPTIONS"]),
        Route("/api/me/presets/{name}/apply", my_preset_apply, methods=["POST"]),
        Route("/api/me/presets/{name}/apply", options_handler, methods=["OPTIONS"]),
        Route("/api/me/instructions", my_instructions_list, methods=["GET"]),
        Route("/api/me/instructions", options_handler, methods=["OPTIONS"]),
        Route("/api/me/instructions/{slug}", my_instruction_get, methods=["GET"]),
        Route("/api/me/instructions/{slug}", my_instruction_save, methods=["PUT"]),
        Route("/api/me/instructions/{slug}", my_instruction_delete, methods=["DELETE"]),
        Route("/api/me/instructions/{slug}", options_handler, methods=["OPTIONS"]),
        Route("/api/me/instructions/{slug}/versions", my_instruction_versions, methods=["GET"]),
        Route("/api/me/instructions/{slug}/versions", options_handler, methods=["OPTIONS"]),
        Route("/api/me/instructions/{slug}/revert", my_instruction_revert, methods=["POST"]),
        Route("/api/me/instructions/{slug}/revert", options_handler, methods=["OPTIONS"]),
        Route("/api/settings/api-keys/{provider}", api_key_get, methods=["GET"]),
        Route("/api/settings/api-keys/{provider}", api_key_save, methods=["POST"]),
        Route("/api/settings/api-keys/{provider}", api_key_clear, methods=["DELETE"]),
        Route("/api/settings/api-keys/{provider}", options_handler, methods=["OPTIONS"]),
        Route("/api/settings/linkedin/browser/status", linkedin_browser_status, methods=["GET"]),
        Route("/api/settings/linkedin/browser/status", options_handler, methods=["OPTIONS"]),
        Route("/api/settings/linkedin/browser/start", linkedin_browser_start, methods=["POST"]),
        Route("/api/settings/linkedin/browser/start", options_handler, methods=["OPTIONS"]),
        Route("/api/settings/linkedin/browser/cancel", linkedin_browser_cancel, methods=["POST"]),
        Route("/api/settings/linkedin/browser/cancel", options_handler, methods=["OPTIONS"]),
        Route("/api/settings/linkedin/browser/stream", linkedin_browser_stream, methods=["GET"]),
        Route("/api/settings/linkedin/browser/stream", options_handler, methods=["OPTIONS"]),
        Route("/api/whatsapp/status", whatsapp_status, methods=["GET"]),
        Route("/api/whatsapp/status", options_handler, methods=["OPTIONS"]),
        Route("/api/whatsapp/pair/start", whatsapp_pair_start, methods=["POST"]),
        Route("/api/whatsapp/pair/start", options_handler, methods=["OPTIONS"]),
        Route("/api/whatsapp/pair/cancel", whatsapp_pair_cancel, methods=["POST"]),
        Route("/api/whatsapp/pair/cancel", options_handler, methods=["OPTIONS"]),
        Route("/api/whatsapp/pair/stream", whatsapp_pair_stream, methods=["GET"]),
        Route("/api/whatsapp/pair/stream", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/users", admin_users, methods=["GET"]),
        Route("/api/admin/users", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/users/{sub}", admin_user_detail, methods=["GET"]),
        Route("/api/admin/users/{sub}", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/users/{sub}/role", admin_set_role, methods=["POST"]),
        Route("/api/admin/users/{sub}/role", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/platform-keys", admin_platform_keys_list, methods=["GET"]),
        Route("/api/admin/platform-keys", admin_platform_key_create, methods=["POST"]),
        Route("/api/admin/platform-keys", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/platform-keys/{key_id}", admin_platform_key_delete, methods=["DELETE"]),
        Route("/api/admin/platform-keys/{key_id}", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/users/{sub}/grants/{key_id}", admin_grant, methods=["POST"]),
        Route("/api/admin/users/{sub}/grants/{key_id}", admin_revoke, methods=["DELETE"]),
        Route("/api/admin/users/{sub}/grants/{key_id}", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/users/{sub}/tokens", admin_tokens_list, methods=["GET"]),
        Route("/api/admin/users/{sub}/tokens", admin_tokens_create, methods=["POST"]),
        Route("/api/admin/users/{sub}/tokens", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/users/{sub}/tokens/{token_id}", admin_tokens_delete, methods=["DELETE"]),
        Route("/api/admin/users/{sub}/tokens/{token_id}", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/monitoring/summary", admin_monitoring_summary, methods=["GET"]),
        Route("/api/admin/monitoring/summary", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/monitoring/calls", admin_monitoring_calls, methods=["GET"]),
        Route("/api/admin/monitoring/calls", options_handler, methods=["OPTIONS"]),
        *datastore_routes,
        *sirene_routes,
        *orgs_routes,
        *memento_routes,
        *scout_routes,
        *capability_routes,
        *connectors_routes,
    ]
