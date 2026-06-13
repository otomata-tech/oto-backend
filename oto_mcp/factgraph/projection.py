"""Read-model prospection : projection matérialisée du graphe (ADR 0008).

Le graphe (fact+edge) est la source de vérité générique. Mais trier 500 leads
par score à chaque ouverture de la file serait absurde → on matérialise une
projection typée `factgraph.prospect` (une ligne par entreprise, dénormalisée +
scorée + claimable), reconstruite depuis le graphe. Jetable : DROP + rebuild.

CQRS de GR (`facts → sites/companies`) en miniature. Amélioration vs blitz : le
score est *stocké* → tri ET claim en SQL (`FOR UPDATE SKIP LOCKED`), sans la
limite « scoring JS post-LIMIT 500 ».
"""

from __future__ import annotations

from typing import Optional

import psycopg

from .. import db
from . import store

# Config de scoring (à terme : factgraph.workspace.scoring_config JSONB éditable).
DEFAULT_SCORING = {
    "bp_sweet_min": 100,
    "bp_sweet_max": 300,
    "idcc_cibles": {"1285", "3090", "2642", "3097", "2770", "2717"},  # les 6 IDCC blitz
}

# outcome d'action → statut. Le statut est DÉRIVÉ du dernier outcome, jamais stocké
# dans le graphe (à terme : transitions en données, factgraph.workspace).
OUTCOME_TO_STATUT = {
    "sent": "emailed", "talked": "talked", "rdv": "rdv", "dead": "dead", "called": "called",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS factgraph.prospect (
  fact_id        BIGINT PRIMARY KEY REFERENCES factgraph.fact(id) ON DELETE CASCADE,
  workspace_id   BIGINT NOT NULL REFERENCES factgraph.workspace(id) ON DELETE CASCADE,
  siren          TEXT NOT NULL,
  nom            TEXT NOT NULL,
  bp_an          INT,
  idcc           TEXT,
  n_contacts     INT  NOT NULL DEFAULT 0,
  has_phone      BOOLEAN NOT NULL DEFAULT false,
  has_linkedin   BOOLEAN NOT NULL DEFAULT false,
  statut         TEXT NOT NULL DEFAULT 'qualified',
  last_action_at TIMESTAMPTZ,
  fit            INT  NOT NULL DEFAULT 0,
  heat           TEXT NOT NULL DEFAULT 'cold',
  claimed_by     TEXT,
  claimed_until  TIMESTAMPTZ,
  projected_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS prospect_queue_idx
  ON factgraph.prospect (workspace_id, statut, heat, fit DESC);
"""

# Ordre SQL réutilisé par la file et le claim : hot > warm > cold, puis fit desc.
_ORDER = "CASE heat WHEN 'hot' THEN 0 WHEN 'warm' THEN 1 ELSE 2 END, fit DESC, fact_id"


def init_schema(conn: psycopg.Connection) -> None:
    conn.execute(_SCHEMA)


def score(bp_an, idcc, has_phone, has_linkedin, cfg=DEFAULT_SCORING):
    """fit 0-100 + heat hot/warm/cold (dérivés, comme blitz)."""
    fit = 0
    if bp_an is not None and cfg["bp_sweet_min"] <= bp_an <= cfg["bp_sweet_max"]:
        fit += 50
    elif bp_an is not None and bp_an > 0:
        fit += 20
    if idcc in cfg["idcc_cibles"]:
        fit += 25
    if has_phone:
        fit += 15
    if has_linkedin:
        fit += 10
    heat = "hot" if fit >= 75 else "warm" if fit >= 40 else "cold"
    return fit, heat


def project_entreprise(entreprise_fact_id: int) -> None:
    """(Re)construit la ligne projection d'un prospect depuis le graphe (incrémental)."""
    ent = store.get_fact(entreprise_fact_id)
    if ent["kind"] != "entreprise":
        raise ValueError(f"fact {entreprise_fact_id} n'est pas une entreprise")
    incoming = store.incoming(entreprise_fact_id, "concerns")
    contacts = [f for f in incoming if f["kind"] == "contact"]
    actions = sorted((f for f in incoming if f["kind"] == "action"), key=lambda f: f["created_at"])

    has_phone = any(c["data"].get("tel") for c in contacts)
    has_linkedin = any(c["data"].get("linkedin") for c in contacts)
    last = actions[-1] if actions else None
    statut = OUTCOME_TO_STATUT.get(last["data"]["outcome"], "qualified") if last else "qualified"
    fit, heat = score(ent["data"].get("bp_an"), ent["data"].get("idcc"), has_phone, has_linkedin)

    with db._connect() as conn:
        conn.execute(
            """
            INSERT INTO factgraph.prospect
              (fact_id, workspace_id, siren, nom, bp_an, idcc, n_contacts, has_phone,
               has_linkedin, statut, last_action_at, fit, heat, projected_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
            ON CONFLICT (fact_id) DO UPDATE SET
              siren=EXCLUDED.siren, nom=EXCLUDED.nom, bp_an=EXCLUDED.bp_an, idcc=EXCLUDED.idcc,
              n_contacts=EXCLUDED.n_contacts, has_phone=EXCLUDED.has_phone,
              has_linkedin=EXCLUDED.has_linkedin, statut=EXCLUDED.statut,
              last_action_at=EXCLUDED.last_action_at, fit=EXCLUDED.fit, heat=EXCLUDED.heat,
              projected_at=NOW()
            """,
            (entreprise_fact_id, ent["workspace_id"], ent["data"]["siren"], ent["data"]["nom"],
             ent["data"].get("bp_an"), ent["data"].get("idcc"), len(contacts), has_phone,
             has_linkedin, statut, last["created_at"] if last else None, fit, heat),
        )


def rebuild(workspace_id: int) -> int:
    """Rebuild complet : reprojette toutes les entreprises du workspace."""
    ents = store.find(workspace_id, "entreprise")
    for e in ents:
        project_entreprise(e["id"])
    return len(ents)


def _get_prospect(fact_id: int) -> Optional[dict]:
    """La ligne projection d'un prospect (sans les contacts/actions)."""
    with db._connect() as conn:
        return conn.execute(
            "SELECT siren, nom, bp_an, idcc, n_contacts, has_phone, has_linkedin, "
            "statut, last_action_at, fit, heat, claimed_by, claimed_until "
            "FROM factgraph.prospect WHERE fact_id = %s",
            (fact_id,),
        ).fetchone()


def queue(workspace_id: int, limit: int = 50) -> list[dict]:
    """File Blitz Day : qualified strictement libres, triés heat→fit, EN SQL."""
    with db._connect() as conn:
        return conn.execute(
            f"""
            SELECT fact_id, nom, siren, fit, heat, statut FROM factgraph.prospect
            WHERE workspace_id = %s AND statut = 'qualified'
              AND (claimed_until IS NULL OR claimed_until < NOW())
            ORDER BY {_ORDER}
            LIMIT %s
            """,
            (workspace_id, limit),
        ).fetchall()


def claim_next(workspace_id: int, who: str, ttl_min: int = 20) -> Optional[dict]:
    """Attribue atomiquement le prochain prospect libre (anti-collision multi-sales).

    FOR UPDATE SKIP LOCKED = pattern atomique propre : deux sales concurrents
    obtiennent deux prospects différents, sans retry applicatif.
    """
    with db._connect() as conn:
        return conn.execute(
            f"""
            WITH pick AS (
              SELECT fact_id FROM factgraph.prospect
              WHERE workspace_id = %s AND statut = 'qualified'
                AND (claimed_until IS NULL OR claimed_until < NOW())
              ORDER BY {_ORDER}
              LIMIT 1
              FOR UPDATE SKIP LOCKED
            )
            UPDATE factgraph.prospect p
               SET claimed_by = %s, claimed_until = NOW() + make_interval(mins => %s)
              FROM pick WHERE p.fact_id = pick.fact_id
            RETURNING p.fact_id, p.nom, p.siren, p.fit, p.heat
            """,
            (workspace_id, who, ttl_min),
        ).fetchone()
