"""Ce qu'une résolution REND : le credential gagnant, son origine, sa config (ADR 0024).

Extrait de `resolve.py` le 2026-08-29 (cliquet des 500 lignes, #584) : c'est le type
que TOUTES les voies de résolution produisent — le chemin identifié (`resolve`), le
chemin anonyme (`resolve_anon`), l'instance épinglée — et il ne dépend d'aucune
d'elles. Le mettre en bas du package est ce qui permet à ces voies d'être des
modules frères sans cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .. import credentials_store, providers
from . import secret_repr


@dataclass(frozen=True)
class ResolvedCredential:
    """Credential GAGNANT de la cascade (ADR 0024) — la clé, son origine, ET sa
    config non-secrète (endpoint/host) en un seul objet. Source unique : toute
    résolution (clé seule, multi-champs, ou endpoint) en dérive.

    - `secret` : la valeur stockée brute (la clé pour un keyed ; le pack JSON pour
      un multi-champs). `key` = alias (un keyed s'instancie avec).
    - `is_platform` / `mode` : origine (user|group|org|tenant|platform) — miroir de
      `status_for`.
    - `fields` (lazy) : champs unpackés (un client multi-secrets s'instancie avec).
    - `config` (lazy) : champs NON-secrets déclarés (data_center, base_url…) ∪ `meta`
      public du credential (ex. `dsn` unipile). La config voyage avec la clé.
    - `entity_type`/`entity_id` : niveau gagnant (None pour un grant plateforme — sa
      config est l'environnement, pas un credential du coffre)."""
    provider: str
    secret: str
    is_platform: bool
    mode: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    account: str = ""

    def __repr__(self) -> str:
        # La clé ne sort JAMAIS par le repr (#564) — cf. `secret_repr`.
        return secret_repr.expurge(self, "secret")

    @property
    def key(self) -> str:
        return self.secret

    @property
    def fields(self) -> dict:
        return credentials_store.unpack_secret(providers.credential_provider(self.provider), self.secret)

    @property
    def config(self) -> dict:
        """Config non-secrète appariée à la clé gagnante. Lazy : aucun coût pour
        les appelants qui ne lisent que `key` (chemin chaud resolve_api_key)."""
        porteur = providers.credential_provider(self.provider)
        _, cfg = credentials_store.split_secret_config(porteur, self.fields)
        if self.entity_type is not None:
            try:
                row = credentials_store.get_credential_with_meta(
                    self.entity_type, self.entity_id, porteur, self.account)
            # noqa: SILENT — config non-secrète absente ⇒ la clé gagnante reste utilisable
            except Exception:
                row = None
            if row:
                cfg = {**cfg, **credentials_store.public_meta(row.get("meta"))}
        return cfg
