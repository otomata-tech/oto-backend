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
`capabilities/connectors_verify.py`.
"""
from __future__ import annotations

import inspect
from typing import Awaitable, Callable, Optional, Union

# probe(fields, config) -> None : lève une exception sur échec d'authentification (son
# message est rendu au client). Sync OU async (la capacité awaite si besoin). `fields` =
# champs DÉCHIFFRÉS du credential (client_id/secret/refresh_token/data_center pour zoho) ;
# `config` = satellites NON-secrets appariés à la clé gagnante (meta public : dsn
# unipile…). Une sonde qui parle à un endpoint dont l'hôte dépend de la clé (unipile,
# tenant BYO) DOIT lire `config`, sinon elle teste la clé contre le mauvais tenant.
Probe = Callable[[dict, dict], Union[None, Awaitable[None]]]

_REGISTRY: dict[str, Probe] = {}


def register(connector: str, probe: Probe) -> None:
    """Déclare la sonde de vérification d'un connecteur (appelé au chargement du module)."""
    _REGISTRY[connector] = probe


def supports(connector: str) -> bool:
    return connector in _REGISTRY


def probe_for(connector: str) -> Optional[Probe]:
    return _REGISTRY.get(connector)


async def run(connector: str, fields: dict, config: Optional[dict] = None) -> None:
    """Exécute la sonde du connecteur si elle existe (await si async) ; LÈVE
    l'exception de la sonde sur échec d'authentification, no-op si aucune sonde n'est
    enregistrée. Helper partagé entre la capacité `connectors.verify` (qui traduit
    l'exception en `{ok:false}`) et le verify-avant-persist de `api_key_save` (#106,
    qui la traduit en 400 et n'écrit pas le credential)."""
    probe = _REGISTRY.get(connector)
    if probe is None:
        return
    res = probe(fields, config or {})
    if inspect.isawaitable(res):
        await res
