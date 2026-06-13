"""Harnais « scout » — couche service prospection (ADR 0008).

Oriente le write-model (`store`) et le read-model (`projection`) en une surface
unique, **scopée par org** (un workspace `prospection` par org). Consommée par
l'adaptateur REST (`api_routes_scout`) ET les tools MCP (`tools/scout`).

Toute écriture reprojette l'entreprise concernée (incrémental).
"""

from __future__ import annotations

from typing import Optional

from . import projection, store

WORKSPACE_KIND = "prospection"


def workspace(org_id: int) -> int:
    """Le workspace prospection de l'org (créé à la volée)."""
    return store.get_or_create_workspace(org_id, WORKSPACE_KIND)


# ── écriture (reprojette) ────────────────────────────────────────────────────
def add_prospect(org_id: int, siren: str, nom: str, bp_an: Optional[int] = None,
                 idcc: Optional[str] = None, created_by: str = "system") -> int:
    ws = workspace(org_id)
    fid = store.add_fact(ws, "entreprise",
                         {"siren": siren, "nom": nom, "bp_an": bp_an, "idcc": idcc}, created_by)
    projection.project_entreprise(fid)
    return fid


def add_contact(org_id: int, entreprise_fact_id: int, nom: str, tel: Optional[str] = None,
                linkedin: Optional[str] = None, created_by: str = "system") -> int:
    ent = _require_owned(org_id, entreprise_fact_id)
    cid = store.add_fact(ent["workspace_id"], "contact",
                         {"nom": nom, "tel": tel, "linkedin": linkedin}, created_by)
    store.link(cid, entreprise_fact_id, "concerns")
    projection.project_entreprise(entreprise_fact_id)
    return cid


def record_action(org_id: int, entreprise_fact_id: int, canal: str, outcome: str,
                  note: Optional[str] = None, created_by: str = "system") -> dict:
    ent = _require_owned(org_id, entreprise_fact_id)
    aid = store.add_fact(ent["workspace_id"], "action",
                         {"canal": canal, "outcome": outcome, "note": note}, created_by)
    store.link(aid, entreprise_fact_id, "concerns")
    projection.project_entreprise(entreprise_fact_id)
    return get_detail(org_id, entreprise_fact_id)


# ── lecture ──────────────────────────────────────────────────────────────────
def queue(org_id: int, limit: int = 50) -> list[dict]:
    return projection.queue(workspace(org_id), limit)


def claim_next(org_id: int, who: str, ttl_min: int = 20) -> Optional[dict]:
    return projection.claim_next(workspace(org_id), who, ttl_min)


def get_detail(org_id: int, entreprise_fact_id: int) -> dict:
    """Fiche prospect = ligne projection + contacts + actions (parcours du graphe).
    Scopé à l'org : un prospect d'une autre org lève KeyError (anti-IDOR)."""
    ent = _require_owned(org_id, entreprise_fact_id)
    incoming = store.incoming(entreprise_fact_id, "concerns")
    contacts = [{"fact_id": f["id"], **f["data"]} for f in incoming if f["kind"] == "contact"]
    actions = [{"fact_id": f["id"], "created_at": f["created_at"], **f["data"]}
               for f in incoming if f["kind"] == "action"]
    actions.sort(key=lambda a: a["created_at"])
    row = projection._get_prospect(entreprise_fact_id)
    return {
        "fact_id": entreprise_fact_id,
        **(row or {"siren": ent["data"]["siren"], "nom": ent["data"]["nom"],
                   "statut": "qualified", "fit": 0, "heat": "cold"}),
        "contacts": contacts,
        "actions": actions,
    }


def _require_owned(org_id: int, entreprise_fact_id: int) -> dict:
    """Récupère le fact entreprise EN VÉRIFIANT qu'il appartient au workspace
    prospection de l'org. Lève KeyError sinon — garde anti-IDOR cross-org pour
    toute opération adressée par `fact_id` (REST + MCP passent par ici)."""
    ent = store.get_fact(entreprise_fact_id)  # KeyError si le fact n'existe pas
    if ent["kind"] != "entreprise" or ent["workspace_id"] != workspace(org_id):
        raise KeyError(f"prospect {entreprise_fact_id} introuvable dans cette org")
    return ent
