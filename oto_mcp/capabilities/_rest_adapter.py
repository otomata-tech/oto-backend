"""Adaptateur REST de la couche capacité (ADR 0009).

Boucle sur le registre et monte une Route Starlette par capacité ayant un
binding `rest`. Même séquence que l'adaptateur MCP : authenticate → input
(path_params + body) → autz → handler. L'`AuthzDenied` neutre est re-émis via
`json_error(request, status, code)` — **conserve l'enveloppe + les en-têtes
CORS** consommés par le dashboard.

⚠️ **Autz et handler sync partent en threadpool** — même raison qu'en MCP, et le même
piège : la route est `async def` (elle doit `await` le corps et l'authentification),
donc Starlette la laisse dans la boucle, et tout ce qu'elle appelle nûment avec. Un
serveur MONO-LOOP ne tolère pas ça : `docs/event-loop-perf.md` mode n°4, incident du
2026-09-01. Le garde-fou est `tests/test_capacites_hors_boucle.py`.

Dépend du core (sens unique ADR 0004).
"""
from __future__ import annotations

import dataclasses
import logging
import types
import typing
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

from .. import client_trace
from ..json_body import InvalidJsonBody, read_json_body
from ._types import AuthzDenied, Capability, NotModified, RawCtx
from ._execution import execute

AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]


def _porte_une_liste(annotation) -> bool:
    """`list[...]` quelque part dans l'annotation — `Optional`/`Union` traversés, rien
    d'autre. ⚠️ Pas `Literal` : ses arguments sont des VALEURS (`Literal["a", "b"]`),
    et les traverser comme une union ferait d'un scalaire une liste."""
    origine = typing.get_origin(annotation)
    if annotation is list or origine is list:
        return True
    if origine in (typing.Union, types.UnionType):
        return any(_porte_une_liste(a) for a in typing.get_args(annotation))
    return False


def _champs_liste(model) -> frozenset:
    """Les champs de l'`Input` qui DÉCLARENT une liste — calculé une fois au montage."""
    return frozenset(nom for nom, f in model.model_fields.items()
                     if _porte_une_liste(f.annotation))


def _make_handler(cap: Capability, binding, verifier, authenticate, json_response, json_error):
    champs_liste = _champs_liste(cap.Input)

    async def _handler(request: Request) -> JSONResponse:
        # `allow_api_token` n'est passé QUE lorsqu'il vaut False : le défaut reste un
        # appel à deux arguments, donc les appelants (et les stubs de test) écrits avant
        # ce cran continuent de fonctionner tels quels.
        if binding.allow_api_token:
            sub, err = await authenticate(request, verifier)
        else:
            sub, err = await authenticate(request, verifier, allow_api_token=False)
        if err:
            return err
        data: dict = {}
        # Query string (filtres des GET/DELETE sans body : `?query=…&limit=…`).
        # Valeurs str → pydantic coerce vers le type du champ Input. Priorité la
        # plus basse (body puis path params écrasent).
        #
        # Une clé RÉPÉTÉE (`?filter=a:1&filter=b:2`) arrive en LISTE quand le champ
        # en déclare une, et jamais autrement. Jusqu'au 29/08 (#418), ce bloc faisait
        # `dict(request.query_params)` — qui ne garde que la DERNIÈRE valeur : `a`
        # disparaissait sans un mot, alors que la face MCP recevait la liste entière
        # et que l'OpenAPI servi (`anyOf [array, string]`, sérialisation `form` +
        # `explode`) promettait exactement cette forme. Une clé unique reste une
        # chaîne : c'est au champ de la normaliser (virgule, cf. #367), pour que les
        # deux faces passent par la même validation. Une clé répétée sur un champ
        # SCALAIRE est REFUSÉE plus bas, jamais tronquée.
        repetees_scalaires: list[str] = []
        for cle in request.query_params.keys():
            valeurs = request.query_params.getlist(cle)
            if len(valeurs) == 1:
                data[cle] = valeurs[0]
                continue
            data[cle] = valeurs
            if cle not in champs_liste:
                repetees_scalaires.append(cle)
        if request.method in ("POST", "PUT", "PATCH") or binding.reads_body:
            # Un corps illisible est REFUSÉ, jamais ignoré — c'est le même principe
            # que la garde des champs inconnus vingt lignes plus bas, et il lui
            # manquait exactement ce cas : la garde couvrait « un champ que je ne
            # connais pas », pas « un corps que je ne comprends pas ». Sur ~200 routes
            # générées, l'appelant recevait un 200 et des valeurs par défaut
            # (`docs/silences-2026-08-27.md`, site B4). Un corps ABSENT reste `{}` :
            # les routes sans argument ne changent pas de contrat.
            try:
                body = await read_json_body(request)
            except InvalidJsonBody as e:
                logger.warning("capacité %s : corps de requête refusé (%s)",
                               cap.key, e.code)
                return json_error(request, 400, e.code, e.detail)
            # `if body:` — un corps `{}` EXPLICITE ne se distingue pas d'un corps
            # absent une fois parsé, donc il laisse le champ à son défaut au lieu de
            # recevoir `{}`. Vérifié inerte sur les quatre capacités à `body_field` :
            # `fields` vaut `{}` par défaut, `row`/`patch` ont une default_factory, et
            # `arguments` (défaut `None`) est normalisé par son handler — un test le
            # fige (`test_tools_me_capability`, cas `({}, {})`).
            if body:
                if binding.body_field:
                    # Corps LIBRE (les colonnes d'une ligne de tableau) : il ne se
                    # fusionne pas clé par clé, il EST la valeur d'un champ déclaré.
                    # Cf. `RestBinding.body_field` — la garde ci-dessous continue
                    # donc de couvrir la query string et les params de chemin.
                    data[binding.body_field] = body
                else:
                    data.update(body)
        # path params : mapping explicite placeholder->champ Input, sinon nom identique.
        for ph, value in request.path_params.items():
            field = (binding.path_map or {}).get(ph, ph)
            data[field] = value
        # REFUSER un champ inconnu, jamais l'IGNORER.
        #
        # Pydantic ignore par défaut les clés qu'il ne connaît pas (`extra="ignore"`).
        # Un client qui se trompe de forme reçoit donc un 200 et un comportement de
        # repli, sans le moindre signal. Vécu le 05/08 : un front envoyait
        # `{app, scope}` au premier niveau alors que l'`Input` déclare `params: dict` —
        # les deux ont été jetés en silence, le scope est retombé sur sa valeur par
        # défaut et le retour OAuth est parti chez le mauvais front. Aucune erreur,
        # aucun log, une demi-journée pour le trouver.
        #
        # C'est la MÊME famille que le bug des jetons de contexte du 28/07 (`account`
        # métier mangé par l'axe `account`) : un argument légitime avalé sans bruit.
        # Le remède est le même — refuser plutôt qu'ignorer — et il vaut pour les ~200
        # routes générées, pas connecteur par connecteur.
        #
        # Les noms sont RENDUS au client : un refus qui ne dit pas quel champ pose
        # problème oblige à deviner, et c'est exactement ce qu'on cherche à supprimer.
        inconnus = sorted(set(data) - set(cap.Input.model_fields))
        if inconnus:
            logger.warning("capacité %s : champ(s) inconnu(s) refusé(s) : %s",
                           cap.key, ", ".join(inconnus))
            return json_error(
                request, 400, "unknown_fields",
                f"Champ(s) non reconnu(s) : {', '.join(inconnus)}. "
                f"Attendus : {', '.join(sorted(cap.Input.model_fields))}.")
        # Après la garde des inconnus (une clé inconnue répétée est d'abord inconnue),
        # avant la validation : pydantic accepterait `["1", "2"]` pour certains
        # scalaires ou rendrait un `invalid_input` qui ne nomme pas la clé.
        if repetees_scalaires:
            noms = sorted(repetees_scalaires)
            logger.warning("capacité %s : paramètre(s) scalaire(s) répété(s) refusé(s) : %s",
                           cap.key, ", ".join(noms))
            return json_error(
                request, 400, "repeated_scalar",
                f"Paramètre(s) répété(s) alors qu'une seule valeur est attendue : "
                f"{', '.join(noms)}. Une liste se déclare dans le schéma du champ ; "
                f"ici, n'envoie qu'une valeur.")
        try:
            inp = cap.Input(**data)
        except ValidationError:
            return json_error(request, 400, "invalid_input")
        # L'empreinte du client (IP réelle + user-agent) est posée AUTOUR du
        # handler : elle est un fait de transport, et un handler ne voit pas la
        # requête (ADR 0004). Une seule capacité la lit aujourd'hui — l'acceptation
        # d'un document légal, qu'il faut pouvoir SITUER, pas seulement dater — mais
        # elle vaut pour toutes les routes générées, donc elle se pose ici et pas
        # dans une route. `finally` obligatoire : une ContextVar non reset fuit sur
        # la requête suivante servie par la même tâche.
        jeton_client = client_trace.set_current(
            ip=client_trace.pick_ip(request.headers.get("cf-connecting-ip"),
                                    request.headers.get("x-forwarded-for"),
                                    request.client.host if request.client else None),
            user_agent=request.headers.get("user-agent"))
        try:
            def _amont():
                """Autz + handler SYNC — le bloc qui touche la base, en un thread.

                `run_in_threadpool` (anyio) exécute sur une COPIE du contexte : le
                jeton `client_trace` posé juste au-dessus est donc bien lu par le
                handler. Ce qu'une copie ne rend PAS, c'est une écriture de ContextVar
                faite DANS le thread — aucune capacité n'en fait, et le jour où l'une
                s'y mettrait, c'est ici que ça se saurait."""
                ctx_ = cap.authz(RawCtx(sub=sub), inp)
                # Symétrique du seuil MCP : la face HTTP se nomme, elle aussi. Sans ça
                # « pas mcp » serait la seule façon de reconnaître REST — donc un
                # adaptateur muet passerait pour la face humaine.
                ctx_ = dataclasses.replace(ctx_, channel="rest")
                return ctx_, inp

            ctx, result = await execute(cap.handler, _amont)
        except AuthzDenied as d:
            # `message` EN 4e ARG, sinon il est jeté et le client ne voit qu'un code nu.
            # Les auteurs de capacités écrivent des refus actionnables (« Enregistre
            # d'abord le Consumer Key… ») qui n'atteignaient personne : `_json_error`
            # n'émet `detail` que s'il lui est passé. La face MCP, elle, rendait déjà
            # `d.message` — les deux surfaces disaient donc des choses différentes du
            # MÊME refus.
            #
            # `details` en 5e arg, et SEULEMENT s'il y en a : un refus structuré
            # (#487) doit arriver entier au client, mais le passer inconditionnellement
            # imposerait la signature à tous les `json_error` injectés — dont ceux des
            # modules de routes historiques et des stubs de test.
            if d.details:
                return json_error(request, d.status, d.code, d.message or None,
                                  details=d.details)
            return json_error(request, d.status, d.code, d.message or None)
        finally:
            client_trace.reset(jeton_client)
        if isinstance(result, NotModified):
            # 304 : **sans corps**, c'est la spec et c'est tout l'intérêt — le client
            # garde ce qu'il a en cache. Un 200 portant « rien n'a changé » ferait
            # ranger CE message à la place des données.
            return Response(status_code=304, headers=_cors_of(request, json_response))
        return json_response(request, result, status=binding.status)
    return _handler


def _cors_of(request, json_response) -> dict:
    """Les en-têtes CORS de la réponse ordinaire, recopiés sur la 304.

    Une 304 est une réponse comme une autre pour le navigateur : sans `Access-Control-
    Allow-Origin`, le dashboard voit une erreur CORS là où le serveur a répondu « ton
    cache est bon ». On les DÉRIVE de la réponse normale plutôt que de les réécrire —
    la politique CORS vit dans `json_response`, et un second endroit qui la décide
    divergerait au premier changement d'origine autorisée."""
    try:
        modele = json_response(request, {}, status=200)
        return {k: v for k, v in modele.headers.items()
                if k.lower().startswith("access-control-")}
    # noqa: SILENT — sans origine lisible, pas d'en-tête CORS : le navigateur tranchera
    except Exception:
        return {}


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
    capabilities: list[Capability],
) -> list[Route]:
    """Une Route (+ OPTIONS) par capacité REST. Liste vide si rien (canari)."""
    routes: list[Route] = []
    for cap in capabilities:
        if not cap.is_exposed():
            continue
        for binding in cap.rest_bindings():
            h = _make_handler(cap, binding, verifier, authenticate, json_response, json_error)
            routes.append(Route(binding.path, h, methods=[binding.verb]))
            routes.append(Route(binding.path, options_handler, methods=["OPTIONS"]))
    return routes
