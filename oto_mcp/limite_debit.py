"""Garde ASGI : le débit applicatif de TOUT ce que sert le processus (F-025).

**Le défaut qu'il ferme.** Rien, dans l'application, ne bornait le débit de `/mcp`, de
`/api/*`, des webhooks ni des routes d'authentification : seules trois bornes locales
existaient (`api/routes_lourdes` sur quelques agrégats, le seau du relais sur
`/oauth/token`, celui des sous-domaines de projet publiés). Le serveur est MONO-LOOP et
son pool de connexions est partagé : un scanner, un fuzzer ou un seul client qui
martèle suffit à dégrader le service de TOUS les autres (le 04/10/2026, un seul client
à 600 appels/min a vidé le pool, #1141).

**Ce qu'il fait.** Chaque requête HTTP tombe dans UNE classe, décidée sur ce qu'elle
porte (sans rien vérifier, sans I/O) ; chaque classe a son seau à jetons (débit
régulier `par_minute`, rafale `par_minute / 2`), et sa clé :

- `jeton` — la requête porte `Authorization` : clé = empreinte du jeton porté (jamais
  le jeton), comme `routes_lourdes._empreinte`. Un principal, quelle que soit son
  adresse : un client hébergé (claude.ai) sert TOUS ses utilisateurs depuis quelques
  adresses, une borne par adresse sur le trafic authentifié les couperait ensemble ;
- `sensible` — sans jeton, une route dont le chemin porte un secret ou qui ouvre un
  geste d'authentification interactif (invitation, partage public, désabonnement,
  autorisation, retours OAuth) : clé = adresse, borne STRICTE — c'est la surface
  d'énumération. L'inscription du shim anonyme (`/register`, `/oauth/register` d'un
  projet publié) n'y est PAS : un client hébergé s'inscrit à chaque connexion
  d'utilisateur, depuis sa sortie partagée — elle est `anonyme` ;
- `webhook` — un tiers qui déclenche (`/api/hooks/*`, `/api/receivers/*`, webhooks de
  facturation et de Logto) : clé = (adresse, chemin), pour qu'un envoyeur partagé (une plateforme
  d'automatisation sert mille clients depuis ses adresses) ne s'épuise pas d'un crochet
  sur l'autre ; plus un plafond par adresse, tous chemins confondus, à dix fois ce
  débit — sans lui, faire tourner le chemin (`/api/hooks/<deviné>`) rendrait un seau
  neuf à chaque essai ;
- `anonyme` — tout le reste sans jeton (découverte `/.well-known/*`, inscription du shim
  anonyme, `/mcp` avant connexion, upload signé, relais) : clé = adresse.

**Le contournement qu'il referme : le jeton inventé.** La classe `jeton` se décide à la
PRÉSENCE de l'en-tête — un `Bearer <aléatoire>` par requête aurait chacun son seau neuf.
D'où une cinquième borne, `echecs`, par adresse : chaque réponse 401/403 (et 404 sur la
classe `sensible`, où un 404 est un secret deviné de travers) consomme un jeton de
l'adresse ; une adresse qui a épuisé ce budget voit refuser ses jetons INCONNUS et ses
requêtes `sensible`. Elle ne touche JAMAIS :

- un jeton qui a déjà obtenu une réponse acceptée de ce processus (`_reconnus`) — ni
  au refus, ni au débit : son 401 (jeton expiré qui va se rafraîchir) ne coûte rien à
  ses voisins d'adresse ;
- la classe `anonyme` (elle paie déjà son seau par adresse, et porte la découverte
  OAuth d'un client hébergé) ;
- la classe `webhook` (ni refus, ni débit) : un envoyeur retente pendant des jours sur
  un crochet supprimé, et ses 404 ne doivent pas épuiser le budget de l'adresse qu'il
  partage avec mille autres clients ; son seau (adresse, chemin) et son plafond par
  adresse sont sa limite ;
- une adresse exemptée (ci-dessous).

⚠️ Le prix assumé : depuis une adresse au budget épuisé, le PREMIER appel d'un jeton
tout juste émis est refusé jusqu'à ce que le budget remonte (`Retry-After`).

**L'adresse** est celle de la règle unique du dépôt (`subdomain_project._client_ip`,
c'est-à-dire `client_trace.pick_ip`) : le pair TCP ; les en-têtes seulement si ce pair
est un proxy déclaré dans `OTO_PROXIES_DE_CONFIANCE`, et alors `CF-Connecting-IP`, puis
l'adresse la plus à droite de `X-Forwarded-For` hors des réseaux de confiance. ⚠️ Sans ce
réglage, derrière un proxy, tout le trafic partage l'adresse du proxy.
⚠️⚠️ **Elle ne vaut que ce que vaut le proxy** :
servie derrière un proxy déclaré qui n'ÉCRASE pas ces DEUX en-têtes avec le pair qu'il a vu (le Caddy de
nos boxes depuis F-024), une instance laisse le client les écrire et choisir son seau —
les classes par adresse et le budget d'échecs y sont alors contournables. D'où le
défaut `report` : **`enforce` ne se pose QUE derrière un proxy qui écrase les deux
en-têtes**, instance par instance. Une seconde règle d'adresse ici divergerait de la
trace de consentement et du seau des projets publiés.

**Adresses exemptées** (`OTO_DEBIT_ADRESSES_EXEMPTEES`, adresses ou CIDR séparés par des
virgules, vide par défaut) : nos propres machines (runner, front, back) ne passent ni
par les classes par adresse ni par le budget d'échecs ; leurs jetons paient toujours la
classe `jeton`. Les adresses se déclarent dans la configuration de l'instance, jamais
dans ce dépôt public.

**Le refus** : `429 rate_limited`, `Retry-After` = le temps qu'il faut au seau pour
rendre un jeton, corps de l'enveloppe d'erreur ordinaire (`{error, detail, details}`,
même forme que `routes_lourdes`), en-têtes CORS de l'origine et `X-Oto-Version`. Il est
décidé AVANT d'appeler la suite — rien n'est lu du corps, rien n'est mis en tampon (le
SSE de `/mcp` passe intact) ; seule la ligne de statut de la réponse est lue, pour
`echecs`. Un refus n'écrit RIEN en base sur son chemin : une ligne de journal par clé et
par minute au plus, et la mesure (`limite_debit_mesure`) agrège en mémoire et n'écrit
qu'une ligne par classe et par minute, en tâche de fond ; le palmarès des clés
HACHÉES refusées (`limite_refus_top`, #1232) agrège de même et s'écrit hors de la boucle.

**Où il est posé** (`server.build_root_app`) : sous la garde de déconnexion, AU-DESSUS du
compteur des refus du transport — un 429 de débit n'est pas un refus du transport, et
le compter le noierait — donc au-dessus du dispatch par Host : les deux faces MCP et
toute la face REST d'un seul geste.

**Ce qu'il ne fait pas.** Les compteurs vivent dans le PROCESSUS (bascule bleu/vert,
canari : chacun les siens) : c'est une protection de CE processus, pas un quota de
facturation. `OPTIONS` passe toujours (un pré-vol ne coûte rien et un navigateur en
paie un par appel). Les bornes plus fines restent où elles sont et s'appliquent EN
PLUS (`routes_lourdes`, relais, projets publiés).

**Réglages** (`env_inventory`), lus UNE fois au démarrage — en changer demande un
redémarrage : `OTO_DEBIT_MODE` = `report` (défaut : mesure et journalise ce qui serait
refusé, ne refuse rien) | `enforce` | `off` ; un débit par classe,
`OTO_DEBIT_<CLASSE>_PAR_MIN`, `0` désarme la classe ; `OTO_DEBIT_ADRESSES_EXEMPTEES`.
Un réglage illisible LÈVE au démarrage (`ReglageDebitInvalide`) : jamais un défaut
deviné, qui ferait tourner une instance sur une borne que personne n'a posée.

Couche applicative d'un dispositif à DEUX couches (cf. `docs/rest-api.md` §Deux couches) : le
proxy devant l'instance arrête les inondations et les corps trop gros ; ici, ce que
seule l'application sait compter (jeton, routes sensibles, budget d'échecs).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

ENFORCE, REPORT, OFF = "enforce", "report", "off"

#: Débit régulier (requêtes/min) par classe. La rafale vaut la moitié du débit : une
#: minute calme laisse passer une demi-minute d'élan. Chaque défaut se règle par sa
#: variable (`bornes_depuis_env`).
DEFAUTS: dict[str, float] = {
    "jeton": 1200.0,
    "anonyme": 600.0,
    "sensible": 60.0,
    "webhook": 300.0,
    "echecs": 120.0,
}

#: Sans jeton, ces chemins sont `sensible` : un secret dans le chemin (invitation,
#: partage public, page publique, désabonnement) ou un geste d'authentification.
_SENSIBLES_PREFIXES = ("/api/invitations/", "/api/public/", "/p/d/", "/o/u/", "/o/d/",
                       "/o/r/")
_SENSIBLES_EXACTS = frozenset({"/api/mcp/auth_callback"})
#: Tout retour OAuth d'un connecteur (`/api/<connecteur>/oauth/callback`).
_SENSIBLES_SUFFIXES = ("/oauth/callback",)

_WEBHOOKS_PREFIXES = ("/api/hooks/", "/api/receivers/")
_WEBHOOKS_EXACTS = frozenset({"/api/billing/webhook", "/api/logto/webhook"})

#: Le plafond d'une adresse sur TOUS ses crochets, en multiple du débit par crochet.
_WEBHOOK_ADRESSE_FACTEUR = 10

#: Les statuts qui disent « tu n'as pas le droit » : ils consomment le budget `echecs`
#: de l'adresse. 404 seulement là où il signifie « secret inconnu ». Rien sur `webhook`.
_ECHECS = frozenset({401, 403})
_ECHECS_SI_SECRET = frozenset({401, 403, 404})
_ECHECS_PAR_CLASSE = {"jeton": _ECHECS, "anonyme": _ECHECS,
                      "sensible": _ECHECS_SI_SECRET, "webhook": frozenset()}

_SEAUX_MAX = 50_000          # par classe ; au-delà, le plus ancien sort
_RECONNUS_MAX = 50_000
_JOURNAL_INTERVALLE_S = 60.0


@dataclass(frozen=True)
class Borne:
    par_minute: float

    @property
    def rafale(self) -> float:
        return max(1.0, self.par_minute / 2.0)

    @property
    def par_seconde(self) -> float:
        return self.par_minute / 60.0


class ReglageDebitInvalide(RuntimeError):
    """Un réglage `OTO_DEBIT_*` illisible : levé à la construction de la garde, donc au
    démarrage — l'instance ne sert pas sur un réglage qu'elle n'a pas compris."""


def mode_depuis_env() -> str:
    """`report` (défaut, absent ou vide) | `enforce` | `off`. Le défaut est `report` :
    la même image tourne sur des instances dont le proxy n'écrase pas les en-têtes
    d'adresse, et `enforce` s'y contournerait (cf. la docstring du module). Une valeur
    inconnue LÈVE : `enforced` pour `enforce` ne doit pas tourner en mesure sans bruit."""
    brut = (os.environ.get("OTO_DEBIT_MODE") or REPORT).strip().lower()
    if brut not in (ENFORCE, REPORT, OFF):
        raise ReglageDebitInvalide(
            f"OTO_DEBIT_MODE={brut!r} inconnu : `report`, `enforce` ou `off`.")
    return brut


def adresses_exemptees_depuis_env() -> tuple:
    """Les réseaux exemptés, lus UNE fois au démarrage. Une entrée illisible LÈVE :
    ni exemption devinée, ni machine à nous bornée sans qu'on le sache."""
    reseaux = []
    for brut in (os.environ.get("OTO_DEBIT_ADRESSES_EXEMPTEES") or "").split(","):
        brut = brut.strip()
        if not brut:
            continue
        try:
            reseaux.append(ipaddress.ip_network(brut, strict=False))
        except ValueError:
            raise ReglageDebitInvalide(
                f"OTO_DEBIT_ADRESSES_EXEMPTEES : {brut!r} n'est ni une adresse ni un "
                "CIDR.") from None
    return tuple(reseaux)


def _borne(nom: str, brut: Optional[str], defaut: float) -> Borne:
    """Absente ou vide : le défaut. Illisible ou négative : LÈVE."""
    if brut in (None, ""):
        return Borne(defaut)
    try:
        valeur = float(brut)
    except ValueError:
        valeur = -1.0
    if valeur < 0:
        raise ReglageDebitInvalide(
            f"{nom}={brut!r} illisible : un débit par minute positif, `0` désarme.")
    return Borne(valeur)


def bornes_depuis_env() -> dict[str, Borne]:
    """Une borne par classe, lue UNE fois (à la construction de la garde, au boot).
    Une lecture littérale par variable : l'inventaire les trouve par leur nom."""
    lues = {
        "jeton": ("OTO_DEBIT_JETON_PAR_MIN", os.environ.get("OTO_DEBIT_JETON_PAR_MIN")),
        "anonyme": ("OTO_DEBIT_ANONYME_PAR_MIN",
                    os.environ.get("OTO_DEBIT_ANONYME_PAR_MIN")),
        "sensible": ("OTO_DEBIT_SENSIBLE_PAR_MIN",
                     os.environ.get("OTO_DEBIT_SENSIBLE_PAR_MIN")),
        "webhook": ("OTO_DEBIT_WEBHOOK_PAR_MIN",
                    os.environ.get("OTO_DEBIT_WEBHOOK_PAR_MIN")),
        "echecs": ("OTO_DEBIT_ECHECS_PAR_MIN", os.environ.get("OTO_DEBIT_ECHECS_PAR_MIN")),
    }
    return {c: _borne(nom, brut, DEFAUTS[c]) for c, (nom, brut) in lues.items()}


def classer(methode: str, chemin: str, porte_jeton: bool) -> Optional[str]:
    """La classe d'une requête, ou None si elle passe sans compter (`OPTIONS`)."""
    if methode == "OPTIONS":
        return None
    if chemin in _WEBHOOKS_EXACTS or chemin.startswith(_WEBHOOKS_PREFIXES):
        return "webhook"
    if porte_jeton:
        return "jeton"
    if (chemin in _SENSIBLES_EXACTS or chemin.startswith(_SENSIBLES_PREFIXES)
            or chemin.endswith(_SENSIBLES_SUFFIXES)):
        return "sensible"
    return "anonyme"


def _jeton_valide(scope) -> bool:
    """Le bearer a-t-il été VÉRIFIÉ par la couche d'auth pendant cette requête ? Seul
    un jeton vérifié devient « reconnu » : un 2xx sur une route publique
    (`/api/version`, `/.well-known/*`…) ne vérifie rien, et reconnaître le jeton là
    laissait un scanner contourner le budget d'échecs à une requête par jeton
    inventé. REST : `api.base` publie le principal sous `oto_principal` ;
    MCP : l'`AuthenticationMiddleware` pose `scope["user"]`. Les deux écrivent dans
    le MÊME scope (Starlette le met à jour en place), relu ici après la réponse."""
    if scope.get("oto_principal"):
        return True
    return bool(getattr(scope.get("user"), "is_authenticated", False))


def _empreinte(valeur: bytes) -> str:
    return hashlib.sha256(valeur).hexdigest()[:32]


def _empreinte_refus(classe: str, cle) -> str:
    """La clé d'un refus pour le palmarès (`limite_refus_top`) : celle de `jeton` est
    déjà une empreinte (gardée telle quelle, pour qu'elle recoupe le journal) ; une
    adresse, ou (adresse, chemin) d'un crochet, est hachée ici."""
    if classe == "jeton":
        return cle
    return _empreinte((" ".join(cle) if isinstance(cle, tuple) else cle).encode())


class _Seaux:
    """Seaux à jetons d'une classe : clé -> (jetons, instant). Remplissage continu,
    plafonné à la rafale ; borné en nombre de clés (le plus ancien sort)."""

    def __init__(self, borne: Borne) -> None:
        self.borne = borne
        self.etat: dict = {}

    def _remplir(self, cle, maintenant: float) -> float:
        jetons, dernier = self.etat.get(cle, (self.borne.rafale, maintenant))
        return min(self.borne.rafale,
                   jetons + max(0.0, maintenant - dernier) * self.borne.par_seconde)

    def _poser(self, cle, jetons: float, maintenant: float) -> None:
        self.etat.pop(cle, None)          # réinsérée en queue : l'éviction vise l'inactif
        self.etat[cle] = (jetons, maintenant)
        while len(self.etat) > _SEAUX_MAX:
            del self.etat[next(iter(self.etat))]

    def usage_pct(self, jetons: float) -> float:
        """La part de la rafale consommée : 100 = la clé est à sec."""
        return max(0.0, min(100.0, (1.0 - jetons / self.borne.rafale) * 100.0))

    def attente(self, jetons: float) -> int:
        """Secondes avant qu'un jeton soit rendu (au moins 1)."""
        return max(1, math.ceil((1.0 - jetons) / self.borne.par_seconde))

    def prendre(self, cle, maintenant: float,
                debiter: bool = True) -> tuple[Optional[int], float]:
        """(None, usage) = passe (un jeton pris si `debiter`) ; sinon (attente, usage)."""
        jetons = self._remplir(cle, maintenant)
        if jetons < 1.0:
            self._poser(cle, jetons, maintenant)
            return self.attente(jetons), 100.0
        reste = jetons - 1.0 if debiter else jetons
        self._poser(cle, reste, maintenant)
        return None, self.usage_pct(reste)

    def debiter(self, cle, maintenant: float) -> float:
        """Consomme un jeton sans jamais passer sous zéro (un échec constaté) ; rend
        l'usage qui en résulte."""
        reste = max(0.0, self._remplir(cle, maintenant) - 1.0)
        self._poser(cle, reste, maintenant)
        return self.usage_pct(reste)


class LimiteDebit:
    """Middleware ASGI : borne le débit de chaque requête HTTP selon sa classe, plus un
    budget d'échecs d'authentification par adresse, et en mesure l'usage. Compteurs
    propres à l'instance, donc au processus. Cf. la docstring du module."""

    def __init__(self, app, mode: Optional[str] = None,
                 bornes: Optional[dict[str, Borne]] = None, horloge=time.monotonic,
                 exemptees: Optional[tuple] = None, horloge_murale=time.time,
                 publier=None, publier_refus=None):
        from . import limite_debit_mesure, limite_refus_top
        from .subdomain_project import _client_ip
        self.app = app
        self._ip = _client_ip
        self.mode = mode or mode_depuis_env()
        bornes = bornes or bornes_depuis_env()
        self._seaux = {c: _Seaux(b) for c, b in bornes.items() if b.par_minute > 0}
        if "webhook" in self._seaux:
            self._seaux["webhook_adresse"] = _Seaux(
                Borne(bornes["webhook"].par_minute * _WEBHOOK_ADRESSE_FACTEUR))
        self._exemptees = (adresses_exemptees_depuis_env() if exemptees is None
                           else tuple(ipaddress.ip_network(e, strict=False)
                                      for e in exemptees))
        self._reconnus: dict[str, float] = {}
        self._journalise: dict = {}
        self._horloge = horloge
        self._horloge_murale = horloge_murale
        self._mesure = limite_debit_mesure.Mesure(self.mode)
        self._publier = publier or limite_debit_mesure.publier
        # QUI serait refusé (#1232) : le palmarès des clés hachées, par jour.
        self._palmares = limite_refus_top.Palmares(limite_refus_top.DEBIT)
        self._palmares_processus = limite_refus_top.palmares_du_processus()
        self._publier_refus = publier_refus or limite_refus_top.publier
        self._par_minute = {c: s.borne.par_minute for c, s in self._seaux.items()}

    def _est_exemptee(self, ip: str) -> bool:
        if not self._exemptees:
            return False
        try:
            adresse = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(adresse in reseau for reseau in self._exemptees)

    def _basculer_minute(self) -> None:
        """Ferme la minute murale écoulée : ses lignes partent en tâche de fond, au plus
        une par classe — jamais sur le chemin d'une décision."""
        murale = self._horloge_murale()
        lignes = self._mesure.basculer(int(murale // 60), self._par_minute)
        if lignes:
            self._publier(lignes)
        # Les palmarès des refus aussi — ceux du processus compris (clé plateforme,
        # bornes de `bornes_processus`, budget d'unités), qui sinon n'écriraient leur
        # minute close qu'à leur prochain signal.
        for palmares in (self._palmares, *self._palmares_processus):
            self._publier_refus(palmares.basculer(murale))

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or self.mode == OFF:
            return await self.app(scope, receive, send)

        entetes = scope.get("headers") or []
        autorisation = next((v for k, v in entetes if k == b"authorization" and v), None)
        chemin = scope.get("path") or ""
        classe = classer(scope.get("method", ""), chemin, autorisation is not None)
        if classe is None:
            return await self.app(scope, receive, send)

        self._basculer_minute()
        ip = self._ip(scope, dict(entetes))
        exemptee = self._est_exemptee(ip)
        jeton = _empreinte(autorisation) if autorisation is not None else None
        maintenant = self._horloge()

        refus = self._decider(classe, ip, exemptee, jeton, chemin, maintenant)
        if refus is not None:
            classe_refus, cle, attente = refus
            self._mesure.noter_decision(
                classe_refus, "refused" if self.mode == ENFORCE else "would_refuse")
            self._publier_refus(self._palmares.noter(
                classe_refus, _empreinte_refus(classe_refus, cle), self.mode,
                self._horloge_murale()))
            self._journaliser(classe_refus, cle, ip, chemin, maintenant)
            if self.mode == ENFORCE:
                return await self._refuser(scope, send, classe_refus, attente)
        else:
            self._mesure.noter_decision(classe, "allowed")

        echecs = frozenset() if exemptee else _ECHECS_PAR_CLASSE[classe]

        async def envoyer(message):
            if message.get("type") == "http.response.start":
                self._constater(int(message.get("status") or 0), echecs, ip, jeton,
                                valide=_jeton_valide(scope))
            await send(message)

        await self.app(scope, receive, envoyer)

    def _decider(self, classe: str, ip: str, exemptee: bool, jeton: Optional[str],
                 chemin: str, maintenant: float):
        """None = passe ; sinon (classe qui refuse, clé, attente). Le budget d'échecs se
        juge d'abord et ne consomme rien : seul un échec CONSTATÉ le débite."""
        echecs = self._seaux.get("echecs")
        if (echecs is not None and not exemptee and classe in ("jeton", "sensible")
                and (jeton is None or jeton not in self._reconnus)):
            attente, _ = echecs.prendre(ip, maintenant, debiter=False)
            if attente is not None:
                return "echecs", ip, attente
        if classe != "jeton" and exemptee:
            return None
        if classe == "webhook" and "webhook_adresse" in self._seaux:
            attente, usage = self._seaux["webhook_adresse"].prendre(ip, maintenant)
            self._mesure.noter_usage("webhook_adresse", usage)
            if attente is not None:
                return "webhook_adresse", ip, attente
        seaux = self._seaux.get(classe)
        if seaux is None:
            return None
        cle = {"jeton": jeton, "webhook": (ip, chemin)}.get(classe, ip)
        attente, usage = seaux.prendre(cle, maintenant)
        self._mesure.noter_usage(classe, usage)
        return None if attente is None else (classe, cle, attente)

    def _constater(self, statut: int, echecs: frozenset, ip: str,
                   jeton: Optional[str], *, valide: bool = False) -> None:
        maintenant = self._horloge()
        reconnu = jeton is not None and jeton in self._reconnus
        if statut in echecs and not reconnu:
            seaux = self._seaux.get("echecs")
            if seaux is not None:
                self._mesure.noter_usage("echecs", seaux.debiter(ip, maintenant))
        elif jeton is not None and statut < 400 and valide:
            self._reconnus.pop(jeton, None)
            self._reconnus[jeton] = maintenant
            while len(self._reconnus) > _RECONNUS_MAX:
                del self._reconnus[next(iter(self._reconnus))]

    def _journaliser(self, classe: str, cle, ip: str, chemin: str,
                     maintenant: float) -> None:
        """Une ligne par (classe, clé) et par minute au plus — jamais le jeton, son
        empreinte tronquée seulement."""
        marque = (classe, cle)
        if maintenant - self._journalise.get(marque, -_JOURNAL_INTERVALLE_S) \
                < _JOURNAL_INTERVALLE_S:
            return
        self._journalise.pop(marque, None)
        self._journalise[marque] = maintenant
        while len(self._journalise) > _SEAUX_MAX:
            del self._journalise[next(iter(self._journalise))]
        qui = f"jeton {cle[:8]}…" if classe == "jeton" else f"adresse {ip}"
        logger.warning("débit %s : %s dépasse la borne de la classe %s (%s/min) sur %s",
                       "refusé" if self.mode == ENFORCE else "serait refusé (report)",
                       qui, classe, int(self._seaux[classe].borne.par_minute), chemin)

    async def _refuser(self, scope, send, classe: str, attente: int) -> None:
        par_minute = int(self._seaux[classe].borne.par_minute)
        await envoyer_refus(scope, send, 429, {
            "error": "rate_limited",
            "detail": (f"trop de requêtes (classe {classe} : au plus {par_minute} par "
                       f"minute). Réessaie dans {attente} s."),
            "details": {"retryable": True, "retry_after_seconds": attente,
                        "limit_per_minute": par_minute, "scope": classe},
        }, entetes=[(b"retry-after", str(attente).encode())])


async def envoyer_refus(scope, send, statut: int, enveloppe: dict, *,
                        entetes=()) -> None:
    """Un refus servi AU-DESSUS de l'application (débit, borne de corps) : l'enveloppe
    d'erreur ordinaire (`{error, detail, details}`), avec les en-têtes CORS de
    l'origine et `X-Oto-Version` — ces couches sont au-dessus de celles qui les posent."""
    from . import version, version_header
    from .api.base import _cors_headers
    corps = json.dumps(enveloppe).encode()
    origine = next((v.decode("latin-1") for k, v in scope.get("headers") or []
                    if k == b"origin"), None)
    tete = [(b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(corps)).encode()), *entetes,
            (b"x-oto-version", version_header.valeur_entete(version.version_servie()))]
    tete += [(k.lower().encode(), v.encode())
             for k, v in _cors_headers(origine).items()]
    await send({"type": "http.response.start", "status": statut, "headers": tete})
    await send({"type": "http.response.body", "body": corps})
