"""Routes REST `/api/admin/connectors/activation` — gouvernance du cran
d'activation des connecteurs (ADR 0010, B4). Admin only.

Le code DÉCLARE les connecteurs (registre `providers.py`) ; la DB décide
lesquels sont EXPOSÉS (`connector_activation`). Cet endpoint est la surface qui
permet à un admin de basculer le master global (et un override par org) sans SQL.

- `GET    /api/admin/connectors/activation`  → tout le registre × état (global + overrides)
- `POST   /api/admin/connectors/activation`  → {connector, enabled, org_id?} pose l'activation
- `DELETE /api/admin/connectors/activation?connector=&org_id=` → supprime un override d'org

⚠️ Le gate est au CHARGEMENT (register_all, au boot) : (dés)activer le master
global prend effet au prochain redémarrage du serveur (`restart_required` dans la
réponse POST). L'override d'org est posé en DB de la même façon.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import access, connector_activation, db, org_store, providers

logger = logging.getLogger(__name__)


AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
) -> list[Route]:

    async def _admin(request: Request) -> tuple[str | None, JSONResponse | None]:
        sub, err = await authenticate(request, verifier)
        if err:
            return None, err
        if not access.is_platform_operator(sub):
            return None, json_error(request, 403, "forbidden")
        return sub, None

    async def list_activation(request: Request) -> JSONResponse:
        """Tout le registre × son état d'activation (global + overrides d'org).

        `enabled` : True/False (master global posé) ou null (jamais posé → OFF,
        deny-by-default). L'admin voit TOUT le registre, même les connecteurs OFF
        (c'est sa surface pour les activer)."""
        sub, err = await _admin(request)
        if err:
            return err
        glob: dict[str, bool] = {}
        overrides: dict[str, list] = {}
        for r in connector_activation.list_activations():
            if r["org_id"] is None:
                glob[r["connector"]] = bool(r["enabled"])
            else:
                overrides.setdefault(r["connector"], []).append(
                    {"org_id": r["org_id"], "enabled": bool(r["enabled"])}
                )
        out = [
            {
                "connector": name,
                "label": c.label,
                "help": c.help,
                "namespaces": list(c.namespaces),
                "enabled": glob.get(name),  # None = jamais posé = OFF
                "overrides": overrides.get(name, []),
                "paid_option": access.paid_option_for(name),  # couche 3 (ADR 0044 §H) ou None
            }
            for name, c in providers.REGISTRY.items()
        ]
        return json_response(request, {"connectors": out})

    async def set_activation(request: Request) -> JSONResponse:
        """Pose l'activation : master global si `org_id` absent, sinon override d'org.
        Body {connector, enabled: bool, org_id?: int}."""
        sub, err = await _admin(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return json_error(request, 400, "invalid_json")
        connector = body.get("connector")
        enabled = body.get("enabled")
        org_id = body.get("org_id")
        if connector not in providers.REGISTRY:
            return json_error(request, 400, "unknown_connector")
        if not isinstance(enabled, bool):
            return json_error(request, 400, "enabled_must_be_bool")
        if org_id is not None and not isinstance(org_id, int):
            return json_error(request, 400, "org_id_must_be_int")
        connector_activation.set_activation(connector, enabled, org_id=org_id, set_by=sub)
        # Le chargement des tools est résolu au boot → un changement de master
        # global ne prend effet qu'au prochain redémarrage.
        return json_response(request, {
            "ok": True,
            "connector": connector,
            "enabled": enabled,
            "org_id": org_id,
            "restart_required": org_id is None,
        })

    async def clear_override(request: Request) -> JSONResponse:
        """Supprime un override d'org (le connecteur retombe sur le master global).
        Query `?connector=&org_id=`."""
        sub, err = await _admin(request)
        if err:
            return err
        connector = request.query_params.get("connector")
        org_id_raw = request.query_params.get("org_id")
        if not connector or not org_id_raw:
            return json_error(request, 400, "connector_and_org_id_required")
        try:
            org_id = int(org_id_raw)
        except ValueError:
            return json_error(request, 400, "org_id_must_be_int")
        connector_activation.clear_activation(connector, org_id)
        return json_response(request, {"ok": True, "connector": connector, "org_id": org_id})

    async def unipile_connect(request: Request) -> JSONResponse:
        """Hosted-auth Unipile (B2) : génère l'URL où l'user connecte SON LinkedIn
        sous l'abonnement partagé (clé de son org). On pose un **nonce** aléatoire
        comme `name` (le `name` ne revient pas dans /accounts → corrélation via le
        webhook `notify_url` qui, lui, l'échoit). Per-user (pas admin)."""
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            body = {}
        # Corps partagé REST + MCP (`unipile_connect_start`, feedback #131) :
        # gates + nonce + hosted_auth_link vivent dans `unipile_connect`.
        from . import unipile_connect
        try:
            out = await unipile_connect.hosted_auth_url(
                sub, str(body.get("channel") or "linkedin"),
                force=bool(body.get("force")),
                # `premium` = 'recruiter' | 'sales_navigator' : produit LinkedIn à
                # ACTIVER à la connexion (sinon classic seul → 403 sur ces APIs).
                premium=(str(body["premium"]).strip().lower()
                         if body.get("premium") else None))
        except unipile_connect.ConnectRefused as e:
            # 502 (échec amont) et 409 (doublon cross-org, #172) portent un message
            # actionnable → on le renvoie ; les autres exposent leur code machine.
            detail = e.message if e.status in (409, 502) else e.code
            return json_error(request, e.status, detail)
        # Adoption (binding-par-org) : le compte connecté ailleurs a été lié ICI sans
        # wizard → pas d'URL, le front rafraîchit ({adopted, account_name, channel}).
        if out.get("adopted"):
            return json_response(request, out)
        return json_response(request, {"url": out["url"]})

    async def unipile_webhook(request: Request) -> JSONResponse:
        """Notification Unipile au succès du hosted-auth (B3). **NON authentifié**
        (Unipile l'appelle, server-to-server) → sécurisé par le **nonce** : on ne
        lie le compte que si `name` est un nonce VIVANT qu'on a nous-mêmes posé
        (non devinable, court). Logue le payload brut pour instrumenter le format
        réel. Toujours 200 (ack ; un échec ne doit pas faire rejouer Unipile en
        boucle)."""
        raw = await request.body()
        logger.info("unipile webhook raw=%s", raw[:2000])
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            return JSONResponse({"ok": True})
        # Format réel confirmé (instrumenté 2026-06-18) :
        # {status:"CREATION_SUCCESS", account_id, name:<nonce>, account_type}.
        # On ne lie QUE sur un succès de création — un événement d'échec/autre ne
        # doit pas mapper un account_id. Le nonce (consommé au 1er resolve) protège
        # déjà du double-binding.
        status = body.get("status")
        name = body.get("name")
        account_id = body.get("account_id") or body.get("accountId") or body.get("id")
        if status == "CREATION_SUCCESS" and name and account_id:
            pend = db.resolve_unipile_pending(name)
            if pend:
                # Filet : un pending émis AVANT le deploy B4 (BYO) porte org_id NULL
                # → org maison du sub (le binding doit toujours avoir une org).
                org_id = pend.get("org_id") or org_store.get_active_org(pend["sub"])
                db.set_unipile_account(pend["sub"], account_id, org_id=org_id,
                                       provider=pend.get("provider", "LINKEDIN"),
                                       platform_seat=bool(pend.get("platform_seat")))
                logger.info("unipile webhook: bound sub=%s account_id=%s org=%s",
                            pend["sub"], account_id, pend.get("org_id"))
            else:
                logger.warning("unipile webhook: nonce inconnu/expiré name=%s", name)
        elif status and status != "CREATION_SUCCESS":
            logger.info("unipile webhook: statut ignoré status=%s name=%s", status, name)
        return JSONResponse({"ok": True})

    async def unipile_status(request: Request) -> JSONResponse:
        """Statut de connexion Unipile per-user (pour le dashboard). **Self-heal** :
        le webhook hosted-auth v2 n'étant pas livré, on réconcilie (poll-and-bind)
        les comptes fraîchement connectés au chargement du statut — no-op sans
        pending (donc sans appel Unipile). Best-effort : jamais fatal pour le statut."""
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        from . import unipile_connect
        try:
            await asyncio.to_thread(unipile_connect.reconcile_pending, sub)
        except Exception:  # noqa: BLE001 — réconciliation opportuniste, jamais bloquante
            logger.warning("unipile status: reconcile best-effort échoué", exc_info=True)
        from .tools import unipile
        return json_response(request, unipile.status_for(sub))

    async def unipile_reconcile(request: Request) -> JSONResponse:
        """Poll-and-bind explicite (webhook v2 non livré) : lie le compte que `sub`
        vient de connecter. Le dashboard peut l'appeler au retour du hosted-auth
        (`?unipile=connected`). Idempotent."""
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        from . import unipile_connect
        out = await asyncio.to_thread(unipile_connect.reconcile_pending, sub)
        return json_response(request, out)

    async def unipile_disconnect(request: Request) -> JSONResponse:
        """SOFT-déconnecte le canal DANS CETTE ORG (ne supprime pas le compte chez
        Unipile ; la ligne survit comme preuve de propriété → rebind déterministe à la
        reconnexion). Par-org : le binding est un acte par org (modèle explicite) —
        et l'affichage ne montrant QUE les bindings de l'org courante, ce qu'on voit
        est ce qu'on déconnecte (plus de résurgence cross-org, ex-#221)."""
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        provider = str(request.query_params.get("channel") or "linkedin").upper()
        db.clear_unipile_account(sub, access.current_org(sub), provider)
        return json_response(request, {"ok": True})

    async def unipile_platform_seats(request: Request) -> JSONResponse:
        """[super_admin] Sièges de la **clé plateforme** unipile : tous les comptes
        présents sur l'instance partagée (Otomata, api.unipile.com), réconciliés avec leur
        propriétaire oto via `unipile_accounts`. Un compte sur l'instance NON mappé =
        **orphelin** (créé puis user churné → coûte ~5 €/mois pour rien). Révèle
        l'ownership cross-user → super_admin only. Ne renvoie aucun secret."""
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        if not access.is_super_admin(sub):
            return json_error(request, 403, "forbidden")
        from . import credentials_store
        insts = credentials_store.list_platform_instances("unipile")  # ADR 0044 §F : coffre unifié
        if not insts:
            return json_response(request, {"configured": False, "seats": [],
                                           "instance_dsn": None, "orphan_count": 0})
        api_key = credentials_store.get_credential(credentials_store.PLATFORM, insts[0]["label"], "unipile")
        from oto.tools.unipile import UnipileClient
        client = UnipileClient(api_key=api_key)  # dsn=None → env/api.unipile.com (instance plateforme)
        try:
            instance = await asyncio.to_thread(client.list_accounts)
        except Exception as e:
            return json_error(request, 502, f"unipile_list_failed: {e}")
        owners = {r["account_id"]: r for r in db.unipile_account_owners()}
        seats = []
        for a in instance:
            o = owners.get(a.get("id"))
            srcs = a.get("sources") or []
            seats.append({
                "account_id": a.get("id"),
                "name": a.get("name"),
                "type": a.get("type"),
                "status": (srcs[0].get("status") if srcs else None) or "ok",
                "owner_sub": o["sub"] if o else None,
                "owner_email": o["email"] if o else None,
                "org_id": o["org_id"] if o else None,
                "org_name": o["org_name"] if o else None,
                "orphan": o is None,
            })
        return json_response(request, {
            "configured": True,
            "instance_dsn": client.dsn,
            "seats": seats,
            "orphan_count": sum(1 for s in seats if s["orphan"]),
        })

    async def platform_access(request: Request) -> JSONResponse:
        """[super_admin] Accès PLATEFORME d'un connecteur (ADR 0044 §H) : les orgs et
        membres à qui la plateforme ouvre ce connecteur — grantees de la clé plateforme
        (`share_down` des instances scope PLATFORM, §F) ∪ bénéficiaires de l'option comp
        (couche 3). Vue connecteur-centrique UNIQUE (remplace les leviers dispersés
        /platform/orgs · /platform/users). Aucun secret."""
        sub, err = await _admin(request)
        if err:
            return err
        provider = request.path_params["provider"]
        if provider not in providers.REGISTRY:
            return json_error(request, 404, "unknown_connector")
        from . import credentials_store
        option = access.paid_option_for(provider)

        acc: dict[str, dict] = {}

        def touch(scope: str, sid: str) -> dict:
            k = f"{scope}:{sid}"
            if k not in acc:
                acc[k] = {"scope": scope, "id": sid, "has_key": False, "has_option": False}
            return acc[k]

        insts = credentials_store.list_platform_instances(provider)
        open_tier = any(i["share_mode"] == "open" for i in insts)
        for inst in insts:
            for g in inst["share_down"]:
                scope, _, sid = str(g).partition(":")
                if scope in ("user", "org") and sid:
                    touch(scope, sid)["has_key"] = True
        if option:
            for c in db.list_option_comps_for_option(option):
                if c["entity_type"] in ("user", "org"):
                    touch(c["entity_type"], str(c["entity_id"]))["has_option"] = True

        out = []
        for rec in acc.values():
            if rec["scope"] == "org":
                o = org_store.get_org(int(rec["id"])) if rec["id"].isdigit() else None
                rec["label"] = o["name"] if o else f"org #{rec['id']}"
                rec["logo_url"] = org_store.effective_logo_url(o) if o else None
            else:
                u = db.get_user(rec["id"])
                rec["label"] = (u.get("name") or u.get("email") or rec["id"]) if u else rec["id"]
                rec["email"] = u.get("email") if u else None
            out.append(rec)
        out.sort(key=lambda r: (r["scope"], (r["label"] or "").lower()))
        return json_response(request, {
            "connector": provider,
            "paid_option": option,          # None = pas d'option payante (couche 3)
            "platform_key": bool(insts),    # une clé plateforme existe (couche 2)
            "open_tier": open_tier,         # free-tier : ouvert à tous sans grant
            "beneficiaries": out,
        })

    async def set_platform_access(request: Request) -> JSONResponse:
        """[super_admin] Acte UNIQUE « accès plateforme » (ADR 0044 §H) : ouvre/ferme
        l'accès plateforme d'une org ou d'un membre à un connecteur = pose ENSEMBLE
        l'option comp (couche 3) ET le grant de la clé plateforme (couche 2) — ce que
        le backend couplait déjà, exposé en un geste. L'effet suit le connecteur :
        option payante ⟹ comp (+ clé si mode-plateforme) ; keyé sans option ⟹ grant
        de clé seul. Body: {scope:'org'|'user', id, on:bool}."""
        sub, err = await _admin(request)
        if err:
            return err
        if not access.is_super_admin(sub):
            return json_error(request, 403, "forbidden")
        provider = request.path_params["provider"]
        if provider not in providers.REGISTRY:
            return json_error(request, 404, "unknown_connector")
        try:
            body = await request.json()
        except Exception:
            return json_error(request, 400, "invalid_body")
        scope = body.get("scope")
        sid = str(body.get("id", "")).strip()
        on = bool(body.get("on"))
        if scope not in ("org", "user") or not sid:
            return json_error(request, 400, "invalid_body")
        # existence (pas de grant vers un fantôme)
        if scope == "org":
            if not sid.isdigit() or not org_store.get_org(int(sid)):
                return json_error(request, 404, "unknown_org")
        elif not db.get_user(sid):
            return json_error(request, 404, "unknown_user")

        from . import credentials_store
        option = access.paid_option_for(provider)
        has_key = bool(credentials_store.list_platform_instances(provider))
        if not option and not has_key:
            # ni option payante ni clé plateforme → rien à ouvrir côté plateforme
            return json_error(request, 400, "no_platform_access")
        gscope = f"{scope}:{sid}"
        if on:
            if option:
                db.set_option_comp(scope, sid, option, granted_by=sub)
            if has_key:
                credentials_store.platform_grant(provider, gscope)
        else:
            if option:
                db.clear_option_comp(scope, sid, option)
            if has_key:
                credentials_store.platform_revoke(provider, gscope)
        return json_response(request, {
            "ok": True, "connector": provider, "scope": scope, "id": sid, "on": on,
            "paid_option": option, "platform_key": has_key,
        })

    return [
        Route("/api/admin/unipile/seats", unipile_platform_seats, methods=["GET"]),
        Route("/api/admin/unipile/seats", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/connectors/{provider}/platform-access", platform_access, methods=["GET"]),
        Route("/api/admin/connectors/{provider}/platform-access", set_platform_access, methods=["POST"]),
        Route("/api/admin/connectors/{provider}/platform-access", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/connectors/activation", list_activation, methods=["GET"]),
        Route("/api/admin/connectors/activation", set_activation, methods=["POST"]),
        Route("/api/admin/connectors/activation", clear_override, methods=["DELETE"]),
        Route("/api/admin/connectors/activation", options_handler, methods=["OPTIONS"]),
        Route("/api/me/unipile/connect", unipile_connect, methods=["POST"]),
        Route("/api/me/unipile/connect", options_handler, methods=["OPTIONS"]),
        Route("/api/me/unipile/reconcile", unipile_reconcile, methods=["POST"]),
        Route("/api/me/unipile/reconcile", options_handler, methods=["OPTIONS"]),
        Route("/api/unipile/webhook", unipile_webhook, methods=["POST"]),
        Route("/api/me/unipile", unipile_status, methods=["GET"]),
        Route("/api/me/unipile", unipile_disconnect, methods=["DELETE"]),
        Route("/api/me/unipile", options_handler, methods=["OPTIONS"]),
    ]
