"""Cran d'activation des connecteurs — gouvernance DB (ADR 0010, décision 4).

**Déclaration (registre `providers.py`) ≠ activation (cette table).** Un connecteur
déclaré en code ne s'expose PAS du seul fait d'être déclaré : il faut une ligne
d'activation. Deux niveaux, l'org primant sur le global :

    exposé(connector, org) = override_org si défini, sinon master global, sinon OFF

- **master global**  : ligne `(connector, org_id=NULL)` — interrupteur plateforme.
- **override d'org**  : ligne `(connector, org_id=<id>)` — force ON/OFF pour une org,
  par-dessus le global.
- **aucune ligne**    : OFF (deny-by-default — un nouveau connecteur reste inerte
  jusqu'à activation explicite par un admin).

**Seed unique** à la création de la table : les connecteurs ALORS au registre sont
activés (ON global) — snapshot de l'état au moment où le cran est introduit, pour
ne rien changer au comportement existant. Une fois la table peuplée, le boot n'y
touche plus : les connecteurs déclarés APRÈS (foncier, santé…) restent OFF tant
qu'un admin ne les active pas.

NB barreau **B1** (ADR 0010) : table + helpers seuls, aucun appelant ne lit encore
`is_exposed`/`exposed_connectors` — canari de déploiement (même discipline que le
palier org en son temps). Le câblage (catalogue `/api/connectors`, chargement des
tools) suit en B2/B3 ; la surface admin (set/clear) en B4.

Convention : les lectures/écritures sont **self-managing** (ouvrent leur propre
connexion, comme `db.*` et `org_store.*`). Seuls `init_schema`/`seed_initial`
reçoivent le `conn` de la transaction de `db.init_db`. Le module ne fait AUCUN
import oto_mcp au niveau module (leaf, comme `providers`) — `db`/`providers` sont
importés paresseusement pour éviter tout cycle.
"""
from __future__ import annotations

from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS connector_activation (
    connector TEXT NOT NULL,            -- nom de connecteur (registre providers.py)
    org_id    BIGINT,                   -- NULL = master switch plateforme ; sinon override d'org
    enabled   BOOLEAN NOT NULL,
    set_by    TEXT,
    set_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- org_id nullable → pas de PRIMARY KEY (PG impose NOT NULL sur une PK). Unicité
-- garantie par deux index partiels (même pattern que org_members_one_active).
CREATE UNIQUE INDEX IF NOT EXISTS connector_activation_global
    ON connector_activation (connector) WHERE org_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS connector_activation_org
    ON connector_activation (connector, org_id) WHERE org_id IS NOT NULL;

-- Tier ÉQUIPE (ADR 0012, restrict-only) : un chef d'équipe peut COUPER un connecteur
-- pour SON équipe, jamais l'exposer au-delà de ce que l'org autorise (invariant
-- MONOTONE — platform ⊇ org ⊇ group). On ne stocke donc que des coupures
-- (`enabled=FALSE`) ; l'absence de ligne = hérité de l'org (pas de restriction).
CREATE TABLE IF NOT EXISTS group_connector_activation (
    group_id  BIGINT NOT NULL,
    connector TEXT NOT NULL,
    enabled   BOOLEAN NOT NULL,      -- FALSE = coupé pour l'équipe (seule valeur posée)
    set_by    TEXT,
    set_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (group_id, connector)
);
"""


# --- schéma (reçoit le conn de la transaction init_db) ----------------------

def init_schema(conn) -> None:
    """Crée la table + index. Idempotent. Appelé par `db.init_db` dans la même
    transaction que le reste du schéma."""
    conn.execute(_SCHEMA)


def seed_initial(conn) -> None:
    """Seed unique : si la table est vide, active (ON global) tous les connecteurs
    du registre courant — snapshot de l'état à l'introduction du cran. Ne tourne
    qu'une fois (table peuplée → no-op), pour que les connecteurs futurs restent
    OFF (deny-by-default). `ON CONFLICT DO NOTHING` couvre un boot concurrent."""
    n = conn.execute("SELECT COUNT(*) AS n FROM connector_activation").fetchone()["n"]
    if n:
        return
    from . import providers  # registre source unique (pur, pas d'import oto_mcp)

    for name in providers.REGISTRY:
        conn.execute(
            "INSERT INTO connector_activation (connector, org_id, enabled, set_by) "
            "VALUES (%s, NULL, TRUE, %s) ON CONFLICT DO NOTHING",
            (name, "seed"),
        )


# --- résolution (pure) ------------------------------------------------------

def _resolve(global_map: dict[str, bool], override_map: dict[str, bool]) -> set[str]:
    """Applique `override d'org > master global > OFF`. Renvoie les connecteurs
    exposés. Pur (pas de DB) → testable hors connexion."""
    names = set(global_map) | set(override_map)
    return {n for n in names if override_map.get(n, global_map.get(n, False))}


def effective_for_group(exposed: set[str], group_cut: set[str]) -> set[str]:
    """Exposition EFFECTIVE pour un membre d'une équipe = ce que l'org expose MOINS
    les coupures de l'équipe active. Invariant MONOTONE (ADR 0012) : l'équipe ne peut
    que RETRANCHER — jamais rendre visible un connecteur que l'org a coupé. Pur
    (pas de DB) → testable hors connexion."""
    return exposed - group_cut


# --- lectures (self-managing) -----------------------------------------------

def is_exposed(connector: str, org_id: Optional[int] = None) -> bool:
    """exposé = override d'org si défini, sinon master global, sinon OFF."""
    from . import db

    with db._connect() as conn:
        if org_id is not None:
            row = conn.execute(
                "SELECT enabled FROM connector_activation WHERE connector = %s AND org_id = %s",
                (connector, org_id),
            ).fetchone()
            if row is not None:
                return bool(row["enabled"])
        row = conn.execute(
            "SELECT enabled FROM connector_activation WHERE connector = %s AND org_id IS NULL",
            (connector,),
        ).fetchone()
        return bool(row["enabled"]) if row is not None else False


def exposed_connectors(org_id: Optional[int] = None) -> set[str]:
    """Ensemble des connecteurs exposés (résout override d'org vs global en un
    scan). Pour filtrer le catalogue / le chargement en une requête."""
    from . import db

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT connector, org_id, enabled FROM connector_activation "
            "WHERE org_id IS NULL OR org_id = %s",
            (org_id,),
        ).fetchall()
    global_map: dict[str, bool] = {}
    override_map: dict[str, bool] = {}
    for r in rows:
        target = override_map if r["org_id"] is not None else global_map
        target[r["connector"]] = bool(r["enabled"])
    return _resolve(global_map, override_map)


def list_activations() -> list[dict]:
    """Toutes les lignes (master global + overrides d'org), pour la surface admin."""
    from . import db

    with db._connect() as conn:
        return conn.execute(
            "SELECT connector, org_id, enabled, set_by, set_at FROM connector_activation "
            "ORDER BY connector, org_id NULLS FIRST"
        ).fetchall()


# --- écritures (surface admin, B4) ------------------------------------------

def set_activation(connector: str, enabled: bool, org_id: Optional[int] = None,
                   set_by: Optional[str] = None) -> None:
    """Pose/maj l'activation : master global si `org_id` None, sinon override d'org.
    Upsert via les index partiels (cf. _SCHEMA)."""
    from . import db

    with db._connect() as conn:
        if org_id is None:
            conn.execute(
                "INSERT INTO connector_activation (connector, org_id, enabled, set_by) "
                "VALUES (%s, NULL, %s, %s) "
                "ON CONFLICT (connector) WHERE org_id IS NULL "
                "DO UPDATE SET enabled = EXCLUDED.enabled, set_by = EXCLUDED.set_by, set_at = NOW()",
                (connector, enabled, set_by),
            )
        else:
            conn.execute(
                "INSERT INTO connector_activation (connector, org_id, enabled, set_by) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (connector, org_id) WHERE org_id IS NOT NULL "
                "DO UPDATE SET enabled = EXCLUDED.enabled, set_by = EXCLUDED.set_by, set_at = NOW()",
                (connector, org_id, enabled, set_by),
            )


def clear_activation(connector: str, org_id: int) -> None:
    """Supprime un override d'org → le connecteur retombe sur le master global."""
    from . import db

    with db._connect() as conn:
        conn.execute(
            "DELETE FROM connector_activation WHERE connector = %s AND org_id = %s",
            (connector, org_id),
        )


# --- tier ÉQUIPE (restrict-only, ADR 0012) ----------------------------------

def group_cut_connectors(group_id: int) -> set[str]:
    """Connecteurs COUPÉS pour l'équipe (lignes `enabled=FALSE`). L'exposition
    effective d'un membre = `exposed_connectors(org) - group_cut_connectors(équipe
    active)` — invariant monotone : l'équipe ne peut que retrancher."""
    from . import db

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT connector FROM group_connector_activation "
            "WHERE group_id = %s AND enabled = FALSE",
            (group_id,),
        ).fetchall()
    return {r["connector"] for r in rows}


def list_group_activations(group_id: int) -> list[dict]:
    """Lignes de coupure de l'équipe (surface admin d'équipe)."""
    from . import db

    with db._connect() as conn:
        return conn.execute(
            "SELECT connector, enabled, set_by, set_at FROM group_connector_activation "
            "WHERE group_id = %s ORDER BY connector",
            (group_id,),
        ).fetchall()


def set_group_activation(group_id: int, connector: str, enabled: bool,
                         set_by: Optional[str] = None) -> None:
    """Pose une coupure d'équipe. `enabled` DOIT être False (restrict-only) — la
    garde métier (invariant monotone) est dans la capacité ; ici on stocke."""
    from . import db

    with db._connect() as conn:
        conn.execute(
            "INSERT INTO group_connector_activation (group_id, connector, enabled, set_by) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (group_id, connector) "
            "DO UPDATE SET enabled = EXCLUDED.enabled, set_by = EXCLUDED.set_by, set_at = NOW()",
            (group_id, connector, enabled, set_by),
        )


def clear_group_activation(group_id: int, connector: str) -> None:
    """Retire la coupure d'équipe → le connecteur retombe sur l'exposition de l'org."""
    from . import db

    with db._connect() as conn:
        conn.execute(
            "DELETE FROM group_connector_activation WHERE group_id = %s AND connector = %s",
            (group_id, connector),
        )
