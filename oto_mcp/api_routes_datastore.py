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
- `GET    /api/datastore/namespaces/{ns}/url`   → deep-link dashboard du namespace
- `GET    /api/datastore/namespaces/{ns}/rows`  → liste les rows (filter=k:v, limit=N)
- `POST   /api/datastore/namespaces/{ns}/rows`  → append row
- `GET    /api/datastore/namespaces/{ns}/rows/{row_id}`    → fetch row
- `PATCH  /api/datastore/namespaces/{ns}/rows/{row_id}`    → update row
- `DELETE /api/datastore/namespaces/{ns}/rows/{row_id}`    → delete row

Auth : Bearer JWT Logto **ou** API token long-lived (préfixe `oto_`),
résolu via `_authenticate` (partagé avec `api_routes.py`).
"""
from __future__ import annotations

import json
import os
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import db, google_oauth, ownership, roles
from .datastore import (
    NamespaceExists,
    NamespaceForbidden,
    NamespaceNotFound,
    NamespaceReadOnly,
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
        parsed = google_oauth.verify_state(state)
        if not parsed:
            return json_error(request, 400, "invalid_state")
        sub, org_id = parsed
        try:
            tokens = google_oauth.exchange_code(code)
            google_oauth.persist_token(sub, org_id, tokens)
        except Exception as e:
            return json_error(request, 502, f"oauth_exchange_failed: {e}")
        # Retour vers la page connecteurs (où vit la config Google, ADR 0024 B2).
        # `datastore` n'est plus Google Sheets (ADR 0016, PG natif) → ex-signal
        # `?datastore=connected` retiré.
        return RedirectResponse(url=f"{_app_url()}/console/connectors?google=connected", status_code=302)

    async def google_oauth_status(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        accounts = google_oauth.list_accounts(sub)
        default = next((a for a in accounts if a.get("is_default")), None)
        return json_response(request, {
            "connected": bool(accounts),
            # Compat : champs au niveau racine = compte par défaut.
            "granted_at": default["granted_at"] if default else None,
            "scopes": default["scopes"].split() if default and default.get("scopes") else [],
            "accounts": [
                {
                    "email": a.get("google_email"),
                    "is_default": a.get("is_default", False),
                    "scopes": a["scopes"].split() if a.get("scopes") else [],
                    "granted_at": a.get("granted_at"),
                }
                for a in accounts
            ],
        })

    async def google_oauth_revoke(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        # ?account=<email> révoque un compte précis ; absent = tous.
        account = request.query_params.get("account") or None
        google_oauth.revoke(sub, account=account)
        return json_response(request, {"ok": True, "account": account})

    async def google_oauth_set_default(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            body = {}
        account = (body.get("account") if isinstance(body, dict) else None) or ""
        account = account.strip()
        if not account:
            return json_error(request, 400, "missing_account")
        org_id = access.current_org(sub)
        if org_id is None or not db.set_default_google_account(sub, org_id, account):
            return json_error(request, 404, "unknown_account")
        return json_response(request, {"ok": True, "default": account})

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

    # --- Datastore (PG natif, ADR 0016) ----------------------------------

    async def ds_list_ns(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        return json_response(request, {"namespaces": make_store(sub).list_namespaces()})

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
        # owner optionnel (ADR 0030) : classeur d'org/groupe. Défaut = perso.
        owner = (body or {}).get("owner") or {}
        owner_type = (owner.get("type") or "user").strip()
        owner_id = sub
        if owner_type == "org":
            try:
                org_id = int(owner.get("id"))
            except (TypeError, ValueError):
                return json_error(request, 400, "invalid_owner_id")
            if not roles.is_org_member(sub, org_id):
                return json_error(request, 403, "not_org_member")
            owner_id = str(org_id)
        elif owner_type == "group":
            try:
                group_id = int(owner.get("id"))
            except (TypeError, ValueError):
                return json_error(request, 400, "invalid_owner_id")
            if not roles.can_read_group(sub, group_id):
                return json_error(request, 403, "not_group_member")
            owner_id = str(group_id)
        elif owner_type != "user":
            return json_error(request, 400, "invalid_owner_type")
        try:
            created = make_store(sub).create_namespace(
                namespace, owner_type=owner_type, owner_id=owner_id)
            return json_response(request, created, status=201)
        except NamespaceExists:
            return json_error(request, 409, "namespace_exists")

    async def ds_delete_ns(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        namespace = request.path_params["namespace"]
        try:
            make_store(sub).delete_namespace(namespace)
        except NamespaceNotFound:
            return json_error(request, 404, "namespace_not_found")
        except NamespaceForbidden:
            return json_error(request, 403, "forbidden")
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
        try:
            return json_response(request, make_store(sub).append_row(namespace, body), status=201)
        except NamespaceNotFound:
            return json_error(request, 404, "namespace_not_found")
        except NamespaceReadOnly:
            return json_error(request, 403, "namespace_read_only")

    async def ds_list_rows(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        namespace = request.path_params["namespace"]
        qp = request.query_params

        def _int(name: str, default: int) -> int:
            try:
                return int(qp.get(name, default))
            except (TypeError, ValueError):
                return default

        offset = max(0, _int("offset", 0))
        limit = min(500, max(1, _int("limit", 50)))
        order_by = qp.get("order_by") or None
        order_dir = qp.get("order_dir", "desc")
        q = qp.get("q") or None
        filters = None
        raw_filters = qp.get("filters")
        if raw_filters:
            try:
                filters = json.loads(raw_filters)
            except ValueError:
                return json_error(request, 400, "invalid_filters")
            if not isinstance(filters, list):
                return json_error(request, 400, "invalid_filters")
        try:
            page = make_store(sub).page_rows(
                namespace, offset=offset, limit=limit,
                order_by=order_by, order_dir=order_dir, q=q, filters=filters)
        except NamespaceNotFound:
            return json_error(request, 404, "namespace_not_found")
        except ValueError:
            return json_error(request, 400, "invalid_filters")
        return json_response(request, page)

    async def ds_row_activity(request: Request) -> JSONResponse:
        """Parcours de l'agent d'une row (ADR 0046 b4) : appels `data_*` du calllog
        corrélés à cette row (par `_id` OU valeur de clé métier) + leur run. Gate =
        accès LECTURE au namespace (la row est relue via le store, jamais l'id nu)."""
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        namespace = request.path_params["namespace"]
        row_id = request.path_params["row_id"]
        store = make_store(sub)
        try:
            row = store.get_row(namespace, row_id)
        except NamespaceNotFound:
            return json_error(request, 404, "namespace_not_found")
        except RowNotFound:
            return json_error(request, 404, "row_not_found")
        key = store.declared_key(namespace)
        key_value = row.get(key) if key else None
        activity = db.datastore_row_activity(
            row_id, str(key_value) if key_value is not None else None)
        return json_response(request, {"activity": activity, "key": key,
                                       "retention_days": 30})

    async def ds_aggregate(request: Request) -> JSONResponse:
        """Agrégat serveur (ADR 0046 b1 — compteurs du cockpit) : COUNT/SUM/AVG/…
        groupés par un champ JSONB, sans rapatrier les rows. Miroir REST du tool
        MCP `data_aggregate` (délègue au même `store.aggregate`)."""
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        namespace = request.path_params["namespace"]
        qp = request.query_params
        group_by = qp.get("group_by") or None
        metrics = None
        raw_metrics = qp.get("metrics")
        if raw_metrics:
            try:
                metrics = json.loads(raw_metrics)
            except ValueError:
                return json_error(request, 400, "invalid_metrics")
        filter_eq = None
        raw_filter = qp.get("filter")
        if raw_filter:
            try:
                filter_eq = json.loads(raw_filter)
            except ValueError:
                return json_error(request, 400, "invalid_filter")
            if not isinstance(filter_eq, dict):
                return json_error(request, 400, "invalid_filter")
        try:
            groups = make_store(sub).aggregate(
                namespace, group_by=group_by, metrics=metrics, filter=filter_eq)
        except NamespaceNotFound:
            return json_error(request, 404, "namespace_not_found")
        except ValueError:
            return json_error(request, 400, "invalid_aggregate")
        return json_response(request, {"groups": groups})

    async def ds_get_row(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        namespace = request.path_params["namespace"]
        row_id = request.path_params["row_id"]
        try:
            return json_response(request, make_store(sub).get_row(namespace, row_id))
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
        try:
            return json_response(request, make_store(sub).update_row(namespace, row_id, body))
        except NamespaceNotFound:
            return json_error(request, 404, "namespace_not_found")
        except NamespaceReadOnly:
            return json_error(request, 403, "namespace_read_only")
        except RowNotFound:
            return json_error(request, 404, "row_not_found")

    async def ds_delete_row(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        namespace = request.path_params["namespace"]
        row_id = request.path_params["row_id"]
        try:
            make_store(sub).delete_row(namespace, row_id)
        except NamespaceNotFound:
            return json_error(request, 404, "namespace_not_found")
        except NamespaceReadOnly:
            return json_error(request, 403, "namespace_read_only")
        except RowNotFound:
            return json_error(request, 404, "row_not_found")
        return json_response(request, {"ok": True, "id": row_id})

    async def ds_url(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        namespace = request.path_params["namespace"]
        try:
            return json_response(request, {"url": make_store(sub).get_url(namespace)})
        except NamespaceNotFound:
            return json_error(request, 404, "namespace_not_found")

    async def ds_set_schema(request: Request) -> JSONResponse:
        """Pose/retire le schéma typé d'un namespace (ADR 0032 §6 / 0029, B6).
        Corps : {schema: {fields:[...]}} ou {schema: null} pour repasser en table libre."""
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
        try:
            return json_response(request, make_store(sub).set_schema(namespace, body.get("schema")))
        except NamespaceNotFound:
            return json_error(request, 404, "namespace_not_found")
        except NamespaceReadOnly:
            return json_error(request, 403, "namespace_read_only")
        except ValueError:
            return json_error(request, 400, "invalid_schema")

    def _govern_ns(sub: str, namespace: str) -> tuple[int | None, tuple[int, str] | None]:
        """Résout le namespace par nom + vérifie le droit de GOUVERNANCE de l'acteur
        (owner ∪ escalade roles.py). Retourne (ns_id, None) ou (None, (status, code))."""
        try:
            ns_id = make_store(sub).resolve_ns_id(namespace)
        except NamespaceNotFound:
            return None, (404, "namespace_not_found")
        if not ownership.can_govern(sub, "datastore_namespace", str(ns_id)):
            return None, (403, "forbidden")
        return ns_id, None

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
        ns_id, gerr = _govern_ns(sub, namespace)
        if gerr:
            return json_error(request, gerr[0], gerr[1])
        ownership.grant("datastore_namespace", str(ns_id), "user", recipient["sub"],
                        permission, granted_by=sub)
        return json_response(
            request,
            {"ok": True, "namespace": namespace, "shared_with": email, "permission": permission},
        )

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
        ns_id, gerr = _govern_ns(sub, namespace)
        if gerr:
            return json_error(request, gerr[0], gerr[1])
        removed = ownership.revoke("datastore_namespace", str(ns_id), "user", recipient["sub"])
        if not removed:
            return json_error(request, 404, f"no active share for {email} on {namespace}")
        return json_response(request, {"ok": True, "namespace": namespace, "removed": email})

    async def ds_list_shares(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        namespace = request.path_params["namespace"]
        ns_id, gerr = _govern_ns(sub, namespace)
        if gerr:
            return json_error(request, gerr[0], gerr[1])
        shares = [
            {"email": s.get("email"), "permission": s.get("permission"),
             "principal_type": s.get("principal_type"), "principal_id": s.get("principal_id"),
             "created_at": s.get("granted_at")}
            for s in ownership.list_grants("datastore_namespace", str(ns_id))
        ]
        return json_response(request, {"shares": shares})

    async def ds_rename(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        namespace = request.path_params["namespace"]
        try:
            body = await request.json()
        except Exception:
            return json_error(request, 400, "invalid_json")
        new = ((body or {}).get("name") or "").strip()
        if not new:
            return json_error(request, 400, "name_required")
        ns_id, gerr = _govern_ns(sub, namespace)
        if gerr:
            return json_error(request, gerr[0], gerr[1])
        try:
            db.rename_datastore_namespace_by_id(ns_id, new)
        except ValueError as e:
            return json_error(request, 409, str(e))
        return json_response(request, {"ok": True, "namespace": new})

    async def ds_transfer(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        namespace = request.path_params["namespace"]
        try:
            body = await request.json()
        except Exception:
            return json_error(request, 400, "invalid_json")
        # Cible : une de SES orgs (`new_owner_org`, owner_type='org', ADR 0030) OU un
        # utilisateur (`email`). Transférer VERS une org exige d'en être membre.
        new_owner_org = (body or {}).get("new_owner_org")
        if new_owner_org is not None:
            try:
                org_id = int(new_owner_org)
            except (TypeError, ValueError):
                return json_error(request, 400, "invalid_org")
            if not roles.is_org_member(sub, org_id):
                return json_error(request, 403, "not_org_member")
            new_owner_type, new_owner_id = "org", str(org_id)
            from . import org_store
            new_owner_label = (org_store.get_org(org_id) or {}).get("name") or f"#{org_id}"
        else:
            email = ((body or {}).get("email") or "").strip()
            if not email:
                return json_error(request, 400, "email_required")
            recipient = db.get_user_by_email(email)
            if not recipient:
                return json_error(request, 404, f"no oto user with email {email}")
            new_owner_type, new_owner_id, new_owner_label = "user", recipient["sub"], email
        ns_id, gerr = _govern_ns(sub, namespace)
        if gerr:
            return json_error(request, gerr[0], gerr[1])
        try:
            ownership.transfer("datastore_namespace", str(ns_id), new_owner_type, new_owner_id)
        except ValueError as e:
            return json_error(request, 409, str(e))
        return json_response(request, {"ok": True, "namespace": namespace, "new_owner": new_owner_label})

    return [
        # Google OAuth
        Route("/api/google/oauth/start", google_oauth_start, methods=["GET"]),
        Route("/api/google/oauth/start", options_handler, methods=["OPTIONS"]),
        Route("/api/google/oauth/callback", google_oauth_callback, methods=["GET"]),
        Route("/api/google/oauth/status", google_oauth_status, methods=["GET"]),
        Route("/api/google/oauth/status", options_handler, methods=["OPTIONS"]),
        Route("/api/google/oauth", google_oauth_revoke, methods=["DELETE"]),
        Route("/api/google/oauth/default", google_oauth_set_default, methods=["POST"]),
        Route("/api/google/oauth/default", options_handler, methods=["OPTIONS"]),
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
        Route("/api/datastore/namespaces/{namespace}/schema", ds_set_schema, methods=["PUT"]),
        Route("/api/datastore/namespaces/{namespace}/schema", options_handler, methods=["OPTIONS"]),
        Route("/api/datastore/namespaces/{namespace}/aggregate", ds_aggregate, methods=["GET"]),
        Route("/api/datastore/namespaces/{namespace}/aggregate", options_handler, methods=["OPTIONS"]),
        Route("/api/datastore/namespaces/{namespace}/rows", ds_list_rows, methods=["GET"]),
        Route("/api/datastore/namespaces/{namespace}/rows", ds_append, methods=["POST"]),
        Route("/api/datastore/namespaces/{namespace}/rows", options_handler, methods=["OPTIONS"]),
        Route("/api/datastore/namespaces/{namespace}/rows/{row_id}/activity", ds_row_activity, methods=["GET"]),
        Route("/api/datastore/namespaces/{namespace}/rows/{row_id}/activity", options_handler, methods=["OPTIONS"]),
        Route("/api/datastore/namespaces/{namespace}/rows/{row_id}", ds_get_row, methods=["GET"]),
        Route("/api/datastore/namespaces/{namespace}/rows/{row_id}", ds_update_row, methods=["PATCH"]),
        Route("/api/datastore/namespaces/{namespace}/rows/{row_id}", ds_delete_row, methods=["DELETE"]),
        Route("/api/datastore/namespaces/{namespace}/rows/{row_id}", options_handler, methods=["OPTIONS"]),
        Route("/api/datastore/namespaces/{namespace}", ds_rename, methods=["PATCH"]),
        Route("/api/datastore/namespaces/{namespace}/share", ds_list_shares, methods=["GET"]),
        Route("/api/datastore/namespaces/{namespace}/share", ds_share, methods=["POST"]),
        Route("/api/datastore/namespaces/{namespace}/share", ds_unshare, methods=["DELETE"]),
        Route("/api/datastore/namespaces/{namespace}/share", options_handler, methods=["OPTIONS"]),
        Route("/api/datastore/namespaces/{namespace}/transfer", ds_transfer, methods=["POST"]),
        Route("/api/datastore/namespaces/{namespace}/transfer", options_handler, methods=["OPTIONS"]),
    ]
