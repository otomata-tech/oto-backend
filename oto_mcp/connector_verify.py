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

from typing import Awaitable, Callable, Optional, Union

# probe(fields, config) -> None : lève une exception sur échec d'authentification (son
# message est rendu au client). Sync OU async (la capacité awaite si besoin). `fields` =
# champs DÉCHIFFRÉS du credential (client_id/secret/refresh_token/data_center pour zoho) ;
# `config` = satellites NON-secrets appariés à la clé gagnante (meta public : dsn/
# api_version unipile…). Une sonde qui parle à un endpoint VERSIONNÉ (unipile v1/v2) DOIT
# lire `config`, sinon elle teste la clé contre le mauvais tenant (401 sur une clé valide).
Probe = Callable[[dict, dict], Union[None, Awaitable[None]]]

_REGISTRY: dict[str, Probe] = {}


def register(connector: str, probe: Probe) -> None:
    """Déclare la sonde de vérification d'un connecteur (appelé au chargement du module)."""
    _REGISTRY[connector] = probe


def supports(connector: str) -> bool:
    return connector in _REGISTRY


def probe_for(connector: str) -> Optional[Probe]:
    return _REGISTRY.get(connector)
