"""Retour de consentement OAuth Zoho — la SEULE route qui reste écrite à la main.

Zoho redirige ici le **navigateur** de l'utilisateur : pas d'en-tête d'auth (l'identité
vient du `state` signé) et la réponse est un **302** vers le dashboard. Un contrat de
capacité (JSON + autz) ne peut pas exprimer ça — d'où l'exception, déclarée comme telle
dans `tests/test_rest_modules_are_capabilities.py`.

Les verbes qui l'accompagnent (`start`, `modes`) sont, eux, des **capacités**
(`capabilities/zoho_connect.py`, ADR 0042 §Convergence des surfaces) : une déclaration,
deux faces dérivées (REST pour le dashboard, MCP pour l'agent), une seule autz.

Une seule URI de redirection sert les TROIS connecteurs Zoho — le connecteur voyage
dans le `state` — car une URI s'enregistre au byte près côté Zoho : une seule à
déclarer par app au lieu de trois.
"""
from __future__ import annotations

import logging
import os
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from ..auth import zoho as zoho_oauth
from .. import config

logger = logging.getLogger(__name__)

AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
) -> list[Route]:

    def _app_url() -> str:
        return config.dashboard_url()

    def _retour(etat: str, return_app: str = "", org_id: "int | None" = None,
               connector: str = "zoho") -> str:
        """Où renvoyer le navigateur après le consentement Zoho.

        `return_app` = le front qui a DEMANDÉ la connexion, relu du state signé (`""`
        quand le state n'a pas pu être lu : on n'a alors aucun moyen de savoir qui
        rappeler, cas dégradé assumé). Tant que cette URL était codée sur
        oto-dashboard, un utilisateur d'un front partenaire finissait son consentement
        chez un autre produit.

        Le défaut reste `/console/connectors` À L'OCTET PRÈS : c'est un chemin propre
        au dashboard, que le patron générique `return_url` ne connaît pas — y
        retomber renverrait l'appelant historique sur `/connectors`.

        Convention unique de retour OAuth (oto-backend#670) : le suffixe vient
        maintenant du fabricant partagé `oauth_flow.connector_return_suffix`, qui
        DOUBLE l'ancienne forme `?<connector>=connected` / `?zoho=error` — encore
        lue par le dashboard aujourd'hui — le temps du préavis. `connector` par
        défaut à `"zoho"` : c'est la valeur historique servie quand le state est
        illisible (avant ce lot, l'échec portait TOUJOURS `?zoho=error`, même pour
        zohodesk/zohoanalytics) — un choix qu'on préserve pour la forme héritée
        SEULEMENT ; la forme neuve porte, elle, le connecteur réellement connecté."""
        from ..auth import flow as oauth_flow
        legacy_cle = connector if etat == "connected" else "zoho"
        suffix = oauth_flow.connector_return_suffix(
            connector, etat, legacy=(legacy_cle, etat))
        if oauth_flow.resolve_return_app(return_app):
            return oauth_flow.return_url(return_app, suffix, org=org_id)
        return f"{_app_url()}/console/connectors{suffix}"

    async def callback(request: Request) -> Response:
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        parsed = zoho_oauth.verify_state(state) if state else None
        if not code or not parsed:
            # State illisible : ni `return_app` ni `org` (ils vivent dedans), ni le
            # connecteur réel — `_retour` retombe sur son défaut ("zoho").
            return RedirectResponse(_retour("error"),
                                    status_code=302)

        def _finish() -> None:
            # ⚠️ La RÉGION doit être repassée : l'app d'éditeur est keyée par data
            # center. Sans elle, `app_fields` ne verrait que le BYO et l'échange du
            # code échouerait pour tout utilisateur venu par l'app d'oto — alors même
            # que le consentement, lui, a réussi.
            app = zoho_oauth.app_fields(parsed["connector"], parsed["sub"],
                                        parsed["data_center"])
            tokens = zoho_oauth.exchange_code(code, parsed["data_center"], app=app)
            zoho_oauth.persist(parsed["sub"], parsed["org"], parsed["connector"],
                               parsed["data_center"], tokens, app=app)

        try:
            await run_in_threadpool(_finish)   # DB + HTTP sync → hors event loop
        except Exception as e:  # noqa: BLE001
            # Jamais le détail dans l'URL (il pourrait porter un message amont) ;
            # le diagnostic va au journal, sans secret (#284).
            logger.warning("zoho oauth callback failed: %s", type(e).__name__)
            return RedirectResponse(
                _retour("error", parsed["return_app"], parsed["org"],
                        parsed["connector"]),
                status_code=302)
        return RedirectResponse(
            _retour("connected", parsed["return_app"], parsed["org"],
                    parsed["connector"]),
            status_code=302)

    return [Route("/api/zoho/oauth/callback", callback, methods=["GET"])]
