"""Routes REST OAuth Folk — fédération du MCP officiel de Folk per-user (#85).

Flow web (calqué sur api/atlassian.py) — routes ancrées sur le NOM du
connecteur `folkmcp` (le widget fédéré du dashboard appelle `/api/<name>/oauth/*`,
name = `folkmcp`, comme atlassian) :
- `GET    /api/folkmcp/oauth/callback` (no auth, Folk redirige) → exchange + persist
- `GET    /api/folkmcp/oauth/status`   (auth) → {connected, set_at}
- `DELETE /api/folkmcp/oauth`          (auth) → déconnecte

`/start` (capacité `me.federation.folkmcp.start`, dans `capabilities/federated_oauth.py`,
pas ici) a été RETIRÉ le 2026-09-04 (oto-dashboard#125, mesuré à 0 appel/30j) : le
consentement démarre désormais par le chemin fixe générique
(`POST /api/me/connectors/folkmcp/connect`).

Le token per-user est stocké dans le coffre (connector='folkmcp') ; le proxy de
tools/mount.py l'injecte par requête (access.resolve_mount_token → refresh). Ne
concerne QUE le connecteur fédéré `folkmcp` — le connecteur natif `folk` (clé API)
n'a pas d'OAuth.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from ..auth import folk as folk_oauth

AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
) -> list[Route]:

    def _retour(statut: str, sub: "str | None" = None) -> str:
        """Où renvoyer le navigateur après le consentement Folk.

        Le retour était codé sur le dashboard oto : un utilisateur d'un front
        partenaire finissait son consentement chez un produit qu'il n'a
        pas. `links.link_for` résout le patron du TENANT depuis le `sub` — que le
        callback tient déjà, relu du state signé — donc aucune modification du
        front n'est nécessaire ici.

        Convention unique de retour OAuth (oto-backend#670) : `connect=<statut>`
        rejoint le `connector=folk` déjà servi, via `oauth_flow.avec_connect` —
        pur ajout, `connector=` ne bouge pas d'un octet.

        Pas de patron chez le tenant ⟹ `None` ⟹ repli sur le fabricant partagé
        (`connector_return_url`, défaut oto-dashboard `/connectors`). Avant ce lot,
        ce repli était une f-string À ACCOLADES DOUBLÉES (`f"{{_app_url()}}/?…"`) —
        une chaîne LITTÉRALE, jamais une URL : un statut qui n'a jamais eu de
        lecteur n'a rien à préserver, donc rien à doubler ici non plus."""
        from .. import links
        from ..auth import flow as oauth_flow
        cible = links.link_for("connector_return", sub=sub, connector="folk") if sub else None
        if cible:
            return oauth_flow.avec_connect(cible, statut)
        return oauth_flow.connector_return_url(None, "folk", statut)



    async def callback(request: Request) -> Response:
        # Folk (Stytch) redirige ici (pas d'auth Logto) ; l'identité vient du state signé.
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        parsed = folk_oauth.verify_state(state) if state else None
        if not code or not parsed:
            return RedirectResponse(_retour("error"), status_code=302)
        sub, verifier_pkce = parsed
        try:
            tokens = folk_oauth.exchange_code(code, verifier_pkce)
            folk_oauth.persist_token(sub, tokens)
        # noqa: SILENT — un callback renvoie la personne CHEZ ELLE, sans détailler l'échec
        except Exception:
            # `sub` est connu ici (relu du state) : même un échec renvoie la personne
            # chez ELLE, pas chez nous.
            return RedirectResponse(_retour("error", sub), status_code=302)
        return RedirectResponse(_retour("connected", sub), status_code=302)



    return [
        Route("/api/folkmcp/oauth/callback", callback, methods=["GET"]),
    ]
