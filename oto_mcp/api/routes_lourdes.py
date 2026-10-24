"""Les routes LOURDES de `/api/*` : un débit par jeton, une concurrence par route.

**Le défaut qu'il ferme** (#1141, infra#9). Le 04/10/2026, un seul client a appelé une
route d'agrégat 300 à 600 fois par minute ; chaque appel pouvait tenir une connexion
plusieurs secondes. Le pool de connexions s'est vidé pour TOUT le serveur : des
centaines de réponses 500 sur des routes qui n'avaient rien demandé, dont la file des
agents hébergés. Une route lourde ne doit pas pouvoir prendre le pool aux autres.

**Ce qu'il fait.** Pour chaque route déclarée dans `ROUTES_LOURDES` — le SEUL endroit
où une route devient lourde —, deux bornes, dans cet ordre, toutes deux sans attente :

1. **le débit par jeton** : au plus `par_minute` appels sur une fenêtre glissante de
   60 s, comptés par empreinte du jeton porté (`Authorization`), jamais par le jeton
   lui-même. Au-delà : `429 rate_limited`, `Retry-After` = le temps qu'il faut pour que
   le plus ancien appel sorte de la fenêtre ;
2. **la concurrence par route** : au plus `concurrence` requêtes en cours à la fois
   dans ce processus. Au-delà : `503 route_busy`, `Retry-After: 2`. Un appel refusé
   ici ne consomme pas de débit.

Le refus part AVANT le reste de la chaîne — ni authentification, ni handler : il ne
coûte au pool que la ligne du journal REST (`RestCallLogger`, au-dessus), écrite hors
de la boucle comme pour tout appel, et qui garde le refus visible. Corps JSON de l'enveloppe d'erreur ordinaire
(`{error, detail, details}`), avec les en-têtes CORS de l'origine : un navigateur le lit.

**Ce qu'il ne fait pas.** Les compteurs vivent dans le PROCESSUS : deux processus
(bascule bleu/vert, canari) ont chacun les leurs. C'est une protection du pool de CE
processus, lui aussi par processus — pas un quota de facturation. Le filtre global par
adresse reste celui du proxy. Pass-through total hors des routes déclarées : n'altère
ni `/mcp` ni le reste de `/api/*`.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .base import _cors_headers

_FENETRE_S = 60.0
# Au-delà, on purge les jetons qui n'ont plus rien dans leur fenêtre : la table ne
# grossit pas sans fin avec le nombre de jetons vus.
_PURGE_AU_DELA = 10_000
_ATTENTE_SATURATION_S = 2


@dataclass(frozen=True)
class RouteLourde:
    """Une route lourde : sa méthode, son gabarit de chemin (paramètres entre
    accolades, comme dans la table des routes) et ses deux bornes. `0` désarme une
    borne."""
    methode: str
    gabarit: str
    par_minute: int
    concurrence: int
    pourquoi: str
    motif: re.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        segments = [re.escape(s) for s in re.split(r"\{[^/{}]+\}", self.gabarit)]
        object.__setattr__(self, "motif", re.compile("^" + "[^/]+".join(segments) + "$"))

    def vise(self, methode: str, chemin: str) -> bool:
        return methode == self.methode and bool(self.motif.match(chemin))


#: Les routes lourdes de `/api/*` — LE seul endroit où une route le devient. Une route
#: s'y inscrit avec sa raison : ce qu'elle coûte, et ce qu'un client qui la martèle
#: priverait aux autres.
#:
#: `POST /api/me/runner/jobs` n'y entre PAS. Le 04/10 elle a été la VICTIME du pool
#: vide (52 des réponses 500), pas sa cause, et ses appelants sont les workers d'agents
#: hébergés, dont le sondage est régulier par construction : la brider ralentirait la
#: file sans rien protéger.
#:
#: Les bornes ci-dessous (#1145) sont réglées sur les consommateurs RÉELS recensés le
#: 04/10/2026, pas sur un idéal : un débit qui coupe un appelant légitime casse le
#: produit. Le débit vise le client qui martèle ; la concurrence protège le pool, et
#: elle est par processus. Chaque lecture d'agrégat est en plus bornée à 10 s
#: (`db.lecture_bornee`) : la concurrence × 10 s est le pire temps de pool qu'une route
#: peut prendre.
ROUTES_LOURDES: tuple[RouteLourde, ...] = (
    RouteLourde(
        "POST", "/api/me/recipes", par_minute=60, concurrence=4,
        pourquoi=("Les recettes en REST : un `run` appelle l'outil d'un connecteur page "
                  "après page et écrit dans un tableau, plusieurs secondes à plusieurs "
                  "minutes. Lancé d'un clic depuis une page, jamais en boucle : 60 par "
                  "minute couvre une page qui liste puis lance ; 4 en cours à la fois "
                  "par processus gardent le pool aux autres routes.")),
    RouteLourde(
        "GET", "/api/orgs/{id}/usage/calls", par_minute=900, concurrence=8,
        pourquoi=("Relevé de consommation appel par appel, le plus appelé de /api "
                  "(~294 000 appels en deux jours). Un service de facturation de tenant "
                  "le lit OUTIL PAR OUTIL (~17) à chaque relevé, 4 en parallèle, page par "
                  "page, toutes les 120 s, sous le jeton de l'utilisateur ; observé "
                  "jusqu'à 600 appels/min d'un même client. Le débit est posé AU-DESSUS "
                  "pour ne pas couper ce relevé légitime : c'est la concurrence (8) qui "
                  "garde le pool. Le remède de fond est usage/tools, une lecture pour "
                  "tous les outils.")),
    RouteLourde(
        "GET", "/api/orgs/{id}/usage/tools", par_minute=120, concurrence=4,
        pourquoi=("Relevé agrégé par outil d'une org sur une fenêtre close : UNE "
                  "requête d'agrégat sur tool_calls pour toute la fenêtre (jusqu'à "
                  "92 jours). Elle remplace les ~17 lectures par outil d'un relevé ; "
                  "deux par minute et par relevé suffisent largement.")),
    RouteLourde(
        "GET", "/api/me/instructions/{slug}/usage", par_minute=60, concurrence=4,
        pourquoi=("Usage d'une procédure : agrégat de 30 jours du journal sous l'org. "
                  "Mesurée à 27-30 s de médiane et 175 s au p95 avant #1145 ; lue à "
                  "l'ouverture de la fiche d'une procédure, par le tableau de bord et "
                  "par le front d'un tenant — une page n'en demande qu'une.")),
    RouteLourde(
        "GET", "/api/me/instructions-usage", par_minute=60, concurrence=4,
        pourquoi=("Usage de TOUTES les procédures de l'org : un agrégat de 30 jours du "
                  "journal sous l'org, groupé par procédure (#1146). Il remplace un "
                  "appel à …/{slug}/usage par ligne de liste ; une liste l'appelle une "
                  "fois à l'affichage.")),
    RouteLourde(
        "GET", "/api/me/activity-summary", par_minute=60, concurrence=6,
        pourquoi=("Agrégats de MON activité (cinq ventilations d'une fenêtre du "
                  "journal) ; 70 s au p95 avant #1145, 134 s avec days=365. Le front "
                  "d'un tenant l'appelle à CHAQUE chargement de page, days=365, pour "
                  "savoir si le compte a déjà appelé un outil : le débit (60/min par "
                  "jeton) laisse une navigation rapide passer ; la concurrence est plus "
                  "large que les autres lentilles parce que tous les comptes la lisent.")),
    RouteLourde(
        "POST", "/api/me/projects", par_minute=240, concurrence=8,
        pourquoi=("Console projets (op=list/get/runs/inventory/… ET les écritures) : "
                  "13 s au p95 avant #1145, lectures de runs d'un projet reconstruites "
                  "depuis le journal. Ouvrir un projet en déclenche plusieurs à la fois "
                  "(get, runs, inventory, activity) : le débit est large pour ne pas "
                  "couper une édition, la concurrence garde le pool.")),
    RouteLourde(
        "GET", "/api/datastores/{datastore}/rows/{row_id}/activity", par_minute=60,
        concurrence=3,
        pourquoi=("Parcours d'une ligne : une lecture du journal des appels. Le "
                  "08/10/2026, non bornée au tableau, elle parcourait tout le journal "
                  "(jusqu'à 302 s) ; seize appels en vingt minutes ont pris douze des "
                  "vingt-six connexions et mis la prod par terre. Bornée depuis au "
                  "tableau et à 10 s ; le cockpit l'ouvre une ligne à la fois.")),
    RouteLourde(
        "GET", "/api/datastores/{datastore}/activity", par_minute=60, concurrence=3,
        pourquoi=("Activité d'un tableau entier : une lecture du journal des appels "
                  "sur sa rétention, même famille que le parcours d'une ligne. Le "
                  "cockpit l'ouvre une fois par tableau.")),
    RouteLourde(
        "GET", "/api/admin/monitoring/summary", par_minute=30, concurrence=2,
        pourquoi=("Résumé d'appels de la supervision plateforme : sans périmètre, il "
                  "lit le journal de toute la plateforme (452 s pour un jour sous "
                  "contention le 04/10). Admin plateforme seul ; la console le demande "
                  "une fois par changement de fenêtre.")),
    RouteLourde(
        "GET", "/api/admin/monitoring/rest", par_minute=30, concurrence=2,
        pourquoi=("Agrégats des appels REST de toute la plateforme (trois lectures "
                  "d'une fenêtre du journal). Admin plateforme seul, une fois par "
                  "changement de fenêtre.")),
    RouteLourde(
        "GET", "/api/admin/monitoring/connectors", par_minute=30, concurrence=2,
        pourquoi=("Échecs de connecteurs de toute la plateforme sur une fenêtre du "
                  "journal. Admin plateforme seul, une fois par changement de fenêtre.")),
    RouteLourde(
        "GET", "/api/admin/monitoring/funnel", par_minute=30, concurrence=2,
        pourquoi=("Entonnoir d'activation : comptages sur tous les comptes et la "
                  "fenêtre du journal. Admin plateforme seul, une fois par changement "
                  "de fenêtre.")),
    RouteLourde(
        "GET", "/api/orgs/{id}/monitoring/summary", par_minute=30, concurrence=4,
        pourquoi=("Résumé d'appels d'une org (agrégat d'une fenêtre jusqu'à 90 jours "
                  "du journal sous l'org). Admin d'org ; la supervision d'org le "
                  "demande une fois par changement de fenêtre. Concurrence 4 : plusieurs "
                  "orgs peuvent superviser en même temps.")),
    RouteLourde(
        "GET", "/api/admin/tenants", par_minute=30, concurrence=2,
        pourquoi=("Vue des tenants : compteurs d'appels de tous les comptes sur 30 "
                  "jours, une lecture de toute la fenêtre du journal. Admin plateforme "
                  "seul.")),
    RouteLourde(
        "GET", "/api/admin/tenants/{slug}", par_minute=30, concurrence=2,
        pourquoi=("Fiche d'un tenant : ~150 s pour le primaire le 04/10 (journal de "
                  "toute la fenêtre lu deux fois). Pour le primaire, la lecture reste "
                  "celle de toute la plateforme. Admin plateforme ou admin du tenant, "
                  "une fiche à la fois.")),
)


def _empreinte(scope: dict) -> str:
    """La clé de débit : l'empreinte du jeton porté, jamais le jeton. Sans jeton,
    l'adresse du client — la requête sera refusée plus loin, mais elle ne passe pas
    au travers du débit pour autant."""
    for cle, valeur in scope.get("headers", []):
        if cle == b"authorization" and valeur:
            return "j:" + hashlib.sha256(valeur).hexdigest()[:32]
    client = scope.get("client") or ("?", 0)
    return f"a:{client[0]}"


class _Compteurs:
    def __init__(self) -> None:
        self.verrou = threading.Lock()
        self.appels: dict[tuple[str, str], deque] = {}
        self.en_cours: dict[str, int] = {}


class GardeRoutesLourdes:
    """Middleware ASGI : borne le débit par jeton et la concurrence des routes
    déclarées (`ROUTES_LOURDES`). Compteurs propres à l'instance, donc au processus."""

    def __init__(self, app, routes: Optional[Iterable[RouteLourde]] = None):
        self.app = app
        self.routes = tuple(ROUTES_LOURDES if routes is None else routes)
        self._c = _Compteurs()

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not self.routes:
            return await self.app(scope, receive, send)
        route = next((r for r in self.routes
                      if r.vise(scope.get("method", ""), scope.get("path", ""))), None)
        if route is None:
            return await self.app(scope, receive, send)

        refus = self._prendre(route, _empreinte(scope), time.monotonic())
        if refus is not None:
            return await self._refuser(scope, send, *refus)
        try:
            await self.app(scope, receive, send)
        finally:
            with self._c.verrou:
                self._c.en_cours[route.gabarit] -= 1

    def _prendre(self, route: RouteLourde, cle: str, maintenant: float):
        """Passe les deux bornes ; rend `None` (place prise) ou le refus à servir."""
        with self._c.verrou:
            fenetre = None
            if route.par_minute > 0:
                fenetre = self._c.appels.setdefault((route.gabarit, cle), deque())
                while fenetre and maintenant - fenetre[0] >= _FENETRE_S:
                    fenetre.popleft()
                if len(fenetre) >= route.par_minute:
                    attente = max(1, math.ceil(_FENETRE_S - (maintenant - fenetre[0])))
                    return (429, "rate_limited",
                            f"trop d'appels sur cette route : au plus {route.par_minute} "
                            f"par minute et par jeton. Réessaie dans {attente} s.",
                            attente, {"limit_per_minute": route.par_minute})
            en_cours = self._c.en_cours.get(route.gabarit, 0)
            if route.concurrence > 0 and en_cours >= route.concurrence:
                return (503, "route_busy",
                        f"cette route sert déjà {route.concurrence} requêtes en même "
                        "temps : réessaie dans quelques secondes.",
                        _ATTENTE_SATURATION_S, {"max_concurrent": route.concurrence})
            self._c.en_cours[route.gabarit] = en_cours + 1
            if fenetre is not None:
                fenetre.append(maintenant)
                self._purger(maintenant)
        return None

    def _purger(self, maintenant: float) -> None:
        if len(self._c.appels) > _PURGE_AU_DELA:
            for k in [k for k, v in self._c.appels.items()
                      if not v or maintenant - v[-1] >= _FENETRE_S]:
                del self._c.appels[k]

    async def _refuser(self, scope, send, statut, code, detail, attente, extra):
        corps = json.dumps({
            "error": code, "detail": detail,
            "details": {"retryable": True, "retry_after_seconds": attente, **extra},
        }).encode()
        origine = next((v.decode("latin-1") for k, v in scope.get("headers", [])
                        if k == b"origin"), None)
        entetes = [(b"content-type", b"application/json"),
                   (b"content-length", str(len(corps)).encode()),
                   (b"retry-after", str(attente).encode())]
        entetes += [(k.lower().encode(), v.encode())
                    for k, v in _cors_headers(origine).items()]
        await send({"type": "http.response.start", "status": statut, "headers": entetes})
        await send({"type": "http.response.body", "body": corps})
