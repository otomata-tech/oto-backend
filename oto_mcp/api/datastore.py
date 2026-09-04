"""LA route qui reste écrite à la main de ce module : le CALLBACK Google OAuth.

Le nom du fichier est un vestige — il est le point d'accroche d'`api/routes.py`, et
trois vagues de migration l'ont vidé de tout le reste :

- **2026-08-12 (#302)** : les 17 routes du datastore sont devenues des capacités
  (`capabilities/datastore/*.py`) ;
- **2026-08-27** : les VERBES Google OAuth (`start`, `status`, `DELETE`, `default`) →
  `capabilities/federated_oauth.py` ;
- **2026-08-27** : les JETONS API (`/api/me/tokens*`) → `capabilities/api_tokens.py`,
  rendu possible par le cran `RestBinding.allow_api_token` (un jeton ne fabrique pas de
  jeton — c'est ce qui les retenait ici).

**Pourquoi le callback ne migre pas, et ne migrera pas.** Google y redirige le
NAVIGATEUR de l'utilisateur : pas d'en-tête d'auth (l'identité vient du `state`
HMAC-signé), et la réponse est une **302** vers la page connecteurs, pas du JSON. Or
`_rest_adapter` authentifie toujours et répond toujours en JSON. Il est hors du moule
par construction, et classé par NATURE comme les autres callbacks OAuth.

**oto-backend#670** : le succès portait `?google=connected` en dur, et l'échec ne
redirigeait pas du tout (JSON brut 400/502/504) — deux formes hors de la convention
unique suivie par les quatre autres connecteurs (`?connector=<nom>&connect=<etat>`).
Le succès DOUBLE désormais les deux formes (`?google=connected` reste, le dashboard
le lit) ; l'échec REDIRIGE avec `connect=error` — pur ajout, rien à doubler puisque
rien n'existait à cette place. Le diagnostic (state illisible, timeout, échange
refusé) va au journal, plus jamais dans le corps de la réponse.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from ..auth import google as google_oauth

logger = logging.getLogger(__name__)


# Type alias for the auth helper passed in from api_routes.
AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]

# oto-backend#867 lot 2 — délai défendable pour l'échange OAuth Google hors boucle
# (exchange_code + persist_token→_fetch_email, deux appels HTTP synchrones 15s
# chacun ; voir google.py::_client_for_user_async pour la même justification).
_OAUTH_EXCHANGE_TIMEOUT_S = 30


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
    """Construit ces routes.

    Les helpers `authenticate`/`json_response`/`json_error`/`cors_headers`/
    `options_handler` sont passés depuis `api/routes.py` pour partager les
    primitives (auth Logto + token, CORS).
    """

    # --- Google OAuth ----------------------------------------------------

    def _retour(etat: str, *, legacy: bool = False) -> str:
        """Où renvoyer le navigateur après le consentement Google.

        Convention unique de retour OAuth (oto-backend#670) : le suffixe vient du
        fabricant partagé `oauth_flow.connector_return_suffix`. `legacy=True`
        double l'ancienne forme `?google=connected` — encore lue par le dashboard
        aujourd'hui — le temps du préavis (`deprecations.dans_le_preavis_retour_oauth`).

        Les branches d'ÉCHEC (`legacy=False`, le défaut) ne redirigeaient PAS du
        tout avant ce lot — un JSON brut 400/502/504 à la place. Rien à doubler :
        un statut qui n'a jamais existé n'a pas de lecteur à préserver, donc PUR
        AJOUT ici, aligné sur les quatre autres connecteurs."""
        from ..auth import flow as oauth_flow
        suffix = oauth_flow.connector_return_suffix(
            "google", etat, legacy=("google", etat) if legacy else None)
        return f"{_app_url()}/console/connectors{suffix}"

    async def google_oauth_callback(request: Request) -> Response:
        # Pas d'auth Logto — Google redirige depuis le navigateur user.
        # Validation via le `state` HMAC-signé.
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        if not code or not state:
            logger.warning("google oauth callback: code ou state absent")
            return RedirectResponse(url=_retour("error"), status_code=302)
        parsed = google_oauth.verify_state(state)
        if not parsed:
            logger.warning("google oauth callback: state illisible/expiré")
            return RedirectResponse(url=_retour("error"), status_code=302)
        sub, org_id = parsed

        def _finish() -> None:
            tokens = google_oauth.exchange_code(code)
            google_oauth.persist_token(sub, org_id, tokens)

        try:
            # DB + HTTP sync → hors event loop (#867), même patron qu'api/zoho.py.
            await asyncio.wait_for(run_in_threadpool(_finish),
                                   timeout=_OAUTH_EXCHANGE_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning("google oauth callback: échange sans réponse au-delà "
                           "de %ss (sub=%s org=%s)", _OAUTH_EXCHANGE_TIMEOUT_S,
                           sub, org_id)
            return RedirectResponse(url=_retour("error"), status_code=302)
        except Exception:
            logger.exception("google oauth callback en échec (sub=%s org=%s)",
                             sub, org_id)
            return RedirectResponse(url=_retour("error"), status_code=302)
        # Retour vers la page connecteurs (où vit la config Google, ADR 0024 B2).
        # `datastore` n'est plus Google Sheets (ADR 0016, PG natif) → ex-signal
        # `?datastore=connected` retiré.
        return RedirectResponse(url=_retour("connected", legacy=True), status_code=302)




    # --- API tokens (CLI auth) -------------------------------------------

    # --- Jetons API : gestion réservée à une SESSION INTERACTIVE ---------------
    # `allow_api_token=False` sur les trois : un jeton `oto_` ne peut ni lister, ni
    # créer, ni révoquer de jeton. Sinon une fuite est auto-entretenue (l'attaquant
    # s'émet un second jeton non-expirant avant qu'on révoque le premier) et peut
    # révoquer les jetons légitimes. Émettre un jeton reste donc un acte humain,
    # ce qui est exactement ce qu'on veut d'un jeton confié à un tiers.




    # --- Datastore (PG natif, ADR 0016) ----------------------------------

    # Lister / créer / supprimer / renommer un tableau, et son deep-link, sont des
    # CAPACITÉS (`capabilities/datastore/namespaces.py`, #302) : mêmes chemins, mêmes
    # réponses, mais entrée ET sortie déclarées — donc décrites dans
    # `/api/openapi.json`, donc générables chez un intégrateur.

    # Les LIGNES sont des CAPACITÉS (`capabilities/datastore/rows.py`, #302) : page,
    # fiche, ajout, modification, suppression, file de travail et agrégat. Mêmes
    # chemins, mêmes réponses, mais entrée ET sortie déclarées — les deux corps LIBRES
    # (ajouter/modifier : les colonnes du tableau) le sont explicitement, par
    # `RestBinding.body_field`.

    # Le SCHÉMA (pose) et le PARTAGE sont des CAPACITÉS (#302) :
    # `capabilities/datastore/schema.py` (la lecture y vivait déjà) et
    # `capabilities/datastore/sharing.py`. Le corps du DELETE de partage — forme
    # historique du client `oto-core` — est déclaré par `RestBinding.reads_body`.

    # Le TRANSFERT de propriété d'un datastore passe par la capacité UNIQUE `oto_resource`
    # (op=transfer, resource_type='datastore_namespace' — même seam `ownership` + garde-fou
    # anti-lockout + cibles user/org/GROUPE). L'ancien endpoint bespoke `ds_transfer` a été
    # retiré (2026-07-24) : il dupliquait la résolution de cible et court-circuitait la
    # confirmation de perte de contrôle. Le front vise `/api/resources` par l'id du namespace.

    return [
        # Google OAuth
        Route("/api/google/oauth/callback", google_oauth_callback, methods=["GET"]),
        # API tokens
    ]
