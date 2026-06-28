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

from pydantic import BaseModel, Field, ValidationError


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


# ── Thème PROSPECTION générique : le « lead » (fiche schema-aware) ───────────
# Un seul kind structuré, riche, rendu en fiche lisible par la vue Fact graph.
# Chaque champ porte un `role` de rendu (title/subtitle/badge/metric/status/
# priority/contact/link/note/qualif) + un `ui_label`. Les champs `qualif`/`note`
# sont du TEXTE LIBRE rendu en bloc complet (≠ data tronquée) — la doctrine/agent
# DOIT les remplir (qualification commerciale), pas seulement les champs data.
class Lead(BaseModel):
    """Lead B2B (thème prospection) — données + qualification texte libre."""
    raison_sociale: str = Field(json_schema_extra={"role": "title", "ui_label": "Raison sociale"})
    siren: str | None = Field(None, json_schema_extra={"role": "badge", "ui_label": "SIREN"})
    ville: str | None = Field(None, json_schema_extra={"role": "subtitle", "ui_label": "Ville"})
    code_postal: str | None = Field(None, json_schema_extra={"role": "meta", "ui_label": "CP"})
    naf: str | None = Field(None, json_schema_extra={"role": "badge", "ui_label": "NAF"})
    effectif: str | None = Field(None, json_schema_extra={"role": "badge", "ui_label": "Effectif"})
    ca: int | None = Field(None, json_schema_extra={"role": "metric", "ui_label": "CA (€)"})
    emetteur_actuel: str | None = Field(None, json_schema_extra={"role": "badge", "ui_label": "Émetteur en place"})
    site: str | None = Field(None, json_schema_extra={"role": "link", "ui_label": "Site"})
    contact: str | None = Field(None, json_schema_extra={"role": "contact", "ui_label": "Contact (DAF/RAF)"})
    statut: str = Field("nouveau", json_schema_extra={"role": "status", "ui_label": "Statut"})
    priorite: int | None = Field(None, json_schema_extra={"role": "priority", "ui_label": "Priorité"})
    pourquoi_lead: str | None = Field(None, json_schema_extra={"role": "qualif", "ui_label": "Pourquoi ce lead"})
    accroche: str | None = Field(None, json_schema_extra={"role": "qualif", "ui_label": "Accroche"})
    next_step: str | None = Field(None, json_schema_extra={"role": "qualif", "ui_label": "Prochaine étape"})
    notes: str | None = Field(None, json_schema_extra={"role": "note", "ui_label": "Notes"})


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
    "lead": Lead,
    "entreprise": Entreprise,
    "contact": Contact,
    "action": Action,
    "facture": Facture,
    "ecriture": Ecriture,
}

# kind → domaine (= `kind` du workspace factgraph, scopé org). Un « thème » de la
# vue Fact graph = un domaine ; la doctrine n'a qu'à donner le `kind`, le workspace
# (org × domaine) est résolu/créé automatiquement.
KIND_DOMAIN: dict[str, str] = {
    "lead": "prospection",
    "entreprise": "prospection",
    "contact": "prospection",
    "action": "prospection",
    "facture": "compta",
    "ecriture": "compta",
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


# ── Description du schéma (« theme data model » exposé à la vue Fact graph) ───
def _field_meta(f) -> tuple[str | None, str | None]:
    extra = f.json_schema_extra if isinstance(f.json_schema_extra, dict) else {}
    return extra.get("role"), extra.get("ui_label")


def _type_name(annotation) -> str:
    return "int" if "int" in str(annotation) else "text"


def describe_kind(kind: str) -> dict:
    """Schéma d'un kind pour le rendu schema-aware : champs + rôle + label.
    C'est le contrat que la vue dashboard lit (aucun schéma codé en dur côté UI)."""
    model = REGISTRY.get(kind)
    if model is None:
        raise SchemaError(f"kind inconnu: {kind!r} (registre: {sorted(REGISTRY)})")
    fields = []
    for name, f in model.model_fields.items():
        role, label = _field_meta(f)
        fields.append({
            "name": name,
            "type": _type_name(f.annotation),
            "required": f.is_required(),
            "role": role,
            "label": label or name,
        })
    doc = (model.__doc__ or kind).strip().splitlines()[0]
    return {"kind": kind, "domain": KIND_DOMAIN.get(kind, "default"),
            "label": doc[:80], "fields": fields}


def describe_kinds() -> list[dict]:
    """Tous les kinds du registre, décrits (pour le sélecteur + le rendu de la vue)."""
    return [describe_kind(k) for k in REGISTRY]
