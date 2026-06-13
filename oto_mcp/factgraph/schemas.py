"""Registre des schémas de facts — la partie « structuré » du graphe (ADR 0008).

Le graphe est générique (2 tables). Mais chaque `kind` de fact déclare ici une
*structure* (modèle pydantic) validée à l'écriture : un `kind=contact` est
*garanti* avoir sa forme. C'est la réconciliation entre le tout-JSONB de GR et
les colonnes typées de blitz.

Pur (aucune dépendance DB) → testable comme `connectors.org_secret_meta`.

NB ADR 0008 §4 : à terme un nouveau cas d'usage = des *données* (kinds/roles en
DB éditables), pas du code. Première itération : registre code-défini, versionné
avec le connecteur. La projection (qui calcule le score) reste du code de toute
façon.
"""

from __future__ import annotations

from typing import Literal, Type

from pydantic import BaseModel, ValidationError


# ── Cas d'usage PROSPECTION (le harnais « scout », généralisation de blitz) ──
class Entreprise(BaseModel):
    siren: str
    nom: str
    bp_an: int | None = None          # bulletins de paie / an (sweet spot blitz)
    idcc: str | None = None


class Contact(BaseModel):
    nom: str
    tel: str | None = None
    linkedin: str | None = None


class Action(BaseModel):
    canal: Literal["appel", "email"]
    outcome: str                       # rdv | talked | sent | dead | called ...
    note: str | None = None


# ── Cas d'usage COMPTA (canari de généricité — MÊMES tables, zéro DDL) ───────
class Facture(BaseModel):
    numero: str
    montant_cents: int
    tiers: str


class Ecriture(BaseModel):
    libelle: str
    montant_cents: int
    sens: Literal["debit", "credit"]


REGISTRY: dict[str, Type[BaseModel]] = {
    "entreprise": Entreprise,
    "contact": Contact,
    "action": Action,
    "facture": Facture,
    "ecriture": Ecriture,
}

# Arêtes typées : role → (kinds source autorisés, kinds cible autorisés).
# set() vide = n'importe quel kind.
EDGE_RULES: dict[str, tuple[set[str], set[str]]] = {
    "concerns":     ({"contact", "action"}, {"entreprise"}),
    "derived-from": (set(), set()),
    "rapproche":    ({"ecriture"}, {"facture"}),
}


class SchemaError(ValueError):
    """Un fact ou une arête ne respecte pas la structure déclarée."""


def validate_fact(kind: str, data: dict) -> dict:
    """Valide `data` contre le schéma du `kind` ; renvoie le dict normalisé.
    Lève SchemaError si kind inconnu ou data malformée."""
    model = REGISTRY.get(kind)
    if model is None:
        raise SchemaError(f"kind inconnu: {kind!r} (registre: {sorted(REGISTRY)})")
    try:
        return model.model_validate(data).model_dump(mode="json")
    except ValidationError as e:
        raise SchemaError(f"fact {kind!r} invalide: {e}") from e


def validate_edge(role: str, src_kind: str, dst_kind: str) -> None:
    """Valide qu'une arête `role` est permise entre ces deux kinds."""
    rule = EDGE_RULES.get(role)
    if rule is None:
        raise SchemaError(f"role d'arête inconnu: {role!r} (règles: {sorted(EDGE_RULES)})")
    allowed_src, allowed_dst = rule
    if allowed_src and src_kind not in allowed_src:
        raise SchemaError(f"role {role!r}: source {src_kind!r} interdite (attendu {sorted(allowed_src)})")
    if allowed_dst and dst_kind not in allowed_dst:
        raise SchemaError(f"role {role!r}: cible {dst_kind!r} interdite (attendu {sorted(allowed_dst)})")
