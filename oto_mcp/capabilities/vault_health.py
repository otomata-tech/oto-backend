"""Capacité admin : santé du coffre de credentials (#72, ADR 0009).

Scanne `connector_credentials` et remonte les lignes NON déchiffrables (chiffrées
avec une master key périmée → `InvalidTag`, cf. mémoire `vault_stale_key_corruption`),
par connecteur et par type d'entité — pour cibler les re-poses SANS SSH + SQL manuel.

Lecture seule, `PLATFORM_ADMIN` (supervision plateforme). Ne renvoie JAMAIS de
plaintext : le déchiffrement sert de test booléen (ok/ko), la valeur est jetée
(cf. `credentials_store.scan_vault_health`). Miroir du modèle « état sans secret »
de `credential_status`.
"""
from __future__ import annotations

from pydantic import BaseModel

from .. import credentials_store
from ._authz import PLATFORM_ADMIN
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class VaultHealthInput(BaseModel):
    pass


def _vault_health(ctx: ResolvedCtx, inp: VaultHealthInput) -> dict:
    try:
        return credentials_store.scan_vault_health()
    except RuntimeError as e:
        # master key absente du process → scan impossible (sinon 100 % faux positifs).
        raise AuthzDenied(503, "vault_scan_unavailable", str(e))


CAPABILITIES += [
    Capability(
        key="admin.vault_health", handler=_vault_health, Input=VaultHealthInput,
        authz=PLATFORM_ADMIN,
        description=(
            "[platform admin] Vault health scan: lists credential rows that FAIL to "
            "decrypt (encrypted with a stale master key — InvalidTag — or corrupted "
            "envelope), grouped by connector and by entity type (member/user/org/"
            "group/platform), plus the offending rows by identity only (entity_type/"
            "entity_id/connector/account — never any secret). Use it to target "
            "re-connections after a master-key rotation. Read-only."),
        mcp="oto_admin_vault_health",
        rest=RestBinding("GET", "/api/admin/vault/health"),
    ),
]
