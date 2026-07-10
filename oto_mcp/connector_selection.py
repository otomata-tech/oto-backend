"""Sélection de connecteurs par un membre — modèle « marketplace » (ADR 0019).

**Trois faits distincts, ne pas confondre** (cf. ADR 0019) :
- *Exposition* = `connector_activation` (qui PEUT voir, gouvernance plateforme,
  deny-by-default, admin-only) — le **plafond**.
- *Proposition* = `orgs.default_connectors` (ce que l'org RECOMMANDE, consultatif).
- *Sélection* = **cette table** (ce que le MEMBRE installe dans son espace), per
  `(sub, org_id)`. C'est l'état neuf que le marketplace introduit.

Trois états membre, par connecteur :
- **non-sélectionné** : aucune ligne → reste dans la library/catalogue.
- **sélectionné-actif** (`state='active'`) : outils visibles (visibilité normale).
- **sélectionné-pause** (`state='paused'`) : installé mais outils masqués.

La table est la **source de vérité de la sélection** ; la *visibilité* reste
calculée (`tool_visibility.is_tool_visible` inchangé) — le middleware en dérive un
masquage supplémentaire (pause/non-sélection), jamais sur PROTECTED_TOOLS ni
grant-only. `org_id=0` = espace perso (sentinelle ADR 0015), comme `user_disabled_tools`.

NB barreau **B1** : table + helpers seuls, AUCUN appelant ne lit encore — canari de
déploiement (no-behavior-change). Le câblage (lecture `/api/me/connectors`, mutation,
masquage pause au middleware) suit en B3/B4/B5.

Convention : self-managing (ouvrent leur propre connexion, comme `connector_activation`).
Seul `init_schema` reçoit le `conn` de la transaction `db.init_db`. Aucun import
oto_mcp au niveau module (leaf) — `db` importé paresseusement.
"""
from __future__ import annotations

# Valeurs fermées de l'état de sélection.
ACTIVE = "active"
PAUSED = "paused"
STATES = (ACTIVE, PAUSED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_selected_connectors (
    sub         TEXT   NOT NULL,
    org_id      BIGINT NOT NULL DEFAULT 0,   -- 0 = espace perso (ADR 0015)
    connector   TEXT   NOT NULL,             -- nom de connecteur (registre providers.py)
    state       TEXT   NOT NULL DEFAULT 'active',  -- 'active' | 'paused'
    selected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sub, org_id, connector)
);
-- Marque de transition (B6) : un (sub, org) « seedé » a reçu sa sélection initiale
-- (= l'exposé courant en active) au passage au régime strict « non-sélectionné =
-- masqué ». Évite de re-seeder un membre qui a légitimement tout désélectionné.
CREATE TABLE IF NOT EXISTS connector_selection_seeded (
    sub       TEXT   NOT NULL,
    org_id    BIGINT NOT NULL DEFAULT 0,
    seeded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sub, org_id)
);
"""


# --- schéma (reçoit le conn de la transaction init_db) ----------------------

def init_schema(conn) -> None:
    """Crée la table. Idempotent. Appelé par `db.init_db` dans la même transaction."""
    conn.execute(_SCHEMA)


# --- lectures (self-managing) -----------------------------------------------

def list_selection(sub: str, org_id: int = 0) -> dict[str, str]:
    """Sélections du membre dans une org : `{connector: state}`. Les connecteurs
    absents de la map sont *non-sélectionnés*."""
    from . import db

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT connector, state FROM user_selected_connectors WHERE sub = %s AND org_id = %s",
            (sub, org_id),
        ).fetchall()
    return {r["connector"]: r["state"] for r in rows}


def state_of(sub: str, connector: str, org_id: int = 0) -> str | None:
    """État d'un connecteur pour le membre : 'active' | 'paused' | None (non-sélectionné)."""
    from . import db

    with db._connect() as conn:
        row = conn.execute(
            "SELECT state FROM user_selected_connectors "
            "WHERE sub = %s AND org_id = %s AND connector = %s",
            (sub, org_id, connector),
        ).fetchone()
    return row["state"] if row is not None else None


# --- écritures (self-managing) ----------------------------------------------

def set_state(sub: str, connector: str, state: str, org_id: int = 0) -> None:
    """Sélectionne (ou bascule actif↔pause) un connecteur pour le membre. Upsert."""
    if state not in STATES:
        raise ValueError(f"état de sélection invalide: {state!r} (∈ {STATES})")
    from . import db

    with db._connect() as conn:
        conn.execute(
            "INSERT INTO user_selected_connectors (sub, org_id, connector, state) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (sub, org_id, connector) "
            "DO UPDATE SET state = EXCLUDED.state, selected_at = NOW()",
            (sub, org_id, connector, state),
        )


def unselect(sub: str, connector: str, org_id: int = 0) -> bool:
    """Retire un connecteur de la sélection du membre (→ retour library).
    Renvoie True si une ligne existait."""
    from . import db

    with db._connect() as conn:
        cur = conn.execute(
            "DELETE FROM user_selected_connectors WHERE sub = %s AND org_id = %s AND connector = %s",
            (sub, org_id, connector),
        )
        return (cur.rowcount or 0) > 0


# --- seed initial d'un (sub, org) — socle curé (ADR 0050) ---------------------

def is_seeded(sub: str, org_id: int = 0) -> bool:
    """True si ce (sub, org) a déjà reçu sa sélection initiale (cf. `seed_active`)."""
    from . import db

    with db._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM connector_selection_seeded WHERE sub = %s AND org_id = %s",
            (sub, org_id),
        ).fetchone()
    return row is not None


def seed_active(sub: str, connectors: set[str], org_id: int = 0) -> None:
    """Sélection initiale d'un (sub, org) (one-shot, marque `seeded`) : installe
    `connectors` en `active`. L'appelant décide le contenu — régime nominal =
    le SOCLE curé `default_active ∩ exposé` (ADR 0050, `session_visibility`).
    Le reste de l'exposé démarre non-sélectionné (→ library). Idempotent, ne
    réécrit jamais une sélection existante."""
    from . import db

    with db._connect() as conn:
        for name in connectors:
            conn.execute(
                "INSERT INTO user_selected_connectors (sub, org_id, connector, state) "
                "VALUES (%s, %s, %s, 'active') ON CONFLICT (sub, org_id, connector) DO NOTHING",
                (sub, org_id, name),
            )
        conn.execute(
            "INSERT INTO connector_selection_seeded (sub, org_id) VALUES (%s, %s) "
            "ON CONFLICT (sub, org_id) DO NOTHING",
            (sub, org_id),
        )


# --- migration ADR 0050 : backfill one-shot des pairs pré-existants -----------

# Connecteurs `default_hidden` AU MOMENT du retrait du flag (ADR 0050 B3) — fait
# historique figé dans la migration : le backfill reconstitue ce que chaque membre
# VOYAIT (l'exposé de son org moins ces masqués), pas l'exposé brut.
_BACKFILL_HIDDEN = frozenset(
    {"attio", "brevoauto", "pennylaneged", "resend", "scaleway", "http", "bridge"})
# Sentinelle du one-shot (jamais un sub réel — les subs Logto sont alphanumériques).
# Posée dans `connector_selection_seeded` après la passe : le backfill ne rejoue
# JAMAIS, car un pair créé APRÈS lui doit recevoir le SOCLE au seed lazy, pas
# l'exposé historique.
_BACKFILL_MARK = "#adr0050-backfill"


def backfill_preexisting(conn) -> None:
    """One-shot ADR 0050 (reçoit le `conn` de la transaction `db.init_db`) : au
    passage au régime nominal « non-sélectionné = masqué », chaque (sub, org) DÉJÀ
    existant et jamais seedé reçoit en sélection `active` ce qu'il VOYAIT (exposé
    de l'org − ex-`default_hidden`) — zéro changement de toolbox pour l'existant.
    Les pairs déjà seedés (régime strict testé en canari) gardent leurs choix."""
    done = conn.execute(
        "SELECT 1 FROM connector_selection_seeded WHERE sub = %s AND org_id = 0",
        (_BACKFILL_MARK,),
    ).fetchone()
    if done:
        return
    from .connector_activation import _resolve

    rows = conn.execute(
        "SELECT connector, org_id, enabled FROM connector_activation").fetchall()
    global_map: dict[str, bool] = {}
    overrides: dict[int, dict[str, bool]] = {}
    for r in rows:
        if r["org_id"] is None:
            global_map[r["connector"]] = bool(r["enabled"])
        else:
            overrides.setdefault(r["org_id"], {})[r["connector"]] = bool(r["enabled"])
    # Tous les couples (sub, org) susceptibles d'un profil de visibilité : les
    # memberships + la sentinelle perso/globale org_id=0 (ADR 0015) — moins les
    # pairs déjà seedés.
    pairs = conn.execute(
        "SELECT sub, org_id FROM org_members "
        "UNION SELECT sub, 0 FROM users "
        "EXCEPT SELECT sub, org_id FROM connector_selection_seeded").fetchall()
    for p in pairs:
        exposed = _resolve(global_map, overrides.get(p["org_id"], {}))
        for name in sorted(exposed - _BACKFILL_HIDDEN):
            conn.execute(
                "INSERT INTO user_selected_connectors (sub, org_id, connector, state) "
                "VALUES (%s, %s, %s, 'active') "
                "ON CONFLICT (sub, org_id, connector) DO NOTHING",
                (p["sub"], p["org_id"], name),
            )
        conn.execute(
            "INSERT INTO connector_selection_seeded (sub, org_id) VALUES (%s, %s) "
            "ON CONFLICT (sub, org_id) DO NOTHING",
            (p["sub"], p["org_id"]),
        )
    conn.execute(
        "INSERT INTO connector_selection_seeded (sub, org_id) VALUES (%s, 0) "
        "ON CONFLICT (sub, org_id) DO NOTHING",
        (_BACKFILL_MARK,),
    )
