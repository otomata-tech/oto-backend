"""Routes REST OAuth Salesforce — live "Connect" flow replacing the manual
Postman-style refresh-token acquisition (see salesforce_oauth.py's module
docstring for the per-customer-Connected-App architecture this works around).

Structure mirrors api/folk.py / api/atlassian.py:
- `GET /api/salesforce/oauth/callback` (no auth, Salesforce redirects) → exchange + persist

Le `/start` n'est PAS ici : c'est une capacité (`capabilities/salesforce_connect.py`,
ADR 0042 §Convergence des surfaces) qui en dérive les faces MCP et REST depuis un seul
descripteur. Seul le callback reste une route écrite à la main — un fournisseur y
redirige le NAVIGATEUR, sans auth et avec un 302, ce qu'un contrat de capacité ne peut
pas exprimer.

Unlike Folk/Atlassian, there is no `/status`/`DELETE` here yet — the
existing generic `/api/settings/api-keys/salesforce` GET/DELETE already covers
status/disconnect for this connector (it's still `secret_kind="fields"`,
`secret_kind="fields"` — client_id/client_secret/login_url are
pasted through the normal form, only refresh_token comes from this flow now,
not pasted at all anymore).

`scope` (`?scope=member|org|group`) selects which credential row `/start`
reads from and `/callback` writes to — mirrors the same three levels the
static-fields form already supports via `/api/settings/api-keys/salesforce`,
`PUT /api/orgs/{id}/secrets/salesforce`, and `PUT /api/groups/{id}/secrets/salesforce`.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from ..auth import salesforce as salesforce_oauth

logger = logging.getLogger(__name__)

AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
) -> list[Route]:

    def _retour(etat: str, return_app: str = "", org_id: int | None = None) -> str:
        """Où renvoyer le navigateur après le consentement : sur la fiche du
        connecteur, dépliée. `connector=` est le deep-link lu par le dashboard —
        sans lui on retombe sur la liste, et il faut retrouver la ligne à la main.

        `return_app`/`org_id` : le FRONT qui a demandé la connexion (`""` =
        historique, dégrade vers `OTO_APP_URL`/oto-dashboard) — `oauth_flow.return_url`
        porte la résolution base+chemin, cf. son docstring. Absents dans l'UNE
        branche où le state n'a même pas pu être lu (voir `callback` ci-dessous) :
        cas dégradé accepté, on n'a alors aucun moyen de savoir qui rappeler.

        `connect=` dit CE QUI S'EST PASSÉ. Le paramètre s'appelait `salesforce=` et
        **personne ne le lisait** : l'utilisateur revenait devant un écran muet. Vécu le
        04/08 chez un client — le consentement avait RÉUSSI (jeton posé à la
        milliseconde du callback, zéro erreur), et faute du moindre signe ils ont
        désinstallé puis réinstallé le connecteur en boucle pendant cinq heures.
        Nom générique : une clé nommée d'après un connecteur obligerait chaque surface
        à en connaître le nom, exactement ce qu'on retire partout ailleurs.

        Salesforce EST la forme cible (oto-backend#670) : `connector_return_url`
        (le fabricant partagé) ne fait ici que ce que cette fonction composait déjà
        à la main — aucun changement de comportement, salesforce n'a rien à doubler."""
        from ..auth import flow as oauth_flow
        return oauth_flow.connector_return_url(
            return_app, "salesforce", etat, org=org_id)

    async def callback(request: Request) -> Response:
        # Salesforce redirige ici (pas d'auth Logto) — l'identité + le scope
        # viennent du state signé (voir salesforce_oauth.make_state).
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        parsed = salesforce_oauth.verify_state(state) if state else None
        if not code or not parsed:
            # State absent/expiré/altéré : on n'a NI org_id NI return_app (ils vivent
            # DANS ce state qu'on vient d'échouer à lire) — dégradation acceptée vers
            # le défaut oto-dashboard, seul cas où ce module ne peut pas faire mieux.
            return RedirectResponse(_retour("error"), status_code=302)
        sub, org_id, scope, verifier_pkce, group_id, return_app = parsed
        # RE-GARDE du droit d'écrire au scope demandé. `build_auth_url` l'a vérifié
        # au /start, mais le state vit 10 min : entre le clic et le retour, l'auteur
        # a pu perdre son rôle. Parti pris maison (ADR 0038, ce qui a fermé #108) :
        # une autorisation se re-vérifie à la RÉSOLUTION, pas seulement à la pose.
        from .. import roles
        allowed = True
        if scope == "org":
            allowed = roles.is_org_admin(sub, org_id)
        elif scope == "group":
            allowed = roles.can_admin_group(sub, group_id)
        if not allowed:
            logger.warning("salesforce callback refusé : %s n'est plus admin du scope "
                           "%s (org=%s group=%s)", sub, scope, org_id, group_id)
            return RedirectResponse(_retour("forbidden", return_app, org_id), status_code=302)
        try:
            fields = salesforce_oauth.read_saved_fields(sub, org_id, scope, group_id)
            if not fields:
                raise RuntimeError("Credential introuvable au retour de Salesforce.")
            tokens = salesforce_oauth.exchange_code(
                code,
                client_id=fields["client_id"],
                client_secret=fields["client_secret"],
                login_url=fields["login_url"],
                verifier=verifier_pkce,
            )
            result = await salesforce_oauth.persist_token(sub, org_id, scope, tokens, group_id)
        except Exception:
            # Le client ne voit qu'un `?salesforce=error` : sans trace ici, un échec de
            # connexion est INDIAGNOSTICABLE (Sentry ne voit rien, l'exception est
            # avalée). On journalise le traceback, jamais le `code` ni les tokens.
            logger.exception("salesforce oauth callback en échec (sub=%s scope=%s org=%s)",
                             sub, scope, org_id)
            return RedirectResponse(_retour("error", return_app, org_id), status_code=302)
        # On revient sur LA FICHE du connecteur, pas sur l'accueil. Le retour
        # atterrissait sur `/`, donc sur la vue d'ensemble — un écran où Salesforce
        # n'apparaît nulle part : l'utilisateur venait d'autoriser et se retrouvait
        # devant rien, sans moyen de constater le résultat de son geste.
        # `connected_unverified` a disparu avec la sonde post-écriture : cet état
        # n'existe plus, et le mot « unverified » inquiétait pour une connexion saine.
        del result  # la pose EST le résultat ; plus de verdict à transporter
        return RedirectResponse(_retour("connected", return_app, org_id), status_code=302)

    return [Route("/api/salesforce/oauth/callback", callback, methods=["GET"])]
