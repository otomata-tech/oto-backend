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

# probe(fields, config) -> None : lève une exception sur échec d'authentification (son
# message est rendu au client). Sync OU async (la capacité awaite si besoin). `fields` =
# champs DÉCHIFFRÉS du credential (client_id/secret/refresh_token/data_center pour zoho) ;
# `config` = satellites NON-secrets appariés à la clé gagnante (meta public : dsn
# unipile…). Une sonde qui parle à un endpoint dont l'hôte dépend de la clé (unipile,
# tenant BYO) DOIT lire `config`, sinon elle teste la clé contre le mauvais tenant.
Probe = Callable[[dict, dict], Union[None, Awaitable[None]]]

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
    return _COUVERTURE.get(connector)


def supports(connector: str) -> bool:
    return connector in _REGISTRY


def probe_for(connector: str) -> Optional[Probe]:
    return _REGISTRY.get(connector)


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
            await probe(fields, config or {}, **kwargs)
            return
        # Une sonde sync part au thread. Une sonde qui n'est pas déclarée `async
        # def` mais rend un awaitable (callable, partial) traverse aussi : la
        # créer dans un thread ne l'exécute pas, on l'attend ensuite ici.
        res = await asyncio.to_thread(probe, fields, config or {}, **kwargs)
        if inspect.isawaitable(res):
            await res

    try:
        await asyncio.wait_for(_joue(), timeout=_BORNE_S)
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
    probe = _REGISTRY.get(connector)
    if probe is None:
        return
    await executer(probe, fields, config, instance)
