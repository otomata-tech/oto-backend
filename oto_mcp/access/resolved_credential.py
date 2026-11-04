"""What a resolution RETURNS: the winning credential, its origin, its config (ADR 0024).

Extracted from `resolve.py` on 2026-08-29 (500-line ratchet, #584): this is the type
that ALL resolution paths produce — the identified path (`resolve`), the anonymous
path (`resolve_anon`), the pinned instance — and it depends on none of them.
Placing it at the bottom of the package is what lets those paths be sibling
modules without a cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .. import credentials_store, providers
from . import secret_repr


@dataclass(frozen=True)
class ResolvedCredential:
    """WINNING credential of the cascade (ADR 0024) — the key, its origin, AND its
    non-secret config (endpoint/host) in a single object. Single source: every
    resolution (key only, multi-field, or endpoint) derives from it.

    - `secret`: the raw stored value (the key for a keyed provider; the JSON pack for
      a multi-field one). `key` = alias (a keyed client is instantiated with it).
    - `is_platform` / `mode`: origin (user|group|org|tenant|platform) — mirror of
      `status_for`.
    - `fields` (lazy): unpacked fields (a multi-secret client is instantiated with them).
    - `config` (lazy): declared NON-secret fields (data_center, base_url…) ∪ the
      credential's public `meta` (e.g. unipile `dsn`). The config travels with the key.
    - `entity_type`/`entity_id`: winning level (None for a platform grant — its
      config is the environment, not a vault credential).
    - `instance_id`: the ACCOUNT (`connector_instances.id`) whose level the resolver
      judged (#1499) — None where no level is judged (own key, tenant, platform)."""
    provider: str
    secret: str
    is_platform: bool
    mode: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    account: str = ""
    instance_id: Optional[int] = None

    def __repr__(self) -> str:
        # The key NEVER leaks through repr (#564) — see `secret_repr`.
        return secret_repr.expurge(self, "secret")

    @property
    def key(self) -> str:
        return self.secret

    @property
    def fields(self) -> dict:
        porteur = providers.credential_provider(self.provider)
        try:
            return credentials_store.unpack_secret(porteur, self.secret)
        except credentials_store.SecretUnpackError as e:
            if not self.is_platform:
                raise
            # A platform key is shaped at POSE time (`preparer_pose`); one that does not
            # unpack was stored raw before that, and is rewritten once by the command.
            raise credentials_store.SecretUnpackError(
                f"{e} Platform key `{self.entity_id}` was stored raw: run "
                "`oto-mcp maintenance cles-plateforme-au-format --apply`.") from None

    @property
    def config(self) -> dict:
        """Non-secret config paired with the winning key. Lazy: no cost for
        callers that only read `key` (hot path resolve_api_key)."""
        porteur = providers.credential_provider(self.provider)
        _, cfg = credentials_store.split_secret_config(porteur, self.fields)
        if self.entity_type is not None:
            try:
                row = credentials_store.get_credential_with_meta(
                    self.entity_type, self.entity_id, porteur, self.account)
            # noqa: SILENT — non-secret config absent ⇒ the winning key stays usable
            except Exception:
                row = None
            if row:
                cfg = {**cfg, **credentials_store.public_meta(row.get("meta"))}
        return cfg
