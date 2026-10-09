"""Le relais d'autorisation : l'émetteur ANNONCÉ devient celui qui ESTAMPILLE la réponse
(RFC 9207). Contrat complet : docs/auth-logto.md §relais.

**Le défaut (08/09/2026).** La façade s'annonce serveur d'autorisation (`issuer` = le host),
mais la réponse sortait de Logto, estampillée de SON émetteur : le SDK MCP Python 2.0 compare
`iss` à l'`issuer` découvert (§2.4, dès que `iss` est PRÉSENT) et refusait le flux avant tout
jeton.

**Le relais**, sur un host DÉCLARÉ : l'autorisation ne réécrit que `redirect_uri` (→ le rappel
de la façade) et `state` (scellé, `relay_seals`) ; le retour rend le client à son rappel avec
`iss` = l'`issuer` servi ; l'échange de jeton remet le rappel de la façade pour un code
MARQUÉ. Logto émet et signe comme avant ; rien n'est stocké.

**Il n'élargit rien de ce que Logto aurait accepté** : rappel enregistré À L'OCTET PRÈS sur
l'application (relu, cache court), annuaire administré, échange pour le seul client de
l'application sans authentification client relayée. **Il ne se contourne pas en silence** :
ce qu'il ne relaie pas reçoit une erreur NOMMÉE ; un host déclaré sans secret empêche le
démarrage. `/oauth/authorize` (oto#202) ne relaie jamais, et aucun drapeau RFC 9207 n'est
annoncé. ⚠️ Retirer une déclaration rend des erreurs nommées aux clients qui ont lu la
métadonnée du relais, jusqu'à leur prochaine découverte.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qsl, quote, unquote_plus, urlencode, urlparse, urlsplit

import httpx
from pydantic import AnyHttpUrl
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import facade
from .authorize_consent import redirection
from .relay_seals import (CODE_MARQUE, lire_code, marquer_code, ouvrir_etat, sceller_etat,
                          secret)

_log = logging.getLogger("oto_mcp.oauth_facade")

AUTHORIZE_PATH = "/oauth/relay/authorize"
CALLBACK_PATH = "/oauth/callback"
TOKEN_PATH = "/oauth/token"
HOSTS_ENV = "OTO_MCP_OAUTH_RELAY_HOSTS"
# ≥ au délai d'un client : si le relais abandonnait le premier, Logto consommerait quand
# même le code (ou ferait tourner le jeton de rafraîchissement), et le rejeu du client
# ferait RÉVOQUER toute la délégation. Jamais de nouvel essai côté relais.
_JETON_TIMEOUT = 30.0
_LECTURE_TIMEOUT = 10.0   # relire les rappels de l'application, sur le chemin d'autorisation
_RAPPELS_TTL = 60.0
_CORPS_MAX = 64 * 1024
_GRANTS = frozenset({"authorization_code", "refresh_token"})
_CLES_DE_REPONSE = frozenset({"code", "state", "iss", "error", "error_description", "error_uri"})
# Deux freins sur l'échange de jeton : un seau par IP OBSERVÉE (large à dessein — un client
# hébergé rafraîchit tous ses utilisateurs depuis quelques IP) et un plafond d'échanges en
# vol, qui garde le client HTTP partagé disponible pour les vrais rafraîchissements.
_SEAU_PAR_MIN, _SEAU_RAFALE, _SEAUX_MAX = 120.0, 60.0, 50_000
_EN_VOL_MAX = 32
# Le seul proxy dont on lit `X-Forwarded-For` : celui de la box, qui écoute en boucle locale.
_PROXIES_DE_CONFIANCE = frozenset({"127.0.0.1", "::1"})
_seaux: dict = {}
_rappels: dict = {}
_en_vol = 0


def hosts_declares() -> frozenset:
    raw = os.environ.get(HOSTS_ENV, "")
    return frozenset(h.strip().lower().rstrip(".") for h in raw.split(",") if h.strip())


def verifier_configuration() -> None:
    """Au démarrage : un host déclaré sans secret de sceau n'est PAS un relais dégradé,
    c'est une configuration incomplète — le process refuse de démarrer en la nommant."""
    if hosts_declares() and secret() is None:
        raise RuntimeError(
            f"{HOSTS_ENV} déclare {sorted(hosts_declares())} mais OTO_MCP_OAUTH_STATE_SECRET "
            "est absent : le relais d'autorisation ne peut pas sceller son état")


def relais_actif(host: str) -> bool:
    return (host or "").lower().rstrip(".") in hosts_declares() and secret() is not None


@dataclass(frozen=True)
class Cible:
    """L'annuaire servi sur un host — résolu côté serveur, jamais lu dans la requête."""
    host: str             # le host qui décide de la déclaration (celui de l'issuer)
    as_base: str
    app_id: str
    oidc_public: str      # où le NAVIGATEUR se connecte
    oidc_jeton: str       # où part l'échange de jeton, de serveur à serveur
    emetteurs: frozenset  # les `iss` que cet annuaire peut estampiller
    consentement: bool    # oto#202 : NOTRE annuaire, ou un tenant qui a DÉCLARÉ
    #                       `logto_mgmt.refresh_tokens` (opt-in, éteint par défaut)
    directory: object = None
    label: str = "?"

    @property
    def rappel(self) -> str:
        return f"{self.as_base}{CALLBACK_PATH}"

    @property
    def issuer(self) -> str:
        return str(AnyHttpUrl(self.as_base))   # la normalisation de `facade.as_metadata`


def cible_pour_host(host: str, public_url: str, claude_app_id: str) -> Cible:
    public_url = public_url.rstrip("/")
    entry = facade.tenant_for_host(host)
    if entry is None:
        d = facade._primary_directory()
        alt = os.environ.get("LOGTO_ENDPOINT_ALT", "").strip().rstrip("/")
        emetteurs = {facade._logto_issuer(), facade._logto_public_oidc()}
        emetteurs |= {f"{alt}/oidc"} if alt else set()
        # Le jeton part vers l'ORIGINE (`LOGTO_ENDPOINT`), pas le domaine public : même
        # annuaire, même jeton, sans le pare-feu applicatif devant le domaine public.
        return Cible(urlparse(public_url).hostname or "", public_url, claude_app_id,
                     facade._logto_public_oidc(), facade._logto_issuer(),
                     frozenset(e.rstrip("/") for e in emetteurs), True,
                     d if facade._credential_present(d) else None, d.label)
    d = facade.directory_for_tenant(entry)
    oidc = entry.issuer.rstrip("/")
    # `consent` (donc un jeton de rafraîchissement) : la décision du TENANT, déclarée par
    # l'administrateur de la plateforme (`entry.refresh_tokens`, éteint par défaut) — jamais
    # celle du client ni de la requête.
    return Cible(host, f"https://{host}", entry.oauth_client_id or "", oidc, oidc,
                 frozenset({oidc}), entry.refresh_tokens,
                 d if d is not None and facade._credential_present(d) else None, entry.slug)


# ── la requête d'autorisation ─────────────────────────────────────────────────

def _cle(segment: str) -> str:
    return unquote_plus(segment.split("=", 1)[0])


_REFUS_DEMANDE = {
    "request_object": "les objets `request` / `request_uri` ne sont pas pris en charge",
    "param_shape": "client_id, redirect_uri, response_type, code_challenge et "
                   "code_challenge_method sont requis une fois chacun ; state au plus une fois",
    "response_mode": "seuls response_type=code et le mode de réponse `query` sont pris en charge",
    "pkce": "PKCE est requis, avec code_challenge_method=S256",
}


def lire_demande(requete: str) -> tuple[Optional[dict], str]:
    """`(demande, "")` si la requête se relaie, `(None, raison)` sinon.

    ⚠️ PKCE S256 est EXIGÉ, pas seulement transmis : c'est la seule garde réelle contre un
    code intercepté (la marque ne lie qu'un rappel). Et seul le mode `query` se relaie : en
    `form_post` Logto POSTerait au rappel de la façade, en `fragment` le code n'atteindrait
    jamais le serveur."""
    paires = parse_qsl(requete, keep_blank_values=True)
    cles = [k for k, _ in paires]
    if "request" in cles or "request_uri" in cles:
        return None, "request_object"
    uniques = ("client_id", "redirect_uri", "response_type", "code_challenge",
               "code_challenge_method")
    if any(cles.count(k) != 1 for k in uniques) or cles.count("state") > 1 \
            or cles.count("response_mode") > 1:
        return None, "param_shape"
    v = dict(paires)
    if v["response_type"] != "code" or v.get("response_mode", "query") != "query":
        return None, "response_mode"
    if v["code_challenge_method"] != "S256" or not v["code_challenge"]:
        return None, "pkce"
    return {"client_id": v["client_id"], "redirect_uri": v["redirect_uri"],
            "state": v.get("state")}, ""


def reecrire(requete: str, remplacements: dict, ajouter: Optional[dict] = None) -> str:
    """La chaîne urlencodée avec les segments nommés remplacés — ajoutés s'ils manquent
    et figurent dans `ajouter`. ⚠️ Segment par segment, jamais re-sérialisée : `urlencode`
    réécrirait l'encodage du reste, et une paire répétée (`resource`) doit survivre."""
    segments, vus = [], set()
    for s in (requete.split("&") if requete else []):
        cle = _cle(s) if s else ""
        if cle in remplacements:
            s = f"{cle}={quote(remplacements[cle], safe='')}"
            vus.add(cle)
        segments.append(s)
    for cle, valeur in (ajouter or {}).items():
        if cle not in vus:
            segments.append(f"{cle}={quote(valeur, safe='')}")
    return "&".join(segments)


def retour(rappel_client: str, parametres: list) -> str:
    """Le rappel du client, paramètres de réponse REMPLACÉS (un `iss` glissé d'avance
    survivrait sinon à côté du nôtre — mix-up), et le reste de SA requête laissé à l'octet
    près : `a=b%20c` ne devient pas `a=b+c`."""
    base, _, requete = rappel_client.partition("?")
    garde = [s for s in requete.split("&") if s and _cle(s) not in _CLES_DE_REPONSE]
    return f"{base}?{'&'.join(garde + [urlencode(parametres)])}"


async def rappels_enregistres(c: Cible) -> frozenset:
    """Les rappels posés sur l'application de l'annuaire — relus, en cache court. La DCR
    l'oublie après chaque écriture (`oublier_rappels`) : un client qui s'enregistre puis
    autorise dans la foulée trouve son rappel."""
    cle = (getattr(c.directory, "key", None), c.app_id)
    en_cache = _rappels.get(cle)
    if en_cache is not None and en_cache[1] > time.monotonic():
        return en_cache[0]
    uris = await asyncio.wait_for(run_in_threadpool(facade._redirect_uris, c.app_id, c.directory),
                                  timeout=_LECTURE_TIMEOUT)
    _rappels[cle] = (frozenset(uris), time.monotonic() + _RAPPELS_TTL)
    return _rappels[cle][0]


def oublier_rappels(directory, app_id: str) -> None:
    _rappels.pop((getattr(directory, "key", None), app_id), None)


# ── l'échange de jeton ────────────────────────────────────────────────────────

_client: Optional[httpx.AsyncClient] = None


async def _client_http() -> httpx.AsyncClient:
    """Un client PARTAGÉ, construit hors boucle au premier échange : sa construction charge
    le magasin de certificats de façon synchrone (plusieurs secondes à froid)."""
    global _client
    if _client is None:
        _client = await run_in_threadpool(httpx.AsyncClient,
                                          timeout=httpx.Timeout(_JETON_TIMEOUT))
    return _client


async def _poster(url: str, corps: bytes, headers: dict) -> httpx.Response:
    client = await _client_http()
    return await asyncio.wait_for(client.post(url, content=corps, headers=headers),
                                  timeout=_JETON_TIMEOUT)


def ip_observee(request: Request) -> str:
    """L'adresse qu'a VUE le proxy de la box, jamais une valeur que le client écrit.

    `X-Forwarded-For` n'est lu que si le pair TCP est un proxy de confiance (la boucle
    locale : Caddy sur la box), et seulement son DERNIER segment — celui que ce proxy a posé.
    Tout autre pair est pris tel quel. Jamais `CF-Connecting-IP` : sur un host qui n'est pas
    derrière Cloudflare, personne ne l'écrase, et un seau clefé dessus ne freine rien."""
    pair = request.client.host if request.client else ""
    if pair in _PROXIES_DE_CONFIANCE:
        xff = request.headers.get("x-forwarded-for", "")
        dernier = xff.split(",")[-1].strip() if xff else ""
        if dernier:
            return dernier
    return pair or "unknown"


def _seau_ok(cle: str, maintenant: float) -> bool:
    jetons, dernier = _seaux.get(cle, (_SEAU_RAFALE, maintenant))
    jetons = min(_SEAU_RAFALE, jetons + (maintenant - dernier) * _SEAU_PAR_MIN / 60.0)
    _seaux[cle] = (jetons - 1.0 if jetons >= 1.0 else jetons, maintenant)
    while len(_seaux) > _SEAUX_MAX:
        del _seaux[next(iter(_seaux))]
    return jetons >= 1.0


async def _corps_borne(request: Request) -> Optional[bytes]:
    """Le corps, ou None s'il dépasse `_CORPS_MAX` — annoncé par `Content-Length` ou constaté
    en lisant : on ne met jamais en mémoire plus que la borne."""
    try:
        if int(request.headers.get("content-length") or 0) > _CORPS_MAX:
            return None
    except ValueError:
        return None
    lu = bytearray()
    async for morceau in request.stream():
        lu.extend(morceau)
        if len(lu) > _CORPS_MAX:
            return None
    return bytes(lu)


def _refus(code: str, detail: str, statut: int = 400, cors: bool = False, **headers):
    h = {"cache-control": "no-store", **headers, **(_cors() if cors else {})}
    return JSONResponse({"error": code, "error_description": detail}, status_code=statut,
                        headers=h)


def _cors() -> dict:
    return {"Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "content-type"}


def make_routes(public_url: str, claude_app_id: str) -> list[Route]:
    """Montées par la façade sur TOUT host ; sur un host non déclaré, elles refusent en le
    disant (sauf l'échange d'un code déjà relayé, que rien d'autre ne saurait échanger)."""

    def cible(request: Request) -> Cible:
        return cible_pour_host(facade._host_of(request), public_url, claude_app_id)

    def refus_autorisation(c: Cible, raison: str, code: str, detail: str, statut: int = 400,
                           client=None):
        # %r sur ce que le client écrit : une valeur décodée peut porter un saut de ligne.
        _log.warning("oauth.relay authorize refused reason=%s host=%s client=%r",
                     raison, c.host, client)
        return _refus(code, detail, statut)

    async def authorize(request: Request) -> Response:
        c = cible(request)
        requete = request.scope.get("query_string", b"").decode("latin-1")
        if not relais_actif(c.host):
            return refus_autorisation(c, "not_declared", "invalid_request",
                                      "le relais d'autorisation n'est pas actif sur ce host : "
                                      "relancer la découverte (/.well-known/oauth-authorization-server)")
        # Tenant désactivé (#1165) : aucune autorisation nouvelle, refus nommé.
        if (coupe := facade.refus_tenant_desactive(
                facade.tenant_for_host(c.host))) is not None:
            return coupe
        demande, raison = lire_demande(requete)
        if demande is None:
            return refus_autorisation(c, raison, "invalid_request", _REFUS_DEMANDE[raison])
        if not c.app_id or demande["client_id"] != c.app_id:
            return refus_autorisation(c, "foreign_client", "invalid_client",
                                      "client inconnu de ce serveur d'autorisation",
                                      client=demande["client_id"])
        if c.directory is None:
            _log.error("oauth.relay authorize host=%s déclaré mais annuaire non administré "
                       "(logto_mgmt ou credential absent) : relais impossible", c.host)
            return _refus("temporarily_unavailable", "le serveur d'autorisation de ce host "
                          "n'est pas administrable par la plateforme", 503)
        rappel = demande["redirect_uri"]
        # La MÊME garde que la DCR (`facade.redirect_autorise`), avec la liste du tenant de
        # ce host : un rappel que la DCR a refusé ne s'autorise pas, un qu'elle a accepté ne
        # se retrouve pas refusé ici. Le rappel de la façade n'y passe jamais.
        if not facade.redirect_autorise(facade.tenant_for_host(c.host), rappel):
            return refus_autorisation(c, "redirect_not_allowed", "invalid_request",
                                      "redirect_uri non autorisé", client=demande["client_id"])
        try:
            enregistres = await rappels_enregistres(c)
        except Exception as exc:
            _log.warning("oauth.relay authorize refused reason=directory_unreadable host=%s "
                         "(%s)", c.host, type(exc).__name__)
            return _refus("temporarily_unavailable", "l'application du serveur d'autorisation "
                          "est illisible — réessayer", 503)
        if rappel not in enregistres:
            return refus_autorisation(c, "redirect_not_registered", "invalid_request",
                                      "redirect_uri non enregistré pour ce client : l'enregistrer "
                                      "(/oauth/register) avant d'autoriser",
                                      client=demande["client_id"])
        etat = sceller_etat(secret(), c.as_base, rappel, demande["state"])
        _log.info("oauth.relay authorize mode=relay host=%s redirect_host=%r",
                  c.host, facade._uri_canonique(rappel).hostname)
        return redirection(c.oidc_public,
                           reecrire(requete, {"redirect_uri": c.rappel, "state": etat},
                                    ajouter={"state": etat}),
                           consentement=c.consentement)

    async def callback(request: Request) -> Response:
        c = cible(request)
        p = request.query_params
        cle = secret()
        ouvert, raison = ouvrir_etat(cle, p.get("state", ""), c.as_base) if cle \
            else (None, "no_secret")
        if ouvert is None:
            _log.warning("oauth.relay callback outcome=bad_state:%s host=%s", raison, c.host)
            return _refus("invalid_request", "état d'autorisation invalide ou expiré : "
                                             "recommencer la connexion depuis le client")
        rappel_client, etat_client, age = ouvert
        iss = p.get("iss")
        if iss is not None and iss.rstrip("/") not in c.emetteurs:
            _log.warning("oauth.relay callback outcome=iss_mismatch host=%s iss=%r "
                         "attendus=%r", c.host, iss, sorted(c.emetteurs))
            return _refus("invalid_request", "réponse d'autorisation d'un émetteur inattendu")
        if p.get("code"):
            parametres, issue = [("code", marquer_code(cle, c.as_base, rappel_client,
                                                       p["code"]))], "code"
        elif p.get("error"):
            parametres = [(k, p[k]) for k in ("error", "error_description", "error_uri")
                          if p.get(k)]
            issue = f"error:{p['error']}"
        else:
            _log.warning("oauth.relay callback outcome=empty host=%s", c.host)
            return _refus("invalid_request", "réponse d'autorisation sans code ni erreur")
        if etat_client is not None:
            parametres.append(("state", etat_client))
        parametres.append(("iss", c.issuer))
        _log.info("oauth.relay callback outcome=%r host=%s redirect_host=%r age_s=%d",
                  issue, c.host, urlsplit(rappel_client).hostname, age)
        return Response(status_code=302, headers={
            "location": retour(rappel_client, parametres), "cache-control": "no-store"})

    async def token(request: Request) -> Response:
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=_cors())
        c = cible(request)
        if not _seau_ok(ip_observee(request), time.monotonic()):
            return _refus("temporarily_unavailable", "trop de requêtes — réessayer", 429,
                          cors=True, **{"retry-after": "30"})
        # Tenant désactivé (#1165) : ni code échangé, ni jeton RAFRAÎCHI — le jeton de
        # rafraîchissement qu'il a émis ne rapporte plus rien par ce relais. Après le
        # seau, comme toute réponse de cette route.
        if (coupe := facade.refus_tenant_desactive(
                facade.tenant_for_host(c.host))) is not None:
            return coupe
        if not request.headers.get("content-type", "").lower().startswith(
                "application/x-www-form-urlencoded"):
            return _refus("invalid_request", "corps attendu en "
                          "application/x-www-form-urlencoded (RFC 6749 §4.1.3)", cors=True)
        if request.headers.get("authorization"):
            return _refus("invalid_client", "authentification client non prise en charge : "
                          "token_endpoint_auth_method=none", cors=True)
        brut = await _corps_borne(request)
        if brut is None:
            return _refus("invalid_request", "corps trop volumineux", 413, cors=True)
        corps = brut.decode("utf-8", "replace")
        paires = parse_qsl(corps, keep_blank_values=True)
        cles, v = [k for k, _ in paires], dict(paires)
        if cles.count("grant_type") != 1 or v["grant_type"] not in _GRANTS:
            return _refus("unsupported_grant_type", "seuls authorization_code et "
                          "refresh_token sont servis", cors=True)
        if cles.count("client_id") != 1 or not c.app_id or v["client_id"] != c.app_id:
            return _refus("invalid_client", "client_id absent ou inconnu de ce serveur "
                          "d'autorisation", cors=True)
        marque = v["grant_type"] == "authorization_code" and cles.count("code") == 1 \
            and v["code"].startswith(CODE_MARQUE + ".")
        if not relais_actif(c.host) and not marque:
            return _refus("invalid_request", "le relais d'autorisation n'est pas actif sur ce "
                          "host : relancer la découverte", cors=True)
        forme = "-"
        if v["grant_type"] == "authorization_code":
            if cles.count("code") != 1 or cles.count("redirect_uri") != 1:
                return _refus("invalid_request", "code et redirect_uri sont requis, une fois "
                              "chacun", cors=True)
            forme = "marked" if marque else "unmarked"
            if marque:
                cle = secret()
                code = lire_code(cle, c.as_base, v["redirect_uri"], v["code"]) if cle else None
                if code is None:
                    _log.warning("oauth.relay token grant=authorization_code code=tag_mismatch "
                                 "host=%s", c.host)
                    return _refus("invalid_grant", "le code ne correspond pas au redirect_uri "
                                  "de la demande d'autorisation", cors=True)
                corps = reecrire(corps, {"code": code, "redirect_uri": c.rappel})
            elif v["redirect_uri"] == c.rappel:
                # Un client légitime n'envoie JAMAIS le rappel de la façade : c'est un code
                # relayé dont on a retiré la marque pour sauter sa vérification.
                _log.warning("oauth.relay token grant=authorization_code code=mark_stripped "
                             "host=%s", c.host)
                return _refus("invalid_grant", "le code ne correspond pas au redirect_uri "
                              "de la demande d'autorisation", cors=True)
        global _en_vol
        if _en_vol >= _EN_VOL_MAX:
            _log.warning("oauth.relay token grant=%s code=%s upstream=saturated host=%s",
                         v["grant_type"], forme, c.host)
            return _refus("temporarily_unavailable", "trop d'échanges en cours — réessayer",
                          503, cors=True, **{"retry-after": "5"})
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                   "Accept": "application/json", "User-Agent": facade._UA}
        debut = time.monotonic()
        _en_vol += 1
        try:
            amont = await _poster(f"{c.oidc_jeton}/token", corps.encode(), headers)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            _log.warning("oauth.relay token grant=%s code=%s upstream=timeout host=%s",
                         v["grant_type"], forme, c.host)
            return _refus("server_error", "le serveur d'autorisation n'a pas répondu", 504,
                          cors=True)
        except httpx.HTTPError as exc:
            _log.warning("oauth.relay token grant=%s code=%s upstream=%s host=%s",
                         v["grant_type"], forme, type(exc).__name__, c.host)
            return _refus("server_error", "serveur d'autorisation injoignable", 502, cors=True)
        finally:
            _en_vol -= 1
        ms = int((time.monotonic() - debut) * 1000)
        json_amont = amont.headers.get("content-type", "").startswith("application/json")
        erreur, refresh, expire = "", "-", "-"
        if json_amont:
            try:
                lu = amont.json()
                if amont.status_code >= 400:
                    erreur = str(lu.get("error", ""))[:40]
                else:
                    # Un booléen et un entier seulement : jamais la valeur d'un jeton
                    # (oto-backend : savoir si un client OpenAI reçoit un refresh token).
                    refresh = "oui" if lu.get("refresh_token") else "non"
                    ttl = lu.get("expires_in")
                    expire = ttl if type(ttl) is int else "-"
            # noqa: SILENT — la raison n'est qu'une étiquette de journal, le corps part tel quel
            except Exception:
                if amont.status_code >= 400:
                    erreur = "?"
                else:
                    refresh = "?"
        _log.info("oauth.relay token grant=%s code=%s upstream=%d error=%r ms=%d host=%s "
                  "refresh_token=%s expires_in=%s",
                  v["grant_type"], forme, amont.status_code, erreur or "-", ms, c.host,
                  refresh, expire)
        if amont.status_code >= 500 or not json_amont:
            return _refus("server_error", "réponse inattendue du serveur d'autorisation", 502,
                          cors=True)
        garder = {k: amont.headers[k] for k in ("content-type", "cache-control", "pragma",
                                                "www-authenticate") if k in amont.headers}
        garder.setdefault("cache-control", "no-store")
        return Response(content=amont.content, status_code=amont.status_code,
                        headers={**garder, **_cors()})

    return [Route(AUTHORIZE_PATH, authorize, methods=["GET"]),
            Route(CALLBACK_PATH, callback, methods=["GET"]),
            Route(TOKEN_PATH, token, methods=["POST", "OPTIONS"])]
