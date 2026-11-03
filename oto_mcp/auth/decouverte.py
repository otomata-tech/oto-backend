"""La découverte OAuth d'un client MCP : la métadonnée de ressource protégée (RFC 9728),
sensible au host. La description d'autorisation, elle, reste celle de Logto ; seule
l'inscription des clients sans CIMD l'augmente d'un `registration_endpoint`, sous le
domaine de l'émetteur (`auth/inscription_dynamique.py`).

Le serveur d'autorisation annoncé est l'émetteur Logto LUI-MÊME — le nôtre
(`LOGTO_ENDPOINT` + `/oidc`), ou celui du tenant qui réclame le host. Le client lit
la métadonnée d'autorisation chez Logto, s'y inscrit par CIMD (son `client_id` est
l'URL de son document de métadonnées) et y obtient ses jetons ; nous ne faisons que
les vérifier (`docs/auth-logto.md`).

⚠️ **L'émetteur annoncé est celui qui SIGNE, jamais une autre adresse du même Logto.**
Logto sert ses endpoints sur chaque domaine qui l'atteint, mais son `issuer` ne suit pas
le host : annoncer un autre domaine que l'émetteur fait lire au client une métadonnée
dont l'`issuer` diffère de celui qu'il a demandé (RFC 8414 §3.3), et un retour
d'autorisation dont l'`iss` diffère (RFC 9207) — un client strict refuse les deux.
"""
from __future__ import annotations

import os

from pydantic import AnyHttpUrl
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.routing import Route

# Annoncés dans le PRM. Sans eux le PRM expose `scopes_supported: []` : un client qui
# DÉRIVE le `scope` de sa demande d'autorisation du PRM n'en envoie aucun, et Logto
# refuse l'autorisation (vécu 2026-06-25).
SCOPES = ["openid", "profile", "email", "offline_access"]
RESOURCE_NAME = "oto MCP"


def emetteur_primaire() -> str:
    """L'émetteur de NOTRE annuaire : celui qui signe nos jetons, et que le
    vérificateur attend (`server._build_verifier`)."""
    return os.environ["LOGTO_ENDPOINT"].rstrip("/") + "/oidc"


def _prm_handler(resource_url: str, as_url: str, resource_name: str = RESOURCE_NAME):
    from mcp.server.auth.handlers.metadata import ProtectedResourceMetadataHandler
    from mcp.shared.auth import ProtectedResourceMetadata
    md = ProtectedResourceMetadata(
        resource=AnyHttpUrl(resource_url),
        authorization_servers=[AnyHttpUrl(as_url)],
        scopes_supported=SCOPES,
        resource_name=resource_name,
    )
    return ProtectedResourceMetadataHandler(md)


def tenant_discovery_for_host(host: str):
    """`(as_url, resource_name)` à annoncer pour ce host, ou **None** s'il n'est
    réclamé par aucun tenant.

    L'`as_url` est l'émetteur du TENANT : lui annoncer le nôtre enverrait son
    utilisateur natif sur NOTRE écran de connexion, où il n'a pas de compte
    (oto-private#83). Partagé par le PRM et le 401 (`server.TenantChallengeMiddleware`)
    pour qu'ils ne puissent pas diverger."""
    from .. import tenancy
    entry = tenancy.current().for_host(host)
    if entry is None:
        return None
    return entry.issuer, (entry.name or entry.slug)


def make_routes(public_url: str) -> list[Route]:
    public_url = public_url.rstrip("/")

    async def prm(request: Request):
        host = (request.headers.get("host") or "").split(":")[0].strip().lower()
        candidate = f"https://{host}/mcp"
        from .. import subdomain_project
        from ..config import mcp_audience_alt_hosts
        tenant = tenant_discovery_for_host(host)
        as_url, resource_name = tenant if tenant else (emetteur_primaire(), RESOURCE_NAME)
        # La `resource` = l'audience que le client demandera à Logto. Host = domaine
        # canonique SECONDAIRE (ex. mcp.oto.cx) → ce host ; sinon host d'un TENANT
        # déclaré ; sinon sous-domaine d'un projet org publié (sinon claude.ai reçoit un
        # jeton opaque, blocage #44) ; sinon canonique.
        # `valid_org_audience` LIT LA BASE : route publique sans jeton, donc jamais sur
        # la boucle — et seulement quand les deux crans gratuits n'ont pas tranché.
        connu = host in mcp_audience_alt_hosts() or tenant is not None
        resource_url = candidate if (
            connu or await run_in_threadpool(subdomain_project.valid_org_audience, candidate)
        ) else f"{public_url}/mcp"
        return await _prm_handler(resource_url, as_url, resource_name).handle(request)

    return [
        # Les deux variantes de chemin que tentent les clients MCP (RFC 9728 §3.1 :
        # racine, et suffixée par le chemin de la ressource). Elles masquent celles de
        # fastmcp (insérées avant → priorité).
        Route("/.well-known/oauth-protected-resource", prm, methods=["GET", "OPTIONS"]),
        Route("/.well-known/oauth-protected-resource/mcp", prm, methods=["GET", "OPTIONS"]),
    ]
