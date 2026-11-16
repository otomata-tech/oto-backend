"""L'inscription dynamique des clients MCP (RFC 7591) devant Logto, qui n'en a pas.

Pour les clients qui ne portent pas CIMD (Le Chat) : le client lit la description
d'autorisation de Logto, augmentée d'un `registration_endpoint`, et s'y inscrit ;
l'inscription crée (ou retrouve) une application Logto. Les clients qui portent CIMD
l'utilisent toujours (la description de Logto l'annonce, rien ici ne la retire).

**L'émetteur ne change pas** (RFC 9207 : `iss` reste celui de Logto). Les trois chemins
sont servis SOUS le domaine de l'émetteur : le proxy de ce domaine les mène au backend,
tout le reste va à Logto. Le PRM (`auth/decouverte.py`) annonce toujours Logto lui-même :

    GET  /.well-known/oauth-authorization-server/oidc  — description de Logto + registration_endpoint (RFC 8414)
    GET  /.well-known/openid-configuration/oidc         — idem, forme OIDC avec insertion de chemin
    POST /oauth/register                                — l'inscription

La description est relue chez Logto à sa forme d'origine
(`<LOGTO_ENDPOINT>/oidc/.well-known/openid-configuration`, que le proxy laisse à Logto) et
rendue TELLE QUELLE. Une panne de Logto ou de son API de gestion lève : jamais de
description ni d'inscription de repli.

**Jamais une inscription ouverte** : les adresses de retour sont en liste fermée
(`RETOURS_ADMIS`), une demande dont une seule adresse est hors liste est refusée. Une
demande de même type et mêmes adresses de retour retrouve la même application (le nom,
libre, n'entre pas dans la clé) ; seules les adresses admises par préfixe (ChatGPT,
Gemini) peuvent multiplier les applications, d'où un plafond PAR préfixe
(`PREFIXES_ADMIS`).

**Gemini (gemini.google.com, connecteur MCP « + Custom »)** ne s'inscrit pas lui-même :
il affiche un rappel propre à l'utilisateur
(`https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-<id Google>-<hôte>`)
et demande un client_id / client_secret. On les obtient en postant ce rappel ici
(`POST /oauth/register {"client_name": "Gemini", "redirect_uris": [<rappel>]}`) ; le
rappel ne mène qu'à Google, l'admettre n'ouvre aucune redirection vers un tiers.

**Grok (grok.com, connecteur « Custom »)** s'inscrit lui-même ici, avec un rappel FIXE
(`https://grok.com/connectors-oauth-exchange-code/`) : égalité exacte, pas de plafond.

La logique (`lire_demande`, `inscrire`) ne dépend que d'une `Gestion` injectée ;
`GestionLogto` la branche sur l'API de gestion de NOTRE annuaire (`auth/annuaire.py`).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..json_body import InvalidJsonBody, read_json_body
from . import annuaire

_log = logging.getLogger("oto_mcp.inscription_dynamique")
_TIMEOUT = 15

#: Les adresses de retour admises par égalité exacte.
RETOURS_ADMIS: tuple[str, ...] = (
    "https://callback.mistral.ai/v1/integrations_auth/oauth2_callback",
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    # Grok (grok.com, Connectors › New Connector › Custom) : rappel fixe, slash final
    # compris — la seule forme observée à l'inscription.
    "https://grok.com/connectors-oauth-exchange-code/",
)

METHODES = ("client_secret_basic", "client_secret_post", "none")
_GRANTS = frozenset({"authorization_code", "refresh_token"})

#: Le préfixe du nom des applications créées ici : il les distingue de toutes les autres
#: applications de l'annuaire, et borne ce que la reprise et le plafond regardent.
PREFIXE_DCR = "DCR · "

#: Plafond des applications inscrites à adresse de retour VARIABLE (admise par préfixe) :
#: seules elles peuvent se multiplier, l'inscription n'étant pas authentifiée. Les
#: adresses exactes ne sont jamais plafonnées : un plafond atteint ne bloque pas Le Chat.
#: Au-delà, refus journalisé ; faire le ménage des applications « DCR · » dans Logto.
PLAFOND_VARIABLES = 30
#: Gemini : un rappel PAR utilisateur Google, donc une application par utilisateur.
PLAFOND_GEMINI = 200

#: Les adresses admises par préfixe, chacune avec SON plafond (Gemini n'épuise pas
#: ChatGPT). Après le préfixe : UN segment `[A-Za-z0-9_-]` borné, rien d'autre — ni `/`,
#: ni `?`, ni `..` — jamais un `startswith` sans borne.
PREFIXES_ADMIS: dict[str, int] = {
    "https://chatgpt.com/connector/oauth/": PLAFOND_VARIABLES,
    "https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-": PLAFOND_GEMINI,
}
_SUFFIXE = re.compile(r"[A-Za-z0-9_-]{1,200}")

#: Part du plafond à partir de laquelle chaque nouvelle inscription d'un préfixe ALERTE
#: (journal + Sentry, `_alerte_plafond`) : le ménage se fait avant le premier refus.
PART_ALERTE = 0.8


class RefusDcr(Exception):
    """Un refus au sens de la RFC 7591 §3.2.2 : 400, `error` et `error_description`."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _prefixe_de(uri: str) -> str | None:
    """Le préfixe admis dont `uri` est la forme variable (préfixe + un segment), sinon None."""
    for prefixe in PREFIXES_ADMIS:
        if uri.startswith(prefixe) and _SUFFIXE.fullmatch(uri[len(prefixe):]):
            return prefixe
    return None


def retour_admis(uri: str) -> bool:
    return uri in RETOURS_ADMIS or _prefixe_de(uri) is not None


def _prefixe_inscrit(uri: str) -> str | None:
    """Le préfixe dont relève une adresse DÉJÀ inscrite, par `startswith` seul : une
    application créée avant la borne du segment (ChatGPT, adresse à plusieurs segments
    ou à requête) reste comptée dans le plafond de son préfixe. Le comptage ne prend pas
    la borne de l'admission (`_prefixe_de`), sinon ces applications sortiraient du
    plafond sans avoir quitté l'annuaire."""
    return next((p for p in PREFIXES_ADMIS if uri.startswith(p)), None)


def _alerte_plafond(prefixe: str, inscrites: int, plafond: int) -> None:
    """Le plafond d'un préfixe approche : au journal ET au suivi d'erreurs (Sentry), le
    canal d'exploitation des autres anomalies hors requête (`tenancy._refus`,
    `guide_store._alerte`, `medecin_agents._alerte_exploitation`). Le texte ne porte
    pas le compte : une issue par préfixe, le compte est au journal. Fail-open sur le
    suivi seulement : l'inscription est déjà faite."""
    _log.warning("DCR : %d/%d applications inscrites pour %s, faire le ménage des « %s »",
                 inscrites, plafond, prefixe, PREFIXE_DCR)
    try:
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("oto.dcr_plafond", "approche")
            sentry_sdk.capture_message(
                f"DCR : plafond de {plafond} applications bientôt atteint pour {prefixe}",
                level="warning")
    # noqa: SILENT — le suivi d'erreurs indisponible n'annule pas une inscription faite
    except Exception:
        pass


@dataclass(frozen=True)
class Demande:
    redirect_uris: tuple[str, ...]   # triées, dédoublonnées
    nom: str
    methode: str


def lire_demande(corps) -> Demande:
    """Lit et contrôle le corps de la demande ; lève `RefusDcr` sur toute valeur hors contrat."""
    if not isinstance(corps, dict):
        raise RefusDcr("invalid_client_metadata", "corps JSON attendu")
    uris = corps.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
        raise RefusDcr("invalid_redirect_uri", "redirect_uris : liste d'adresses requise")
    refusees = [u for u in uris if not retour_admis(u)]
    if refusees:
        raise RefusDcr("invalid_redirect_uri",
                       f"adresse de retour non admise : {', '.join(refusees)}")
    # RFC 7591 §2 : `client_secret_basic` quand la méthode n'est pas dite.
    methode = corps.get("token_endpoint_auth_method", "client_secret_basic")
    if methode not in METHODES:
        raise RefusDcr("invalid_client_metadata",
                       f"token_endpoint_auth_method non pris en charge : {methode}")
    grants = corps.get("grant_types", ["authorization_code"])
    if not isinstance(grants, list) or any(g not in _GRANTS for g in grants):
        raise RefusDcr("invalid_client_metadata",
                       "grant_types : authorization_code et refresh_token seulement")
    reponses = corps.get("response_types", ["code"])
    if not isinstance(reponses, list) or any(r != "code" for r in reponses):
        raise RefusDcr("invalid_client_metadata", "response_types : code seulement")
    nom = corps.get("client_name")
    nom = nom.strip() if isinstance(nom, str) and nom.strip() else urlsplit(uris[0]).netloc
    return Demande(tuple(sorted(set(uris))), nom[:64], methode)


def type_logto(methode: str) -> str:
    """Confidentiel (`Traditional`) avec secret, public (`Native`) sans ; jamais SPA."""
    return "Native" if methode == "none" else "Traditional"


class Gestion(Protocol):
    def applications(self) -> list[dict]: ...
    def creer(self, *, name: str, type: str, description: str,
              redirect_uris: list[str]) -> dict: ...
    def secret(self, application_id: str) -> str: ...


def _retours_de(application: dict) -> list[str]:
    return list((application.get("oidcClientMetadata") or {}).get("redirectUris") or [])


def inscrire(gestion: Gestion, demande: Demande, maintenant: float | None = None) -> dict:
    """Inscrit le client : l'application `DCR · …` de même type et mêmes adresses de
    retour, sinon une nouvelle. Rend la réponse RFC 7591 §3.2.1."""
    type_ = type_logto(demande.methode)
    inscrites = [a for a in gestion.applications()
                 if str(a.get("name", "")).startswith(PREFIXE_DCR)]
    cle = sorted(demande.redirect_uris)
    existante = next((a for a in inscrites if a.get("type") == type_
                      and sorted(set(_retours_de(a))) == cle), None)
    prefixes = {p for u in demande.redirect_uris if (p := _prefixe_de(u))}
    proches: list[tuple[str, int, int]] = []
    for prefixe in sorted(prefixes) if existante is None else ():
        plafond = PREFIXES_ADMIS[prefixe]
        deja = sum(1 for a in inscrites
                   if any(_prefixe_inscrit(u) == prefixe for u in _retours_de(a)))
        if deja >= plafond:
            raise RefusDcr(
                "invalid_client_metadata",
                f"plafond de {plafond} applications inscrites atteint pour {prefixe}")
        if deja + 1 >= plafond * PART_ALERTE:
            proches.append((prefixe, deja + 1, plafond))
    application = existante or gestion.creer(
        name=f"{PREFIXE_DCR}{demande.nom}"[:64],
        type=type_,
        description=("Inscrite par DCR (RFC 7591) depuis "
                     f"{urlsplit(demande.redirect_uris[0]).netloc}"),
        redirect_uris=list(demande.redirect_uris),
    )
    for prefixe, compte, plafond in proches:
        _alerte_plafond(prefixe, compte, plafond)
    client: dict = {"client_id": application["id"]}
    if type_ == "Traditional":
        client["client_secret"] = gestion.secret(application["id"])
        client["client_secret_expires_at"] = 0
    client.update({
        "client_id_issued_at": int(time.time() if maintenant is None else maintenant),
        "client_name": demande.nom,
        "redirect_uris": list(demande.redirect_uris),
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": demande.methode,
    })
    return client


# ── Le branchement sur Logto ─────────────────────────────────────────────────

def registration_endpoint() -> str:
    """`<origine de l'émetteur>/oauth/register` : servi sous le domaine de Logto, que
    son proxy mène ici."""
    p = urlsplit(annuaire._logto_base())
    return f"{p.scheme}://{p.netloc}/oauth/register"


def lire_description() -> dict:
    """La description d'autorisation de Logto, telle quelle, plus `registration_endpoint`.
    Logto injoignable, non-200 ou illisible : lève — jamais de description de repli."""
    import requests
    r = requests.get(f"{annuaire._logto_base()}/oidc/.well-known/openid-configuration",
                     headers={"User-Agent": annuaire._UA}, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"Logto : description d'autorisation illisible ({r.status_code})")
    description = r.json()
    if not isinstance(description, dict):
        raise RuntimeError("Logto : description d'autorisation qui n'est pas un objet")
    return {**description, "registration_endpoint": registration_endpoint()}


class GestionLogto:
    """`Gestion` sur l'API de gestion de NOTRE annuaire (le M2M de `auth/annuaire.py`).
    Toute réponse en erreur lève (`raise_for_status`) : aucune inscription de repli."""

    def _entetes(self) -> dict:
        return {"Authorization": f"Bearer {annuaire._mgmt_token()}",
                "User-Agent": annuaire._UA, "Content-Type": "application/json"}

    def applications(self) -> list[dict]:
        import requests
        tout: list[dict] = []
        page = 1
        while True:
            r = requests.get(f"{annuaire._logto_base()}/api/applications",
                             params={"page": page, "page_size": 100},
                             headers=self._entetes(), timeout=_TIMEOUT)
            r.raise_for_status()
            lot = r.json()
            tout.extend(lot)
            if len(lot) < 100:
                return tout
            page += 1

    def creer(self, *, name: str, type: str, description: str,
              redirect_uris: list[str]) -> dict:
        import requests
        r = requests.post(
            f"{annuaire._logto_base()}/api/applications",
            json={"name": name, "type": type, "description": description,
                  "oidcClientMetadata": {"redirectUris": redirect_uris,
                                         "postLogoutRedirectUris": []}},
            headers=self._entetes(), timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def secret(self, application_id: str) -> str:
        """Logto ≥ 1.44 ne rend plus le secret à la création : il se lit à part."""
        import requests
        r = requests.get(f"{annuaire._logto_base()}/api/applications/{application_id}/secrets",
                         headers=self._entetes(), timeout=_TIMEOUT)
        r.raise_for_status()
        secrets = r.json()
        if not secrets or not secrets[0].get("value"):
            raise RuntimeError(f"Logto : l'application {application_id} n'a aucun secret")
        return secrets[0]["value"]


def make_routes() -> list[Route]:
    async def description(request: Request):
        return JSONResponse(await run_in_threadpool(lire_description))

    async def register(request: Request):
        try:
            try:
                corps = await read_json_body(request)
            except InvalidJsonBody as e:
                raise RefusDcr("invalid_client_metadata", e.detail)
            demande = lire_demande(corps)
            client = await run_in_threadpool(inscrire, GestionLogto(), demande)
        except RefusDcr as e:
            _log.warning("DCR : inscription refusée (%s) : %s", e.code, e.message)
            return JSONResponse({"error": e.code, "error_description": e.message},
                                status_code=400)
        # Jamais le secret : l'identifiant et les adresses seulement.
        _log.info("DCR : client %s inscrit (%s) pour %s", client["client_id"],
                  client["token_endpoint_auth_method"], ", ".join(client["redirect_uris"]))
        return JSONResponse(client, status_code=201, headers={"Cache-Control": "no-store"})

    return [
        Route("/.well-known/oauth-authorization-server/oidc", description, methods=["GET"]),
        Route("/.well-known/openid-configuration/oidc", description, methods=["GET"]),
        Route("/oauth/register", register, methods=["POST"]),
    ]
