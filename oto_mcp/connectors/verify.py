"""Sonde de credential par connecteur — « tester la connexion » (framework générique).

Un credential keyé/multi-champs (Zoho, Silae…) peut être POSÉ mais ne pas authentifier
(mauvais data center, refresh token périmé…). `credentials_store.credential_status` ne
dit que « posée / pas posée » (`secret_enc IS NOT NULL`), jamais « ça authentifie ».
Chaque connecteur peut enregistrer une **sonde** : un appel SANS effet de bord qui, à
partir des champs déchiffrés, vérifie que le credential authentifie réellement et LÈVE
sur échec (le message d'exception = le retour d'erreur remonté à l'UI).

Patron identique à `browser_session.register` / `connector_identities.register` : la
logique vit dans le module `tools/<name>.py` du connecteur (qui appelle `register()` à
son chargement) ; la SURFACE (capacité MCP+REST) est déclarée une seule fois dans
`capabilities/connectors/verify.py`.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Awaitable, Callable, Optional, Union

from .. import providers

# probe(fields, config) -> None : lève une exception sur échec d'authentification (son
# message est rendu au client). Sync OU async (la capacité awaite si besoin). `fields` =
# champs DÉCHIFFRÉS du credential (client_id/secret/refresh_token/data_center pour zoho) ;
# `config` = satellites NON-secrets appariés à la clé gagnante (meta public : dsn
# unipile…). Une sonde qui parle à un endpoint dont l'hôte dépend de la clé (unipile,
# tenant BYO) DOIT lire `config`, sinon elle teste la clé contre le mauvais tenant.
Probe = Callable[[dict, dict], Union[None, dict, Awaitable[Union[None, dict]]]]

#: Ce qu'une sonde peut RENDRE, en plus de lever sur échec : un dict de mesures.
#: Aujourd'hui le solde, pour les sondes `auth+quota` — `{"quota": {...}}`.
#:
#: ⚠️ Rendre est FACULTATIF et le restera : les sondes qui ne mesurent qu'une
#: authentification rendent `None`, comme avant. Exiger un retour de toutes aurait
#: obligé à inventer une forme vide pour la quinzaine qui n'a rien à dire — et une
#: forme vide finit par se lire comme une mesure à zéro.
_CLES_DE_MESURE = ("quota",)


def _mesures(rendu) -> dict:
    """Ce que la sonde a mesuré, réduit aux clés connues. Un rendu qui n'est pas un
    dict — le cas de toutes les sondes `auth` — vaut « rien mesuré », jamais une
    erreur : c'est le contrat d'avant, et il doit continuer de passer."""
    if not isinstance(rendu, dict):
        return {}
    return {k: rendu[k] for k in _CLES_DE_MESURE if k in rendu}

#: Ce qu'une sonde COUVRE. Déclaré, jamais deviné (oto#57).
#:
#: Deux sondes voisines faisaient des choses différentes et rien ne les distinguait vu
#: de l'extérieur : celle de `theirstack` lit le SOLDE (l'appel authentifié gratuit),
#: celle d'`origami` liste des objets — ce qui répond parfaitement sur un compte à sec.
#: Mesuré le 04/09/2026 : un préflight tout vert, puis 402 après quatre espaces, quatre
#: tables et 28 lignes créés.
#:
#: ⚠️ **Le préflight n'avait pas menti** : il avait rapporté un vert qui ne voulait pas
#: dire ce qu'on croyait. C'est la nuance qui décide du remède — il ne faut pas plus de
#: sondes, il faut qu'une sonde DISE CE QU'ELLE COUVRE, pour qu'un vert dise ce qu'il
#: vaut et qu'un appelant sache ce qu'il ne sait pas.
#: Les VERDICTS d'une sonde. Trois états qui appellent des conduites OPPOSÉES, là où
#: un booléen n'en distinguait aucune (oto#57) :
#:   `ok`           — ça marche.
#:   `unauthorized` — la clé n'autorise pas : la remplacer ou élargir son périmètre.
#:                    En poser une de plus ne changera rien.
#:   `no_quota`     — la clé est bonne, le solde est vide : recharger.
#:   `unknown`      — la sonde a échoué sans qu'on puisse classer. ⚠️ Se lit « je ne
#:                    sais pas », JAMAIS « rien de grave » : c'est la valeur la plus
#:                    fréquente aujourd'hui, et la confondre avec un diagnostic ferait
#:                    exactement le mal que ce lot répare.
OK = "ok"
UNAUTHORIZED = "unauthorized"
NO_QUOTA = "no_quota"
UNKNOWN = "unknown"
VERDICTS = (OK, UNAUTHORIZED, NO_QUOTA, UNKNOWN)


class SondeRefusee(Exception):
    """Une sonde qui SAIT pourquoi elle échoue le dit en levant l'un des deux
    ci-dessous. C'est la voie explicite, préférée au classement par code HTTP : elle
    survit à un amont qui répondrait 200 avec un corps d'erreur."""


class NonAutorise(SondeRefusee):
    """La clé n'autorise pas — invalide, révoquée, ou périmètre insuffisant."""


class QuotaEpuise(SondeRefusee):
    """La clé authentifie, il n'y a plus rien à dépenser."""


#: Les codes amont qui classent un échec quand la sonde n'a rien dit d'elle-même.
#: ⚠️ Lus sur `status_code` (`UpstreamHTTPError`), **jamais devinés sur le texte** d'un
#: message : un classement bâti sur des mots change de sens au premier reformatage
#: amont, et personne ne s'en aperçoit.
_PAR_CODE = {401: UNAUTHORIZED, 403: UNAUTHORIZED, 402: NO_QUOTA, 429: NO_QUOTA}


def classer(erreur: BaseException) -> str:
    """Le verdict d'un échec de sonde. `unknown` quand rien ne permet de trancher —
    et c'est un verdict à part entière, pas un défaut."""
    if isinstance(erreur, QuotaEpuise):
        return NO_QUOTA
    if isinstance(erreur, NonAutorise):
        return UNAUTHORIZED
    code = getattr(erreur, "status_code", None)
    if isinstance(code, int):
        return _PAR_CODE.get(code, UNKNOWN)
    return UNKNOWN


#: La conduite à tenir, par verdict. Un diagnostic qui ne dit pas quoi faire renvoie
#: chercher — et c'est ainsi qu'une personne a relancé six fois une connexion valide.
CONDUITE = {
    UNAUTHORIZED: ("la clé n'autorise pas cet appel — remplace-la, ou élargis son "
                   "périmètre chez le fournisseur. En poser une de PLUS ne changera "
                   "rien."),
    NO_QUOTA: ("la clé est bonne : c'est le solde qui est vide. Recharge le compte "
               "chez le fournisseur — inutile de reconnecter quoi que ce soit."),
    UNKNOWN: ("le test a échoué sans dire pourquoi. Lis `error` tel quel : il vient "
              "du fournisseur, et c'est la seule chose qu'on sache."),
}

AUTH = "auth"                 # la clé authentifie. Ne dit RIEN du solde.
AUTH_QUOTA = "auth+quota"     # la clé authentifie ET il reste de quoi travailler.
COUVERTURES = (AUTH, AUTH_QUOTA)

_REGISTRY: dict[str, Probe] = {}
_COUVERTURE: dict[str, str] = {}


def register(connector: str, probe: Probe, couvre: str = AUTH) -> None:
    """Déclare la sonde de vérification d'un connecteur (appelé au chargement du module).

    `couvre` par DÉFAUT `auth` — le défaut prudent : une sonde ne prouve que ce qu'elle
    a mesuré, et déclarer `auth+quota` sans lire un solde ferait exactement le vert
    trompeur qu'on cherche à supprimer. Ne le monter que si la sonde lit vraiment un
    solde ou un quota."""
    if couvre not in COUVERTURES:
        raise ValueError(f"couverture inconnue {couvre!r} — attendu {COUVERTURES}")
    _REGISTRY[connector] = probe
    _COUVERTURE[connector] = couvre


def couverture(connector: str) -> Optional[str]:
    """Ce que la sonde de ce connecteur couvre, ou `None` s'il n'en a pas.

    ⚠️ `None` se lit « aucune sonde », jamais « ne couvre rien » — les deux appellent
    des conduites différentes : dans un cas on ne peut pas mesurer, dans l'autre on a
    mesuré l'authentification seule."""
    return _COUVERTURE.get(_porteur(connector))


def _porteur(connector: str) -> str:
    """Le connecteur qui PORTE la clé (délégation `Connector.credential_of`), même
    normalisation que le walker de cascade (`access/cascade.py::walk_cascade`).

    Six canaux Unipile (`linkedin_unipile`, `whatsapp`…) n'ont pas de sonde À EUX —
    ils empruntent celle de `unipile`, enregistrée sous CE nom. Sans cette lecture
    normalisée, chacun répondait `verify_unavailable` malgré une sonde qui teste
    exactement leur clé (oto#69) : six trous qui n'en étaient pas un, tenus par le
    même bug que celui que corrige la cascade pour la résolution de credential."""
    return providers.credential_provider(connector)


def supports(connector: str) -> bool:
    return _porteur(connector) in _REGISTRY


def probe_for(connector: str) -> Optional[Probe]:
    return _REGISTRY.get(_porteur(connector))


# Borne de temps d'UNE sonde, alignée sur celle du bouton « tester » de
# `capabilities/tools_me.py`. Les sondes ont des délais d'attente très inégaux —
# 20 s ici, 120 s de lecture chez Unipile — et aucune n'a de raison de faire
# patienter un humain plus longtemps que ça.
_BORNE_S = 45.0


async def executer(probe: Probe, fields: dict, config: Optional[dict] = None,
                   instance: Optional[tuple] = None) -> None:
    """Exécute UNE sonde HORS de la boucle d'événements, sous une borne de temps.

    Point unique d'exécution des 34 sondes (oto-backend#867, lot 2). Elles sont
    presque toutes synchrones et font du HTTP : appelées nûment depuis un handler
    `async def`, elles bloquent tout le processus — MCP, REST et sondes de veille —
    le temps que l'amont réponde. Le seam des capacités ne protège que les
    handlers `def` ; par la porte `async` il ne protège rien, et c'est par là que
    ces trois entrées passent.

    La règle était écrite en double, ici et dans la capacité, avec la même
    résolution de signature : les deux appelaient la sonde à leur façon. Elle
    vit maintenant à un seul endroit, sinon corriger l'une laisse l'autre.

    Rend les MESURES de la sonde (aujourd'hui `{"quota": …}`), ou `{}` si elle
    n'en a pas — ce qui est le cas de toutes les sondes `auth`.

    ⚠️ La borne libère la BOUCLE, elle n'interrompt pas le thread : un client
    HTTP synchrone n'est pas annulable, et le thread vit jusqu'à ce que son
    propre délai d'attente expire. Ce qui compte est tenu — le processus répond,
    et l'appelant reçoit une erreur nommée au lieu d'attendre.
    """
    kwargs = {}
    # `instance` = (entity_type, entity_id, account) de la clé RÉELLEMENT sondée.
    # Indispensable dès qu'une sonde a un effet de bord sur le credential : sous
    # rotation (Salesforce RTR), sonder CONSOMME le jeton, et le remplaçant doit
    # être réécrit sur la bonne ligne. Sans cette information, la sonde ne peut
    # que deviner via la cascade — qui désigne la clé la plus proche, pas celle
    # qu'on teste. Un `verify level=org` tuait ainsi le jeton d'org en le
    # rafraîchissant : `ok:true`, puis mort. Vécu 03/08. Passé UNIQUEMENT aux
    # sondes qui le déclarent : les ~15 autres gardent leur signature à deux
    # arguments.
    if instance is not None and "instance" in inspect.signature(probe).parameters:
        kwargs["instance"] = instance

    async def _joue():
        if inspect.iscoroutinefunction(probe):
            return await probe(fields, config or {}, **kwargs)
        # Une sonde sync part au thread. Une sonde qui n'est pas déclarée `async
        # def` mais rend un awaitable (callable, partial) traverse aussi : la
        # créer dans un thread ne l'exécute pas, on l'attend ensuite ici.
        res = await asyncio.to_thread(probe, fields, config or {}, **kwargs)
        if inspect.isawaitable(res):
            return await res
        return res

    try:
        return _mesures(await asyncio.wait_for(_joue(), timeout=_BORNE_S))
    except asyncio.TimeoutError as e:
        raise TimeoutError(
            f"le test de connexion n'a pas répondu en {int(_BORNE_S)} s — "
            "le service distant est lent ou injoignable. Le credential n'est pas "
            "invalide pour autant : réessayer plus tard.") from e


async def run(connector: str, fields: dict, config: Optional[dict] = None,
              instance: Optional[tuple] = None) -> None:
    """Exécute la sonde du connecteur si elle existe (await si async) ; LÈVE
    l'exception de la sonde sur échec d'authentification, no-op si aucune sonde n'est
    enregistrée. Helper partagé entre la capacité `connectors.verify` (qui traduit
    l'exception en `{ok:false}`) et le verify-avant-persist de `api_key_save` (#106,
    qui la traduit en 400 et n'écrit pas le credential)."""
    probe = probe_for(connector)
    if probe is None:
        return
    return await executer(probe, fields, config, instance)
