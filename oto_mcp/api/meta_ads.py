"""Route de retour du consentement Meta Ads — la seule pièce écrite à la main.

Même forme qu'`api/instagram_meta.py` : le `/start` passe par le seam commun
(`connectors/flow`), seul le callback est une route (Meta redirige un NAVIGATEUR,
sans en-tête d'auth). L'identité vient du `state` signé.

⚠️ Tant que l'application n'a pas l'accès requis, Meta peut refuser le dialogue
avec `error=access_denied` — indistinguable d'un vrai refus de la personne.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from ..auth import flow as oauth_flow
from ..auth import meta_ads as ads_auth

logger = logging.getLogger(__name__)

AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]

_REFUS = "access_denied"


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
) -> list[Route]:

    def _retour(etat: str, return_app: str = "", org_id: int | None = None) -> str:
        return oauth_flow.connector_return_url(
            return_app, ads_auth.CONNECTOR, etat, org=org_id)

    async def callback(request: Request) -> Response:
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        erreur = request.query_params.get("error")
        parsed = ads_auth.verify_state(state) if state else None
        if not parsed:
            logger.info("meta_ads : retour de consentement sans state lisible")
            return RedirectResponse(_retour("error"), status_code=302)
        sub, org_id, return_app = parsed
        if erreur or not code:
            logger.info("meta_ads : consentement non abouti (sub=%s, motif=%s)",
                        sub, erreur or "code absent")
            return RedirectResponse(
                _retour("forbidden" if erreur == _REFUS else "error",
                        return_app, org_id), status_code=302)

        def _echanger_et_ranger() -> dict:
            grant = ads_auth._coeur().connect(
                ads_auth.app(), code,
                oauth_flow.redirect_uri(ads_auth._CALLBACK_PATH))
            return ads_auth.persist_grant(sub, org_id, grant)

        try:
            # DB + HTTP synchrones hors de la boucle (oto-backend#867).
            await run_in_threadpool(_echanger_et_ranger)
        except Exception:
            # On journalise le traceback, jamais le `code` ni le jeton.
            logger.exception("meta_ads : retour de consentement en échec "
                             "(sub=%s org=%s)", sub, org_id)
            return RedirectResponse(_retour("error", return_app, org_id),
                                    status_code=302)
        return RedirectResponse(_retour("connected", return_app, org_id),
                                status_code=302)

    return [Route(ads_auth._CALLBACK_PATH, callback, methods=["GET"])]
