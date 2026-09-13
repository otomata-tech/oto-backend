"""Le point d'autorisation de la façade : la demande du client, intacte, plus le
consentement qu'exige Logto pour délivrer un jeton de rafraîchissement (oto#202).

**Le défaut mesuré (12/09/2026).** Le connecteur Codex Apps demandait `offline_access`
sans `prompt=consent`. Logto (oidc-provider, `check_scope`) retire alors `offline_access`
de la demande : aucun jeton de rafraîchissement n'est délivré, et chaque expiration du
jeton d'accès (3 600 s) impose une autorisation complète. La métadonnée publiait
directement le point d'autorisation de Logto : Oto ne voyait pas passer la demande et ne
pouvait rien y faire.

**Ce que ce module fait, et seulement cela.** Il redirige vers le point d'autorisation de
NOTRE annuaire — fourni par la façade, JAMAIS lu dans la requête — en recopiant la requête
du client octet pour octet, et n'y change qu'une chose : quand `scope` demande
`offline_access` et que `prompt` ne porte pas déjà `consent`, il ajoute `consent`. Aucun
droit n'est ajouté : un client qui ne demande pas `offline_access` (Mistral n'envoie aucun
scope) suit exactement le parcours d'avant, avec un saut de redirection en plus.

**Ce qu'il laisse passer tel quel**, pour que Logto le traite comme avant :
- `prompt=none` — une autorisation SANS interaction. Y ajouter `consent` fabriquerait une
  combinaison que la norme interdit (« prompt none must only be used alone »), et supposer
  acquis un consentement silencieux serait faux : Logto retire `offline_access`, comme
  aujourd'hui ;
- un `scope` ou un `prompt` répété — Logto rend `invalid_request` ;
- `request` / `request_uri` — la demande vit dans un objet qu'on ne réécrit pas.

⚠️ **Les hôtes d'un tenant ne passent pas par ici.** Délivrer des jetons de
rafraîchissement aux utilisateurs d'un partenaire est SA décision : sa métadonnée continue
d'annoncer son propre point d'autorisation, et la façade refuse cette route sur son hôte.
"""
from __future__ import annotations

import logging
from urllib.parse import parse_qsl, quote, unquote_plus

from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

_OFFLINE = "offline_access"
_CONSENT = "consent"
_SANS_INTERACTION = "none"


def _cle(segment: str) -> str:
    return unquote_plus(segment.split("=", 1)[0])


def avec_consentement(requete: str) -> str:
    """La requête du client, avec `consent` ajouté à `prompt` quand `offline_access` est
    demandé sans consentement explicite. Dans tous les autres cas, rendue INCHANGÉE.

    ⚠️ On ne re-sérialise JAMAIS la requête entière : `urlencode` réécrirait l'encodage
    des autres paramètres (`%20` en `+`, ordre, doublons), et un `state` réencodé
    autrement ne se reconnaît plus au retour chez le client. Seul le segment `prompt` est
    réécrit — encodé `%20`, que tout décodeur lit comme une espace —, ou ajouté en fin.
    """
    paires = parse_qsl(requete, keep_blank_values=True)
    cles = [cle for cle, _ in paires]
    if "request" in cles or "request_uri" in cles:
        return requete
    if cles.count("scope") != 1 or cles.count("prompt") > 1:
        return requete
    valeurs = dict(paires)
    if _OFFLINE not in valeurs["scope"].split():
        return requete
    if "prompt" not in valeurs:
        return f"{requete}&prompt={_CONSENT}"
    prompt = valeurs["prompt"].split()
    if _CONSENT in prompt or _SANS_INTERACTION in prompt:
        return requete
    segment = "prompt=" + quote(" ".join(prompt + [_CONSENT]), safe="")
    return "&".join(segment if _cle(s) == "prompt" else s for s in requete.split("&"))


def redirection(emetteur_oidc: str, requete: str) -> Response:
    """302 vers `<émetteur>/auth`, sans cache, requête recopiée APRÈS le `?`.

    `requete` est la chaîne BRUTE reçue (`scope["query_string"]` décodé en latin-1) : pas
    de `RedirectResponse`, qui répond 307 par défaut et ré-encode l'adresse — les octets
    du client n'arriveraient plus intacts. Un caractère de contrôle n'a rien à faire dans
    une requête : il est refusé en le nommant, jamais recopié dans un en-tête.
    """
    if any(ord(c) < 0x21 or ord(c) == 0x7F for c in requete):
        return JSONResponse({"error": "invalid_request",
                             "error_description": "caractère de contrôle dans la requête "
                                                  "d'autorisation"}, status_code=400)
    cible = f"{emetteur_oidc.rstrip('/')}/auth"
    demande = avec_consentement(requete)
    if demande != requete:
        client = dict(parse_qsl(requete, keep_blank_values=True)).get("client_id")
        logger.info("autorisation : consent ajouté pour offline_access (client %r)", client)
    return Response(status_code=302, headers={
        "location": f"{cible}?{demande}" if demande else cible,
        "cache-control": "no-store"})
