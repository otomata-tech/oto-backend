"""Cran d'activation des connecteurs — gouvernance DB (ADR 0010, décision 4).

**Déclaration (registre `providers/`) ≠ activation (cette table).** Un connecteur
déclaré en code ne s'expose PAS du seul fait d'être déclaré : il faut une ligne
d'activation. Résolution, l'échelle des scopes primant du plus proche au plus large :

    exposé(connector, org)  = override_org si défini, sinon master plateforme, sinon OFF
    effectif(membre équipe) = exposé(org) − coupures de l'équipe (restrict-only)

**Table UNIQUE `connector_availability`** (chantier ACL, cadrage 10/07 — fusion de
l'ex-paire `connector_activation` + `group_connector_activation`) : le grain est une
COLONNE de scope, pas une table par grain.

- **('platform', '')**      : master plateforme (interrupteur global).
- **('org', <org_id>)**     : override d'org — force ON/OFF par-dessus le master.
- **('group', <group_id>)** : coupure d'équipe — `enabled=FALSE` UNIQUEMENT
  (invariant MONOTONE ADR 0012 : l'équipe retranche, n'expose jamais ; la garde
  métier vit dans la capacité).
- **aucune ligne**          : OFF au niveau org (deny-by-default), hérité au niveau équipe.

**Seed unique** (lignes platform) : les connecteurs au registre AU MOMENT de
l'introduction du cran sont activés ; les suivants restent OFF jusqu'à activation
explicite. **Copie legacy au boot** (gardée `to_regclass`, newer-wins sur `set_at`) :
les deux tables historiques sont recopiées tant qu'elles existent — elles tombent
en B2 une fois ce code promu (DB partagée canari/prod).

Convention : les lectures/écritures sont **self-managing** (ouvrent leur propre
connexion, comme `db.*` et `org_store.*`). Seuls `init_schema`/`seed_initial`
reçoivent le `conn` de la transaction de `db.init_db`. Le module ne fait AUCUN
import oto_mcp au niveau module (leaf, comme `providers`) — `db`/`providers` sont
importés paresseusement pour éviter tout cycle.
"""
from __future__ import annotations

from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS connector_availability (
    scope_type TEXT NOT NULL CHECK (scope_type IN ('platform','org','group')),
    scope_id   TEXT NOT NULL DEFAULT '',   -- '' pour platform ; org.id / group.id en texte sinon
    connector  TEXT NOT NULL,              -- nom de connecteur (registre providers/)
    enabled    BOOLEAN NOT NULL,
    set_by     TEXT,
    set_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (scope_type, scope_id, connector)
);
"""


# --- schéma (reçoit le conn de la transaction init_db) ----------------------

def init_schema(conn) -> None:
    """Crée la table unifiée + recopie les tables legacy si elles existent encore.
    Idempotent. Appelé par `db.init_db` dans la même transaction que le reste."""
    conn.execute(_SCHEMA)
    _copy_legacy(conn)


def _copy_legacy(conn) -> None:
    """Copie legacy → unifiée, à CHAQUE boot tant que les tables legacy existent
    (fenêtre canari/prod : la prod écrit encore les legacy jusqu'à promotion —
    newer-wins sur `set_at` rattrape ses écritures au boot suivant). Gardée
    `to_regclass` : après le DROP (B2), no-op — un boot ne casse jamais."""
    if conn.execute("SELECT to_regclass('connector_activation') AS t").fetchone()["t"]:
        conn.execute("""
            INSERT INTO connector_availability (scope_type, scope_id, connector, enabled, set_by, set_at)
            SELECT CASE WHEN org_id IS NULL THEN 'platform' ELSE 'org' END,
                   COALESCE(org_id::text, ''), connector, enabled, set_by, set_at
              FROM connector_activation
            ON CONFLICT (scope_type, scope_id, connector) DO UPDATE
               SET enabled = EXCLUDED.enabled, set_by = EXCLUDED.set_by, set_at = EXCLUDED.set_at
             WHERE EXCLUDED.set_at > connector_availability.set_at
        """)
    if conn.execute("SELECT to_regclass('group_connector_activation') AS t").fetchone()["t"]:
        conn.execute("""
            INSERT INTO connector_availability (scope_type, scope_id, connector, enabled, set_by, set_at)
            SELECT 'group', group_id::text, connector, enabled, set_by, set_at
              FROM group_connector_activation
            ON CONFLICT (scope_type, scope_id, connector) DO UPDATE
               SET enabled = EXCLUDED.enabled, set_by = EXCLUDED.set_by, set_at = EXCLUDED.set_at
             WHERE EXCLUDED.set_at > connector_availability.set_at
        """)


def seed_initial(conn) -> None:
    """Seed unique : si aucune ligne PLATFORM n'existe (ni copiée du legacy, ni déjà
    seedée), active (master ON) tous les connecteurs du registre courant — snapshot
    de l'état à l'introduction du cran. `ON CONFLICT DO NOTHING` couvre un boot
    concurrent. ⚠️ Le guard porte sur les lignes platform SEULEMENT : vider la table
    par erreur re-seederait tout à ON (documenté au cadrage — ne pas la vider)."""
    n = conn.execute("SELECT COUNT(*) AS n FROM connector_availability "
                     "WHERE scope_type = 'platform'").fetchone()["n"]
    if n:
        return
    from .. import providers  # registre source unique (pur, pas d'import oto_mcp)

    for name in providers.REGISTRY:
        conn.execute(
            "INSERT INTO connector_availability (scope_type, scope_id, connector, enabled, set_by) "
            "VALUES ('platform', '', %s, TRUE, %s) ON CONFLICT DO NOTHING",
            (name, "seed"),
        )


# Marque du one-shot de purge. Elle vit dans `connector_selection_seeded` —
# précédent établi par `selection._BACKFILL_MARK` : une table de MARQUES de boot,
# sans sémantique d'accès. Les deux autres emplacements possibles sont des pièges :
# une ligne sentinelle dans `connector_acl` VAUT « connecteur restreint » (l'ACL est
# deny-by-default à la présence), et une ligne 'platform' dans
# `connector_availability` serait lue comme l'interrupteur maître (`is_exposed` ne
# filtre pas sur `scope_id`). Une marque ne doit jamais ressembler à une décision.
_PURGE_MARK = "#purge-connecteur-depose:"


def _purge_faite(conn, connector: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM connector_selection_seeded WHERE sub = %s AND org_id = 0",
        (_PURGE_MARK + connector,),
    ).fetchone() is not None


def purge_connecteur_depose(conn, connector: str) -> int:
    """Efface la GOUVERNANCE laissée par un connecteur DÉPOSÉ — **une seule fois**.

    Déposer un connecteur retire son entrée du registre ; ses lignes d'exposition
    (`connector_availability`) et d'ACL (`connector_acl`), elles, survivent — rien ne
    les nettoie, les `DELETE` existants sont des gestes d'admin explicites. Elles
    dorment sans effet tant que le nom reste mort.

    Elles se réveillent le jour où on **reprend le nom** pour autre chose, et en
    silence : le fan-out qui installe le nouveau connecteur pose ses lignes en
    `ON CONFLICT DO NOTHING` — à dessein, un réglage d'admin déjà pris doit gagner —
    donc la ligne FOSSILE gagne. Une org qui avait coupé l'ancien `linkedin` (données
    achetées AI Ark) verrait le nouveau (session hébergée) coupé lui aussi ; une org
    qui l'avait réservé à une équipe le verrait réservé. Un verdict porté sur un AUTRE
    produit, hérité sans que personne ne l'ait décidé, et sans rien qui échoue : la
    carte est simplement absente ou grise.

    **ONE-SHOT, et c'est structurel, pas une optimisation.** Rejouée à chaque boot,
    la purge effacerait les décisions prises DEPUIS sur le nouveau connecteur — un
    admin coupe LinkedIn pour son org le lundi, le boot du mardi le rallume. Le geste
    ne s'adresse qu'aux fossiles, et un fossile n'existe qu'une fois.

    Renvoie le nombre de lignes effacées : 0 au rejeu, et 0 aussi s'il n'y avait rien
    à purger (un nom neuf n'a pas de passé) — les deux sont des succès."""
    if _purge_faite(conn, connector):
        return 0
    n = 0
    for table in ("connector_availability", "connector_acl"):
        cur = conn.execute(f"DELETE FROM {table} WHERE connector = %s", (connector,))
        n += cur.rowcount or 0
    conn.execute(
        "INSERT INTO connector_selection_seeded (sub, org_id) VALUES (%s, 0) "
        "ON CONFLICT DO NOTHING",
        (_PURGE_MARK + connector,),
    )
    return n


def fanout_availability(conn, source: str, targets: tuple[str, ...]) -> int:
    """Étend à `targets` l'exposition de `source` — aux TROIS scopes.

    Le seed initial (`seed_initial`) ne joue qu'une fois, sur une table vide : un
    connecteur ajouté après lui n'a AUCUNE ligne platform, donc reste OFF
    (deny-by-default). C'est la bonne règle pour un connecteur neuf — et le pire cas
    possible pour un connecteur SCINDÉ : le jour du split unipile (2026-08-28), les
    six canaux naissent OFF et toute la messagerie hébergée s'éteint pour tout le
    monde, alors que rien n'a été désactivé.

    On recopie donc les trois scopes, pas seulement le master :
    · `platform` — l'interrupteur global suit le connecteur d'origine ;
    · `org`      — un override d'org (ON comme OFF) est une DÉCISION de cette org :
                   une org qui avait coupé unipile ne doit pas voir six canaux
                   s'allumer, et une org qui l'avait forcé ON les garde ;
    · `group`    — une coupure d'équipe est monotone (elle ne fait que retrancher) :
                   la perdre RELÂCHERAIT une restriction, jamais l'inverse.

    `ON CONFLICT DO NOTHING` : un réglage déjà posé sur une cible gagne (rejeu de
    boot, ou un admin qui a déjà tranché depuis). Idempotent."""
    n = 0
    for cible in targets:
        cur = conn.execute(
            "INSERT INTO connector_availability "
            "       (scope_type, scope_id, connector, enabled, set_by) "
            "SELECT scope_type, scope_id, %s, enabled, %s "
            "  FROM connector_availability WHERE connector = %s "
            "ON CONFLICT DO NOTHING",
            (cible, f"split:{source}", source),
        )
        n += cur.rowcount or 0
    return n


def fanout_acl(conn, source: str, targets: tuple[str, ...]) -> int:
    """Étend à `targets` l'ACL (`connector_acl`) de `source`.

    L'ACL est deny-by-default À LA PRÉSENCE (ADR 0025) : ≥1 ligne pour
    (scope, connector) ⟹ RESTREINT, aucune ⟹ ouvert. Un connecteur scindé dont
    l'ACL ne suit pas devient donc OUVERT À TOUS — la restriction ne « reste pas
    en place par défaut », elle s'ÉVAPORE. C'est le sens du fail-open qui rend ce
    geste obligatoire, et pas seulement souhaitable : une org qui avait réservé la
    messagerie à son équipe commerciale l'ouvrirait à tout le monde le jour du split,
    sans rien faire et sans rien voir.

    `granted_by` est conservé (l'audit dit QUI a posé la restriction d'origine) ;
    `granted_at` repart à NOW() par défaut de colonne — la ligne est neuve, la
    dater du geste original ferait croire à une décision qui n'a pas eu lieu."""
    n = 0
    for cible in targets:
        cur = conn.execute(
            "INSERT INTO connector_acl "
            "       (scope_type, scope_id, connector, principal_type, principal_id, granted_by) "
            "SELECT scope_type, scope_id, %s, principal_type, principal_id, granted_by "
            "  FROM connector_acl WHERE connector = %s "
            "ON CONFLICT DO NOTHING",
            (cible, source),
        )
        n += cur.rowcount or 0
    return n


# --- résolution (pure) ------------------------------------------------------

def _resolve(global_map: dict[str, bool], override_map: dict[str, bool]) -> set[str]:
    """Applique `override d'org > master plateforme > OFF`. Renvoie les connecteurs
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
    """exposé = override d'org si défini, sinon master plateforme, sinon OFF."""
    from .. import db

    with db._connect() as conn:
        if org_id is not None:
            row = conn.execute(
                "SELECT enabled FROM connector_availability "
                "WHERE scope_type = 'org' AND scope_id = %s AND connector = %s",
                (str(org_id), connector),
            ).fetchone()
            if row is not None:
                return bool(row["enabled"])
        row = conn.execute(
            "SELECT enabled FROM connector_availability "
            "WHERE scope_type = 'platform' AND connector = %s",
            (connector,),
        ).fetchone()
        return bool(row["enabled"]) if row is not None else False


def exposed_connectors(org_id: Optional[int] = None) -> set[str]:
    """Ensemble des connecteurs exposés (résout override d'org vs master en un
    scan). Pour filtrer le catalogue / le chargement en une requête."""
    from .. import db

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT scope_type, connector, enabled FROM connector_availability "
            "WHERE scope_type = 'platform' OR (scope_type = 'org' AND scope_id = %s)",
            (str(org_id) if org_id is not None else "",),
        ).fetchall()
    global_map: dict[str, bool] = {}
    override_map: dict[str, bool] = {}
    for r in rows:
        target = override_map if r["scope_type"] == "org" else global_map
        target[r["connector"]] = bool(r["enabled"])
    return _resolve(global_map, override_map)


def list_activations() -> list[dict]:
    """Toutes les lignes master plateforme + overrides d'org, pour la surface admin.
    Projection HISTORIQUE conservée : `org_id` (None = master) — les appelants
    (REST admin) n'ont pas bougé à l'unification."""
    from .. import db

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT scope_type, scope_id, connector, enabled, set_by, set_at "
            "FROM connector_availability WHERE scope_type IN ('platform', 'org') "
            "ORDER BY connector, (scope_type <> 'platform'), scope_id"
        ).fetchall()
    return [{"connector": r["connector"],
             "org_id": None if r["scope_type"] == "platform" else int(r["scope_id"]),
             "enabled": r["enabled"], "set_by": r["set_by"], "set_at": r["set_at"]}
            for r in rows]


# --- écritures (surface admin, B4) ------------------------------------------

def set_activation(connector: str, enabled: bool, org_id: Optional[int] = None,
                   set_by: Optional[str] = None) -> None:
    """Pose/maj l'activation : master plateforme si `org_id` None, sinon override d'org."""
    from .. import db

    scope_type, scope_id = ("platform", "") if org_id is None else ("org", str(org_id))
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO connector_availability (scope_type, scope_id, connector, enabled, set_by) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (scope_type, scope_id, connector) "
            "DO UPDATE SET enabled = EXCLUDED.enabled, set_by = EXCLUDED.set_by, set_at = NOW()",
            (scope_type, scope_id, connector, enabled, set_by),
        )


def clear_activation(connector: str, org_id: int) -> None:
    """Supprime un override d'org → le connecteur retombe sur le master plateforme."""
    from .. import db

    with db._connect() as conn:
        conn.execute(
            "DELETE FROM connector_availability "
            "WHERE scope_type = 'org' AND scope_id = %s AND connector = %s",
            (str(org_id), connector),
        )


# --- tier ÉQUIPE (restrict-only, ADR 0012) ----------------------------------

def group_cut_connectors(group_id: int) -> set[str]:
    """Connecteurs COUPÉS pour l'équipe (lignes `enabled=FALSE`). L'exposition
    effective d'un membre = `exposed_connectors(org) - group_cut_connectors(équipe
    active)` — invariant monotone : l'équipe ne peut que retrancher."""
    from .. import db

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT connector FROM connector_availability "
            "WHERE scope_type = 'group' AND scope_id = %s AND enabled = FALSE",
            (str(group_id),),
        ).fetchall()
    return {r["connector"] for r in rows}


def list_group_activations(group_id: int) -> list[dict]:
    """Lignes de coupure de l'équipe (surface admin d'équipe)."""
    from .. import db

    with db._connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT connector, enabled, set_by, set_at FROM connector_availability "
            "WHERE scope_type = 'group' AND scope_id = %s ORDER BY connector",
            (str(group_id),),
        ).fetchall()]


def set_group_activation(group_id: int, connector: str, enabled: bool,
                         set_by: Optional[str] = None) -> None:
    """Pose une coupure d'équipe. `enabled` DOIT être False (restrict-only) — la
    garde métier (invariant monotone) est dans la capacité ; ici on stocke."""
    from .. import db

    with db._connect() as conn:
        conn.execute(
            "INSERT INTO connector_availability (scope_type, scope_id, connector, enabled, set_by) "
            "VALUES ('group', %s, %s, %s, %s) "
            "ON CONFLICT (scope_type, scope_id, connector) "
            "DO UPDATE SET enabled = EXCLUDED.enabled, set_by = EXCLUDED.set_by, set_at = NOW()",
            (str(group_id), connector, enabled, set_by),
        )


def clear_group_activation(group_id: int, connector: str) -> None:
    """Retire la coupure d'équipe → le connecteur retombe sur l'exposition de l'org."""
    from .. import db

    with db._connect() as conn:
        conn.execute(
            "DELETE FROM connector_availability "
            "WHERE scope_type = 'group' AND scope_id = %s AND connector = %s",
            (str(group_id), connector),
        )
