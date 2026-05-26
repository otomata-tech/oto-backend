"""Routes REST datastore + Google OAuth + API tokens.

Extrait de `api_routes.py` pour respecter la limite 500 LOC.

Endpoints exposés :

- `GET    /api/google/oauth/start`              → renvoie {auth_url}
- `GET    /api/google/oauth/callback`           → no auth (Google redirige)
- `GET    /api/google/oauth/status`             → {connected, granted_at, scopes}
- `DELETE /api/google/oauth`                    → révoque

- `GET    /api/me/tokens`                       → liste tokens CLI (sans plaintext)
- `POST   /api/me/tokens`                       → crée un token, renvoie le plaintext (one-shot)
- `DELETE /api/me/tokens/{token_id}`            → révoque

- `GET    /api/datastore/namespaces`            → liste les namespaces user
- `POST   /api/datastore/namespaces`            → crée une namespace
- `DELETE /api/datastore/namespaces/{ns}`       → supprime
- `GET    /api/datastore/namespaces/{ns}/url`   → URL du Google Sheet
- `GET    /api/datastore/namespaces/{ns}/rows`  → liste les rows (filter=k:v, limit=N)
- `POST   /api/datastore/namespaces/{ns}/rows`  → append row
- `GET    /api/datastore/namespaces/{ns}/rows/{row_id}`    → fetch row
- `PATCH  /api/datastore/namespaces/{ns}/rows/{row_id}`    → update row
- `DELETE /api/datastore/namespaces/{ns}/rows/{row_id}`    → delete row

Auth : Bearer JWT Logto **ou** API token long-lived (préfixe `oto_`),
résolu via `_authenticate` (partagé avec `api_routes.py`).
"""
from __future__ import annotations

import os
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import db, google_oauth
from .datastore import (
    GoogleNotConnected,
    NamespaceExists,
    NamespaceNotFound,
    RowNotFound,
    make_store,
)


# Type alias for the auth helper passed in from api_routes.
AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]


def _app_url() -> str:
    return os.environ.get("OTO_APP_URL", "https://app.oto.ninja").rstrip("/")


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    cors_headers: Callable[[str | None], dict[str, str]],
    options_handler: Callable[[Request], Awaitable[Response]],
) -> list[Route]:
    """Construit les routes datastore.

    Les helpers `authenticate`/`json_response`/`json_error`/`cors_headers`/
    `options_handler` sont passés depuis `api_routes.py` pour partager les
    primitives (auth Logto + token, CORS).
    """

    # --- Google OAuth ----------------------------------------------------

    async def google_oauth_start(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        try:
            url = google_oauth.build_auth_url(sub)
        except RuntimeError as e:
            return json_error(request, 500, f"oauth_misconfigured: {e}")
        return json_response(request, {"auth_url": url})

    async def google_oauth_callback(request: Request) -> Response:
        # Pas d'auth Logto — Google redirige depuis le navigateur user.
        # Validation via le `state` HMAC-signé.
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        if not code or not state:
            return json_error(request, 400, "missing_code_or_state")
        sub = google_oauth.verify_state(state)
        if not sub:
            return json_error(request, 400, "invalid_state")
        try:
            tokens = google_oauth.exchange_code(code)
            google_oauth.persist_token(sub, tokens)
        except Exception as e:
            return json_error(request, 502, f"oauth_exchange_failed: {e}")
        return RedirectResponse(url=f"{_app_url()}/?datastore=connected", status_code=302)

    async def google_oauth_status(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        row = db.get_google_oauth(sub)
        return json_response(request, {
            "connected": bool(row),
            "granted_at": row["granted_at"] if row else None,
            "scopes": row["scopes"].split() if row and row.get("scopes") else [],
        })

    async def google_oauth_revoke(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        google_oauth.revoke(sub)
        return json_response(request, {"ok": True})

    # --- API tokens (CLI auth) -------------------------------------------

    async def me_tokens_list(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        return json_response(request, {"tokens": db.list_api_tokens(sub)})

    async def me_tokens_create(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            body = {}
        label = (body.get("label") if isinstance(body, dict) else None) or "cli"
        token = db.create_api_token(sub, label=label.strip()[:32])
        return json_response(request, {"token": token, "label": label}, status=201)

    async def me_tokens_delete(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        try:
            token_id = int(request.path_params["token_id"])
        except (ValueError, KeyError):
            return json_error(request, 400, "invalid_id")
        ok = db.delete_api_token(sub, token_id)
        if not ok:
            return json_error(request, 404, "unknown_token")
        return json_response(request, {"ok": True})

    # --- Datastore -------------------------------------------------------

    def _store(request: Request, sub: str):
        """Renvoie (store, err_response). Err = 412 si pas de grant Google.

        Important : on passe `request` pour récupérer l'origine CORS, sinon
        l'erreur 412 est silencieusement bloquée par le browser.
        """
        try:
            return make_store(sub), None
        except GoogleNotConnected as e:
            return None, JSONResponse(
                {"error": "google_not_connected", "detail": str(e)},
                status_code=412,
                headers=cors_headers(request.headers.get("origin")),
            )

    async def ds_list_ns(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        store, err = _store(request, sub)
        if err:
            return err
        return json_response(request, {"namespaces": store.list_namespaces()})

    async def ds_create_ns(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return json_error(request, 400, "invalid_json")
        namespace = (body or {}).get("namespace", "").strip()
        if not namespace:
            return json_error(request, 400, "missing_namespace")
        store, err = _store(request, sub)
        if err:
            return err
        try:
            return json_response(request, store.create_namespace(namespace), status=201)
        except NamespaceExists:
            return json_error(request, 409, "namespace_exists")

    async def ds_delete_ns(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        namespace = request.path_params["namespace"]
        store, err = _store(request, sub)
        if err:
            return err
        try:
            store.delete_namespace(namespace)
        except NamespaceNotFound:
            return json_error(request, 404, "namespace_not_found")
        return json_response(request, {"ok": True, "namespace": namespace})

    async def ds_append(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return json_error(request, 400, "invalid_json")
        if not isinstance(body, dict):
            return json_error(request, 400, "invalid_body")
        namespace = request.path_params["namespace"]
        store, err = _store(request, sub)
        if err:
            return err
        try:
            return json_response(request, store.append_row(namespace, body), status=201)
        except NamespaceNotFound:
            return json_error(request, 404, "namespace_not_found")

    async def ds_list_rows(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        namespace = request.path_params["namespace"]
        try:
            limit = int(request.query_params.get("limit", "100"))
        except ValueError:
            limit = 100
        filter_dict: dict[str, str] = {}
        for f in request.query_params.getlist("filter"):
            if ":" in f:
                k, v = f.split(":", 1)
                filter_dict[k.strip()] = v.strip()
        store, err = _store(request, sub)
        if err:
            return err
        try:
            rows = store.list_rows(namespace, filter=filter_dict or None, limit=limit)
        except NamespaceNotFound:
            return json_error(request, 404, "namespace_not_found")
        return json_response(request, {"rows": rows, "count": len(rows)})

    async def ds_get_row(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        namespace = request.path_params["namespace"]
        row_id = request.path_params["row_id"]
        store, err = _store(request, sub)
        if err:
            return err
        try:
            return json_response(request, store.get_row(namespace, row_id))
        except NamespaceNotFound:
            return json_error(request, 404, "namespace_not_found")
        except RowNotFound:
            return json_error(request, 404, "row_not_found")

    async def ds_update_row(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return json_error(request, 400, "invalid_json")
        if not isinstance(body, dict):
            return json_error(request, 400, "invalid_body")
        namespace = request.path_params["namespace"]
        row_id = request.path_params["row_id"]
        store, err = _store(request, sub)
        if err:
            return err
        try:
            return json_response(request, store.update_row(namespace, row_id, body))
        except NamespaceNotFound:
            return json_error(request, 404, "namespace_not_found")
        except RowNotFound:
            return json_error(request, 404, "row_not_found")

    async def ds_delete_row(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        namespace = request.path_params["namespace"]
        row_id = request.path_params["row_id"]
        store, err = _store(request, sub)
        if err:
            return err
        try:
            store.delete_row(namespace, row_id)
        except NamespaceNotFound:
            return json_error(request, 404, "namespace_not_found")
        except RowNotFound:
            return json_error(request, 404, "row_not_found")
        return json_response(request, {"ok": True, "id": row_id})

    async def ds_url(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        namespace = request.path_params["namespace"]
        store, err = _store(request, sub)
        if err:
            return err
        try:
            return json_response(request, {"url": store.get_url(namespace)})
        except NamespaceNotFound:
            return json_error(request, 404, "namespace_not_found")

    async def ds_share(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        namespace = request.path_params["namespace"]
        try:
            body = await request.json()
        except Exception:
            return json_error(request, 400, "invalid_json")
        email = (body.get("email") or "").strip()
        permission = (body.get("permission") or "write").strip()
        if not email:
            return json_error(request, 400, "email_required")
        if permission not in ("read", "write"):
            return json_error(request, 400, "permission must be 'read' or 'write'")
        recipient = db.get_user_by_email(email)
        if not recipient:
            return json_error(request, 404, f"no oto user with email {email}")
        ns = db.get_datastore_namespace(sub, namespace)
        if not ns:
            return json_error(request, 404, "namespace_not_found")
        try:
            db.share_datastore_namespace(sub, namespace, recipient["sub"], permission)
        except ValueError as e:
            return json_error(request, 400, str(e))
        drive_role = "writer" if permission == "write" else "reader"
        drive_warning = None
        try:
            creds = google_oauth.credentials_for(sub)
            from googleapiclient.discovery import build
            drive = build("drive", "v3", credentials=creds, cache_discovery=False)
            drive.permissions().create(
                fileId=ns["spreadsheet_id"],
                body={"type": "user", "role": drive_role, "emailAddress": email},
                sendNotificationEmail=False,
            ).execute()
        except Exception as e:
            drive_warning = str(e)
        result = {"ok": True, "namespace": namespace, "shared_with": email, "permission": permission}
        if drive_warning:
            result["drive_warning"] = drive_warning
        return json_response(request, result)

    async def ds_unshare(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        namespace = request.path_params["namespace"]
        try:
            body = await request.json()
        except Exception:
            return json_error(request, 400, "invalid_json")
        email = (body.get("email") or "").strip()
        if not email:
            return json_error(request, 400, "email_required")
        recipient = db.get_user_by_email(email)
        if not recipient:
            return json_error(request, 404, f"no oto user with email {email}")
        removed = db.unshare_datastore_namespace(sub, namespace, recipient["sub"])
        if not removed:
            return json_error(request, 404, f"no active share for {email} on {namespace}")
        ns = db.get_datastore_namespace(sub, namespace)
        if ns:
            try:
                creds = google_oauth.credentials_for(sub)
                from googleapiclient.discovery import build
                drive = build("drive", "v3", credentials=creds, cache_discovery=False)
                perms = drive.permissions().list(
                    fileId=ns["spreadsheet_id"], fields="permissions(id,emailAddress)",
                ).execute().get("permissions", [])
                for p in perms:
                    if (p.get("emailAddress") or "").lower() == email.lower():
                        drive.permissions().delete(
                            fileId=ns["spreadsheet_id"], permissionId=p["id"],
                        ).execute()
                        break
            except Exception:
                pass
        return json_response(request, {"ok": True, "namespace": namespace, "removed": email})

    return [
        # Google OAuth
        Route("/api/google/oauth/start", google_oauth_start, methods=["GET"]),
        Route("/api/google/oauth/start", options_handler, methods=["OPTIONS"]),
        Route("/api/google/oauth/callback", google_oauth_callback, methods=["GET"]),
        Route("/api/google/oauth/status", google_oauth_status, methods=["GET"]),
        Route("/api/google/oauth/status", options_handler, methods=["OPTIONS"]),
        Route("/api/google/oauth", google_oauth_revoke, methods=["DELETE"]),
        Route("/api/google/oauth", options_handler, methods=["OPTIONS"]),
        # API tokens
        Route("/api/me/tokens", me_tokens_list, methods=["GET"]),
        Route("/api/me/tokens", me_tokens_create, methods=["POST"]),
        Route("/api/me/tokens", options_handler, methods=["OPTIONS"]),
        Route("/api/me/tokens/{token_id}", me_tokens_delete, methods=["DELETE"]),
        Route("/api/me/tokens/{token_id}", options_handler, methods=["OPTIONS"]),
        # Datastore
        Route("/api/datastore/namespaces", ds_list_ns, methods=["GET"]),
        Route("/api/datastore/namespaces", ds_create_ns, methods=["POST"]),
        Route("/api/datastore/namespaces", options_handler, methods=["OPTIONS"]),
        Route("/api/datastore/namespaces/{namespace}", ds_delete_ns, methods=["DELETE"]),
        Route("/api/datastore/namespaces/{namespace}", options_handler, methods=["OPTIONS"]),
        Route("/api/datastore/namespaces/{namespace}/url", ds_url, methods=["GET"]),
        Route("/api/datastore/namespaces/{namespace}/url", options_handler, methods=["OPTIONS"]),
        Route("/api/datastore/namespaces/{namespace}/rows", ds_list_rows, methods=["GET"]),
        Route("/api/datastore/namespaces/{namespace}/rows", ds_append, methods=["POST"]),
        Route("/api/datastore/namespaces/{namespace}/rows", options_handler, methods=["OPTIONS"]),
        Route("/api/datastore/namespaces/{namespace}/rows/{row_id}", ds_get_row, methods=["GET"]),
        Route("/api/datastore/namespaces/{namespace}/rows/{row_id}", ds_update_row, methods=["PATCH"]),
        Route("/api/datastore/namespaces/{namespace}/rows/{row_id}", ds_delete_row, methods=["DELETE"]),
        Route("/api/datastore/namespaces/{namespace}/rows/{row_id}", options_handler, methods=["OPTIONS"]),
        Route("/api/datastore/namespaces/{namespace}/share", ds_share, methods=["POST"]),
        Route("/api/datastore/namespaces/{namespace}/share", ds_unshare, methods=["DELETE"]),
        Route("/api/datastore/namespaces/{namespace}/share", options_handler, methods=["OPTIONS"]),
    ]
