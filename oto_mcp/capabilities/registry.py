"""Registre des capacités (ADR 0009) — source unique, à la `connectors._REGISTRY_LIST`.

Les modules de domaine (`capabilities/<domaine>.py`) font `CAPABILITIES += [...]`
à l'import. `capabilities/__init__` importe ces modules pour peupler la liste
AVANT que les adaptateurs ne la bouclent. Les dérivations sont calculées à la
demande (la liste se peuple à l'import des domaines)."""
from __future__ import annotations

from ._types import Capability

CAPABILITIES: list[Capability] = []


def by_key(key: str) -> Capability:
    """Opération canonique ; une référence absente ou ambiguë est une erreur."""
    found = [cap for cap in CAPABILITIES if cap.key == key]
    if len(found) != 1:
        raise ValueError(f"Capability {key!r}: expected one declaration, found {len(found)}")
    return found[0]


def caps_with_mcp() -> list[Capability]:
    return [c for c in CAPABILITIES if c.mcp is not None]


def caps_with_rest() -> list[Capability]:
    return [c for c in CAPABILITIES if c.rest_bindings()]
