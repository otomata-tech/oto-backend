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
    scope_type TEXT NOT NULL CHECK (scope_type IN ('platform','org','tenant','group')),
    scope_id   TEXT NOT NULL DEFAULT '',   -- '' pour platform ; org.id / group.id en texte ; slug pour tenant
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


# --- résolution (pure) ------------------------------------------------------

def _resolve(global_map: dict[str, bool], override_map: dict[str, bool],
             tenant_map: "dict[str, bool] | None" = None) -> set[str]:
    """Applique `override d'org > master plateforme > OFF`, sous le PLAFOND du tenant.
    Renvoie les connecteurs exposés. Pur (pas de DB) → testable hors connexion.

    Le cran TENANT (2026-09-26) : un partenaire qui sert oto sous sa marque coupe, pour
    TOUTES ses orgs d'un coup, un connecteur que son offre ne comprend pas — sans
    poser un override sur chacune, et sans qu'un admin d'org puisse le rouvrir. C'est
    un plafond, comme la plateforme : il ne fait que RETRANCHER (`enabled=false`),
    une ligne à `true` n'expose rien que la plateforme n'expose déjà. Motif concret :
    un service Google que le projet Google Cloud du tenant ne déclare pas — le
    consentement échouerait chez Google, la carte ne doit pas exister chez lui."""
    names = set(global_map) | set(override_map)
    exposed = {n for n in names if override_map.get(n, global_map.get(n, False))}
    if not tenant_map:
        return exposed
    return {n for n in exposed if tenant_map.get(n, True)}


def effective_for_group(exposed: set[str], group_cut: set[str]) -> set[str]:
    """Exposition EFFECTIVE pour un membre d'une équipe = ce que l'org expose MOINS
    les coupures de l'équipe active. Invariant MONOTONE (ADR 0012) : l'équipe ne peut
    que RETRANCHER — jamais rendre visible un connecteur que l'org a coupé. Pur
    (pas de DB) → testable hors connexion."""
    return exposed - group_cut


# --- lectures (self-managing) -----------------------------------------------

def tenant_of_org(org_id: Optional[int]) -> Optional[str]:
    """Le slug du tenant qui HÉBERGE cette org, ou `None` (tenant primaire, ou pas
    d'org) — le seul cas où le cran tenant n'existe pas. Lu par `db.org_tenant_slug`
    (l'union des trois axes, `docs/tenants.md`), jamais deviné."""
    if org_id is None:
        return None
    from .. import db, tenancy
    slug = db.org_tenant_slug(int(org_id))
    return None if not slug or slug == tenancy.PRIMARY_SLUG else slug


def is_exposed(connector: str, org_id: Optional[int] = None) -> bool:
    """exposé = override d'org si défini, sinon master plateforme, sinon OFF."""
    return cran_qui_coupe(connector, org_id) is None


def cran_qui_coupe(connector: str, org_id: Optional[int] = None,
                   group_id: Optional[int] = None) -> Optional[str]:
    """Le cran qui COUPE `connector` pour (org, équipe), ou `None` s'il est exposé.

    Même résolution que `effective_for_group(exposed_connectors(org), group_cut…)`,
    pour UN connecteur : `'org'` = override d'org à OFF ; `'platform'` = pas
    d'override d'org et master OFF ou absent (deny-by-default) ; `'group'` = exposé
    pour l'org mais coupé par l'équipe `group_id`. Nommer le cran, c'est nommer QUI
    peut rouvrir — ce que le refus d'appel (`activation_gate`) dit à l'agent."""
    from .. import db

    slug = tenant_of_org(org_id)
    with db._connect() as conn:
        # Le plafond du TENANT d'abord : coupé là, personne dans l'org ne rouvre —
        # ni un override d'org ON, ni une équipe. Nommer ce cran, c'est dire que le
        # geste est chez l'hébergeur.
        if slug is not None and conn.execute(
                "SELECT 1 FROM connector_availability "
                "WHERE scope_type = 'tenant' AND scope_id = %s AND connector = %s "
                "AND enabled = FALSE",
                (slug, connector),
        ).fetchone() is not None:
            return "tenant"
        org_row = None
        if org_id is not None:
            org_row = conn.execute(
                "SELECT enabled FROM connector_availability "
                "WHERE scope_type = 'org' AND scope_id = %s AND connector = %s",
                (str(org_id), connector),
            ).fetchone()
        if org_row is not None:
            if not org_row["enabled"]:
                return "org"
        else:
            row = conn.execute(
                "SELECT enabled FROM connector_availability "
                "WHERE scope_type = 'platform' AND connector = %s",
                (connector,),
            ).fetchone()
            if row is None or not row["enabled"]:
                return "platform"
        if group_id is not None and conn.execute(
                "SELECT 1 FROM connector_availability "
                "WHERE scope_type = 'group' AND scope_id = %s AND connector = %s "
                "AND enabled = FALSE",
                (str(group_id), connector),
        ).fetchone() is not None:
            return "group"
    return None


def exposed_connectors(org_id: Optional[int] = None) -> set[str]:
    """Ensemble des connecteurs exposés (résout override d'org vs master en un
    scan). Pour filtrer le catalogue / le chargement en une requête."""
    from .. import db

    slug = tenant_of_org(org_id)
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT scope_type, connector, enabled FROM connector_availability "
            "WHERE scope_type = 'platform' OR (scope_type = 'org' AND scope_id = %s)"
            "   OR (scope_type = 'tenant' AND scope_id = %s)",
            (str(org_id) if org_id is not None else "", slug or ""),
        ).fetchall()
    global_map: dict[str, bool] = {}
    override_map: dict[str, bool] = {}
    tenant_map: dict[str, bool] = {}
    for r in rows:
        target = {"org": override_map, "tenant": tenant_map}.get(r["scope_type"], global_map)
        target[r["connector"]] = bool(r["enabled"])
    return _resolve(global_map, override_map, tenant_map)


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


# --- tier TENANT (plafond, 2026-09-26) ---------------------------------------

def list_tenant_activations(slug: str) -> dict[str, bool]:
    """Les coupures (et lignes) posées par ce tenant : `{connector: enabled}`."""
    from .. import db

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT connector, enabled FROM connector_availability "
            "WHERE scope_type = 'tenant' AND scope_id = %s ORDER BY connector",
            (slug,),
        ).fetchall()
    return {r["connector"]: bool(r["enabled"]) for r in rows}


def set_tenant_activation(slug: str, connector: str, enabled: bool,
                          set_by: Optional[str] = None) -> None:
    """Pose/maj la ligne tenant. `enabled=false` coupe pour toutes les orgs du tenant ;
    `true` ne fait que retirer la coupure (le plafond plateforme reste le sien)."""
    from .. import db

    with db._connect() as conn:
        conn.execute(
            "INSERT INTO connector_availability (scope_type, scope_id, connector, enabled, set_by) "
            "VALUES ('tenant', %s, %s, %s, %s) "
            "ON CONFLICT (scope_type, scope_id, connector) "
            "DO UPDATE SET enabled = EXCLUDED.enabled, set_by = EXCLUDED.set_by, set_at = NOW()",
            (slug, connector, enabled, set_by),
        )


def clear_tenant_activation(slug: str, connector: str) -> None:
    """Retire la ligne tenant → le connecteur suit à nouveau la plateforme."""
    from .. import db

    with db._connect() as conn:
        conn.execute(
            "DELETE FROM connector_availability "
            "WHERE scope_type = 'tenant' AND scope_id = %s AND connector = %s",
            (slug, connector),
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
