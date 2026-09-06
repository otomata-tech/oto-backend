"""Les SECRETS partagés d'une org — pose, lecture, révocation.

Simple façade au grain org du coffre chiffré `credentials_store`
(`entity_type='org'`) : aucune table locale, aucun secret en clair en colonne.
La garde d'éligibilité (`byo_org`) est lue au registre `connectors`.

Feuille du package : n'importe aucun de ses frères.
"""
from __future__ import annotations

from typing import Optional

from .. import providers
from .. import credentials_store


def get_org_secret(org_id: int, provider: str, account: str = "") -> Optional[str]:
    """Clé du secret partagé `provider` possédé par l'org, ou None. `account`
    discrimine le multi-compte au palier org ('' = mono legacy).

    La restriction aux providers org-partageables (`byo_org`) est portée par la
    couche access et le write-path (`org_secret_meta`).

    Lit le coffre chiffré `connector_credentials` (entité 'org', déchiffre).
    """
    return credentials_store.get_credential("org", str(org_id), provider, account)


def has_org_secret(org_id: int, provider: str) -> bool:
    """Présence d'un org_secret SANS le déchiffrer (status_for)."""
    return credentials_store.has_credential("org", str(org_id), provider)


def set_org_secret(org_id: int, provider: str, api_key: str, set_by: Optional[str] = None,
                   meta: Optional[dict] = None, account: str = "") -> None:
    """Pose/rote le secret partagé `provider` de l'org. `provider` validé comme
    org-partageable (byo_org : exclut slack/linkedin, inclut les remotes org-only) via
    le registre — plus restrictif que KEY_PROVIDERS puisqu'un remote n'est pas keyed.
    `meta` : satellites non-secrets (ex. `base_url` du bridge d'un connecteur
    remote, ADR 0003).

    Un connecteur **remote** (ADR 0003/0011) est défini par la DONNÉE : un `meta`
    avec `base_url` (endpoint du bridge) ⇒ pas d'entrée registre attendue (zéro nom
    client en dur, cf. `providers.org_secret_meta`). Sinon, garde d'éligibilité
    org-partageable via le registre."""
    if not (meta and meta.get("base_url")):
        providers.require_credential("org", provider)
    if not api_key:
        raise ValueError("api_key requise")
    # Coffre chiffré, source unique (entité 'org').
    credentials_store.set_credential("org", str(org_id), provider, api_key, set_by=set_by, meta=meta, account=account)


def delete_org_secret(org_id: int, provider: str, account: str = "") -> bool:
    return credentials_store.clear_credential("org", str(org_id), provider, account=account)


def list_org_secrets(org_id: int) -> list[dict]:
    """Providers posés sur l'org — SANS l'api_key (jamais exposée via API).
    Lit le coffre (entité 'org'). `base_url` exposé pour les connecteurs
    remote (satellite non-secret dans `meta`)."""
    out: list[dict] = []
    for c in credentials_store.list_credentials("org", str(org_id)):
        entry = {"provider": c["connector"], "set_by": c["set_by"], "set_at": c["set_at"]}
        base_url = (c.get("meta") or {}).get("base_url")
        if base_url:
            entry["base_url"] = base_url
        out.append(entry)
    return out
