"""DÉPRÉCIÉ — ré-export de `providers.py` (ADR 0010, barreau 0).

L'axe **connexion/credential** a été extrait dans `providers.py` (renommage pur,
no-behavior-change). Ce module reste un shim le temps de basculer les imports
(`from . import connectors` → `from . import providers`). **Ne rien ajouter ici :
éditer `providers.py`.** Suivi : otomata#24.
"""
from __future__ import annotations

from .providers import (  # noqa: F401  (ré-export rétrocompat, ADR 0010)
    Connector,
    REGISTRY,
    KEY_PROVIDERS,
    ORG_SHAREABLE_PROVIDERS,
    QUOTA_DEFAULTS,
    DEFAULT_BUNDLE,
    DEFAULT_PRESET,
    DEFAULT_HIDDEN_NAMESPACES,
    REMOTE_CONNECTORS,
    MOUNT_CONNECTORS,
    connector_for_provider,
    connector_for_namespace,
    is_keyed,
    require_keyed,
    require_credential,
    is_byo_user,
    is_org_shareable,
    org_secret_meta,
    public_catalog,
)
