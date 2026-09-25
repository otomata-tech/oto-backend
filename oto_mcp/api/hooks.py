"""La route qu'un TIERS appelle pour déclencher un agent — `POST /api/hooks/{id}`.

Écrite à la main, hors de la couche capacité, et ce n'est pas un choix de style :
**l'adaptateur REST refuse tout champ d'entrée qu'une capacité ne déclare pas**
(400 `unknown_fields`). Un corps JSON LIBRE — la demande même de cette route — ne
peut donc pas y passer. Le précédent existe et porte la même marque : le webhook
Mollie (`api/billing.py`), non authentifié par JWT, monté à la main.

## Ce que la route fait, et ce qu'elle ne fait pas

Elle **adapte** : elle lit l'en-tête, le corps et l'agent appelant, puis appelle
`runner_hook.declencher`. Toute la décision — secret, lissage, façonnage de la
charge, enfilage — vit là-bas, où elle se teste sans HTTP.

⚠️ **Tout le travail passe par `run_in_threadpool`.** Le serveur est mono-loop et
psycopg est synchrone : une requête base faite dans la boucle bloque TOUTES les
autres requêtes du process. Une rafale de webhooks ressemblerait alors à une panne
de plateforme (`docs/event-loop-perf.md`, et le même geste dans le webhook Mollie).

⚠️ **Aucun 5xx pour une raison métier.** Un envoyeur qui reçoit un 500 retente, et
retente encore : c'est ainsi qu'une erreur de configuration devient une tempête.
Chaque refus prévu a son code, et il est définitif du point de vue de l'envoyeur.
"""
from __future__ import annotations

import functools
import json
import logging

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .. import runner_hook

logger = logging.getLogger(__name__)

#: Le corps accepté. Lu AVANT d'être parsé : un mégaoctet de JSON hostile ne doit
#: pas être désérialisé pour être refusé.
_CORPS_MAX = runner_hook.CORPS_MAX


async def _lire_borne(request: Request) -> bytes | None:
    """Le corps, ou None dès qu'il dépasse le plafond — sans lire la suite."""
    declare = request.headers.get("content-length")
    if declare and declare.isdigit() and int(declare) > _CORPS_MAX:
        return None
    morceaux, total = [], 0
    async for morceau in request.stream():
        total += len(morceau)
        if total > _CORPS_MAX:
            return None
        morceaux.append(morceau)
    return b"".join(morceaux)


def _refus(statut: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": code, "detail": message, **extra},
                        status_code=statut)


async def fire(request: Request) -> JSONResponse:
    """Un tiers POSTe, un agent part.

    Réponses :
      202 le travail est enfilé (`delayed_seconds` s'il a été lissé)
      202 `duplicate: true` — cette livraison (même `webhook-id` signé) a déjà
          été acceptée : aucun second travail
      400 le corps n'est pas du JSON — ou (`hook_stale_timestamp`) une signature
          VALIDE dont l'horodatage sort de la fenêtre de 5 min
      404 identifiant inconnu, OU secret faux, OU signature fausse, OU preuve
          de l'autre mode — délibérément indistinguables
      409 l'agent est en pause
      413 le corps dépasse le plafond
      429 la file dépasse déjà sa fraîcheur, OU (`hook_daily_cap`) le plafond
          journalier déclaré par le propriétaire est atteint (avec `Retry-After`)
      L'adresse est l'id numérique OU l'adresse privée `h_…` ; un agent qui a une
      adresse privée n'ouvre plus par son id (même 404 que tout le reste).
    """
    # Le segment est un id numérique OU une adresse privée (`h_…`, 128 bits).
    # Résolue hors boucle : une adresse privée se lit en base.
    trigger_id, par_adresse_privee = await run_in_threadpool(
        runner_hook.resoudre_adresse, str(request.path_params.get("trigger_id", "")))
    if trigger_id is None:
        return _refus(404, "hook_not_found", runner_hook.HOOK_INCONNU)
    secret = runner_hook.secret_du_porteur(request.headers.get("authorization"))

    # ⚠️ La TAILLE avant le PARSE, et en FLUX : `request.body()` bufferise tout
    # avant de rendre la main, donc un corps de cent mégaoctets serait entièrement
    # en mémoire au moment où on le refuse — sur une route qu'un inconnu peut
    # appeler sans credential. On lit morceau par morceau et on s'arrête au
    # premier octet de trop ; le reste n'est jamais lu.
    brut = await _lire_borne(request)
    if brut is None:
        # Le propriétaire est le seul à pouvoir réparer une source trop bavarde :
        # la trace part, hors boucle comme tout le reste.
        await run_in_threadpool(runner_hook.noter_corps_trop_gros, trigger_id,
                                secret, (request.headers.get("user-agent") or "")[:200])
        return _refus(413, "payload_too_large",
                      f"Body above the {_CORPS_MAX}-byte limit. Send a REFERENCE "
                      "(an id the agent will load), not the whole record.")

    corps = None
    if brut.strip():
        try:
            corps = json.loads(brut)
        except ValueError:
            # ⚠️ Refus NOMMÉ plutôt qu'un corps ignoré : une source qui envoie du
            # formulaire là où on attend du JSON verrait sinon ses agents tourner
            # sans jamais recevoir sa donnée, et rien ne le dirait.
            return _refus(400, "invalid_json",
                          "The body is not JSON. Send a JSON object, or nothing "
                          "at all if the agent does not need one.")

    # La preuve SIGNÉE (Standard Webhooks), jugée sur les octets REÇUS (`brut`),
    # jamais sur le JSON re-sérialisé : un espace ou un ordre de clés différent
    # suffirait à refuser une livraison légitime.
    signature = runner_hook.signature_des_entetes(request.headers, brut)

    try:
        rendu = await run_in_threadpool(
            functools.partial(
                runner_hook.declencher, trigger_id, secret, corps,
                (request.headers.get("user-agent") or "")[:200],
                signature=signature, par_adresse_privee=par_adresse_privee))
    except runner_hook.HookRefus as refus:
        entetes = ({"Retry-After": str(refus.retry_after)}
                   if refus.retry_after else None)
        r = _refus(refus.statut, refus.code, refus.message)
        if entetes:
            r.headers.update(entetes)
        return r
    except Exception:  # noqa: BLE001 — voir ci-dessous
        # ⚠️ Le SEUL 500 de cette route, et il dit une panne de NOTRE côté — base
        # injoignable, bogue. L'envoyeur DOIT le voir comme réessayable : c'est le
        # seul cas où sa retentative est la bonne conduite. Journalisé entier,
        # jamais avalé (`lint_silences`).
        logger.exception("webhook %s : déclenchement impossible", trigger_id)
        return _refus(500, "hook_failed",
                      "The trigger failed on our side. Retry: no job was "
                      "queued.")

    return JSONResponse(rendu, status_code=202)


def make_routes(options_handler) -> list[Route]:
    """La route du webhook. `options_handler` sert le pré-vol CORS, comme partout
    ailleurs — une source appelée depuis un navigateur existe (un formulaire, un
    outil no-code hébergé)."""
    return [
        Route("/api/hooks/{trigger_id}", fire, methods=["POST"]),
        Route("/api/hooks/{trigger_id}", options_handler, methods=["OPTIONS"]),
    ]
