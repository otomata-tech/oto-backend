"""Grammaire de ref stable des « instances de connecteur » (ADR 0038 §B, barreau 4).

Domicile UNIQUE du format de ref — B5 (bindings slot→instance) et B6 (axe
`instance=`) l'importeront sans tirer la capacité de projection. Le ref projette
1:1 la clé primaire du coffre `connector_credentials`
`(entity_type, entity_id, connector, account)` : il est stable exactement la
durée de vie de la ligne (un `rename_account` change le ref — cohérent avec
l'AAD, qui re-chiffre au rename). Les clés plateforme ont déjà un surrogate
stable (`platform_keys.id` BIGSERIAL) : leur ref le porte, sans connecteur (la
clé porte son `provider` et un label).

Grammaire (segments joints par `:`) :

    member:{org_id}:{sub}:{connector}[:{account}]
    group:{group_id}:{connector}[:{account}]
    org:{org_id}:{connector}[:{account}]
    platform:{platform_key_id}

- Les segments LIBRES (`sub`, `account`, `connector`) sont percent-encodés
  (`urllib.parse.quote(s, safe="")`) → le `split(":")` reste non-ambigu même si
  un segment contient `:` ou `@` (les slugs du registre n'en ont pas aujourd'hui,
  mais l'encodage uniforme est un no-op pour eux et blinde la grammaire pour
  B5 où le ref devient un input). `org_id`/`group_id`/`platform_key_id` = ints
  ASCII stricts.
- `account == ''` (marqueur mono-compte du coffre) = segment OMIS — pas de
  sentinel « default » (un account littéralement nommé ainsi collisionnerait) ;
  l'omission est réversible sans ambiguïté.
- Côté client le ref est OPAQUE : il se stocke et se repasse tel quel, jamais
  reconstruit. En B4 il est output-only (accepté nulle part en entrée).

Plan de survie B5 : la table `connector_instances` portera une colonne
`ref TEXT UNIQUE` backfillée à l'IDENTIQUE (les bindings posés contre des refs
B4 restent valides) ; les instances neuves recevront `inst:{id}` et `parse_ref`
acceptera les deux formes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, unquote

_LEVELS = ("member", "group", "org", "platform")


@dataclass(frozen=True)
class InstanceRef:
    """Ref décomposé. `connector` est None pour `platform` (porté par la clé)."""
    level: str                          # member | group | org | platform
    connector: Optional[str]            # renseigné pour tous (ADR 0044 §F : plateforme aussi)
    org_id: Optional[int] = None
    sub: Optional[str] = None
    group_id: Optional[int] = None
    label: Optional[str] = None         # platform : label de la clé plateforme
    account: str = ""


def _tail(connector: str, account: str) -> str:
    """Suffixe commun `{connector}[:{account}]` — account vide = segment omis."""
    c = quote(connector, safe="")
    if account:
        return f"{c}:{quote(account, safe='')}"
    return c


def make_member_ref(org_id: int, sub: str, connector: str, account: str = "") -> str:
    return f"member:{org_id}:{quote(sub, safe='')}:{_tail(connector, account)}"


def make_group_ref(group_id: int, connector: str, account: str = "") -> str:
    return f"group:{group_id}:{_tail(connector, account)}"


def make_org_ref(org_id: int, connector: str, account: str = "") -> str:
    return f"org:{org_id}:{_tail(connector, account)}"


def make_platform_ref(connector: str, label: str) -> str:
    # ADR 0044 §F : la clé plateforme est une instance du coffre (fin du surrogate
    # platform_keys.id) → ref (connector, label), comme les autres scopes.
    return f"platform:{quote(connector, safe='')}:{quote(label, safe='')}"


def format_ref(r: InstanceRef) -> str:
    """Inverse de `parse_ref` — re-sérialise un ref décomposé (messages d'erreur,
    ré-affichage). Roundtrip exact avec les make_*."""
    if r.level == "member":
        return make_member_ref(r.org_id, r.sub, r.connector, r.account)
    if r.level == "group":
        return make_group_ref(r.group_id, r.connector, r.account)
    if r.level == "org":
        return make_org_ref(r.org_id, r.connector, r.account)
    return make_platform_ref(r.connector, r.label)


def _int(segment: str) -> int:
    """Segment id — int strict ASCII (`isdigit` seul accepte les chiffres Unicode
    → refs alias non-canoniques, latent pour B5 où le ref devient un input)."""
    if not (segment.isascii() and segment.isdigit()):
        raise ValueError("invalid_instance_ref")
    return int(segment)


def parse_ref(ref: str) -> InstanceRef:
    """Décompose un ref. Lève `ValueError("invalid_instance_ref")` si malformé :
    level inconnu, id non-int, arité fausse, segment vide."""
    parts = (ref or "").split(":")
    level = parts[0]
    if level not in _LEVELS or any(p == "" for p in parts):
        raise ValueError("invalid_instance_ref")
    if level == "platform":
        # ADR 0044 §F : `platform:{connector}:{label}` (3 segments).
        if len(parts) != 3:
            raise ValueError("invalid_instance_ref")
        return InstanceRef(level="platform", connector=unquote(parts[1]),
                           label=unquote(parts[2]))
    if level == "member":
        if len(parts) not in (4, 5):
            raise ValueError("invalid_instance_ref")
        return InstanceRef(
            level="member", connector=unquote(parts[3]), org_id=_int(parts[1]),
            sub=unquote(parts[2]),
            account=unquote(parts[4]) if len(parts) == 5 else "")
    # group | org : 3 ou 4 segments.
    if len(parts) not in (3, 4):
        raise ValueError("invalid_instance_ref")
    entity_id = _int(parts[1])
    account = unquote(parts[3]) if len(parts) == 4 else ""
    if level == "group":
        return InstanceRef(level="group", connector=unquote(parts[2]),
                           group_id=entity_id, account=account)
    return InstanceRef(level="org", connector=unquote(parts[2]),
                       org_id=entity_id, account=account)
