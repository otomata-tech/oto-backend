"""La bascule des comptes sur les règles d'accès : reprise rejouée, anciens partages retirés.

oto-backend#1499, étape 3, lot L3 (docs/partage-de-compte.md § Les règles de comptes).
Une seule transaction, dans cet ordre :

1. la REPRISE des anciens partages en règles, rejouée (idempotente) : une sous-instance
   par compte Unipile, puis prêts Unipile (personne, équipe) et `share_side` du coffre →
   règles `sensitive` (avec la liste de `share_services` pour un compte Google ou
   Microsoft), une cible `org:<id>` → règle d'org, une règle d'org `sensitive` sur chaque
   compte d'org vivant. Un prêt entre orgs n'est PAS repris (décision D1 en attente,
   recommandée : couper) : il est journalisé, un par ligne. Une règle `manuelle` n'est
   jamais touchée. **La reprise est FIGÉE ici** : c'est la seule lecture des anciennes
   tables qui reste, et elle part avec elles — aucun code applicatif ne les lit plus ;
2. retrait de `connector_account_grants`, `connector_account_group_grants`,
   `unipile_operated_accounts` ;
3. retrait de `connector_credentials.share_side` (et de son index GIN) et
   `share_services`, qui ne servaient qu'aux prêts. `share_mode` et `share_down`
   RESTENT : ils portent l'accès aux clés plateforme (chaîne `grants`,
   `access/platform_grant`) ;
4. retrait de `connector_instances.visibility` (et de sa contrainte), que rien ne posait.

`connector_loan_events` reste, en lecture (l'historique des prêts d'avant).

**Verrous.** `AccessExclusiveLock` sur les trois tables retirées et, le temps des
`ALTER … DROP COLUMN` (catalogue seul, pas de réécriture), sur `connector_credentials` et
`connector_instances`. `lock_timeout` 5 s : un verrou qui ne vient pas fait échouer la
révision entière, rien n'est à moitié fait.

**Ordre avec le code : APRÈS le tag qui porte la bascule.** Le code d'avant lit les
tables et colonnes retirées ; celui d'après ne les lit plus et ne les recrée pas au
démarrage. Prod et préprod partagent la base.

Essai à blanc (`--sql`) : il imprime les retraits, pas la reprise, qui lit la base.

Le journal des partages gagne le changement de propriétaire (`action='transferred'`,
colonne `owner_before`, `db/transfert_de_compte`).

Retour arrière : recrée les structures VIDES (tables, colonnes à leur défaut, index,
contrainte) et les MARQUE (`COMMENT ON TABLE connector_account_grants`). Les règles
restent : la bascule ne se défait pas en rejouant les prêts. Une remontée sur des
structures ainsi recréées reprend ce que l'ancien code y a posé depuis, sans retirer une
règle reprise faute de prêt : le vide vient du retour arrière, pas d'un retrait.

Révision : 20261105_1000_bascule_comptes
Précédente : 20261105_0900_regles_de_comptes
"""
from __future__ import annotations

import logging

from alembic import context, op
from psycopg.rows import dict_row, tuple_row

revision = "20261105_1000_bascule_comptes"
down_revision = "20261105_0900_regles_de_comptes"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"
logger = logging.getLogger("alembic.runtime.migration")

_RETRAITS = (
    "DROP TABLE IF EXISTS connector_account_group_grants",
    "DROP TABLE IF EXISTS connector_account_grants",
    "DROP TABLE IF EXISTS unipile_operated_accounts",
    "DROP INDEX IF EXISTS idx_conn_cred_share_side",
    "ALTER TABLE connector_credentials DROP COLUMN IF EXISTS share_side",
    "ALTER TABLE connector_credentials DROP COLUMN IF EXISTS share_services",
    "ALTER TABLE connector_instances DROP CONSTRAINT IF EXISTS "
    "connector_instances_visibility_check",
    "ALTER TABLE connector_instances DROP COLUMN IF EXISTS visibility",
)

#: Le déclencheur du journal tel que L1 l'a posé (sans `hors_org`), FIGÉ : le retour
#: arrière le repose quand il retire la colonne.
_JOURNAL_D_AVANT = """
CREATE OR REPLACE FUNCTION connector_account_share_journal() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    r connector_account_rules;
BEGIN
    IF TG_OP = 'DELETE' THEN
        r := OLD;
    ELSE
        r := NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.level IS NOT DISTINCT FROM OLD.level
       AND NEW.services IS NOT DISTINCT FROM OLD.services THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'DELETE' AND (
           NOT EXISTS (SELECT 1 FROM connector_instances WHERE id = OLD.instance_id)
           OR NOT EXISTS (SELECT 1 FROM orgs WHERE id = OLD.org_id)) THEN
        RETURN NULL;
    END IF;
    INSERT INTO connector_account_share_events
        (org_id, instance_id, action, subject_kind, subject_id, level, previous_level,
         services, actor_sub)
    VALUES (r.org_id, r.instance_id,
            CASE WHEN TG_OP = 'DELETE' THEN 'removed' ELSE 'set' END,
            CASE WHEN r.subject_org THEN 'org'
                 WHEN r.subject_group IS NOT NULL THEN 'group' ELSE 'user' END,
            COALESCE(r.subject_user, r.subject_group::text),
            r.level,
            CASE WHEN TG_OP = 'UPDATE' THEN OLD.level END,
            r.services,
            COALESCE(NULLIF(current_setting('oto.acteur', true), ''),
                     CASE WHEN TG_OP = 'INSERT' THEN r.created_by END));
    RETURN NULL;
END $$"""

#: Le déclencheur du journal de la bascule (avec `hors_org`), FIGÉ.
_JOURNAL_DE_LA_BASCULE = """
CREATE OR REPLACE FUNCTION connector_account_share_journal() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    r connector_account_rules;
BEGIN
    IF TG_OP = 'DELETE' THEN
        r := OLD;
    ELSE
        r := NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.level IS NOT DISTINCT FROM OLD.level
       AND NEW.services IS NOT DISTINCT FROM OLD.services THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'DELETE' AND (
           NOT EXISTS (SELECT 1 FROM connector_instances WHERE id = OLD.instance_id)
           OR NOT EXISTS (SELECT 1 FROM orgs WHERE id = OLD.org_id)) THEN
        RETURN NULL;
    END IF;
    INSERT INTO connector_account_share_events
        (org_id, instance_id, action, subject_kind, subject_id, level, previous_level,
         services, actor_sub, hors_org)
    VALUES (r.org_id, r.instance_id,
            CASE WHEN TG_OP = 'DELETE' THEN 'removed' ELSE 'set' END,
            CASE WHEN r.subject_org THEN 'org'
                 WHEN r.subject_group IS NOT NULL THEN 'group' ELSE 'user' END,
            COALESCE(r.subject_user, r.subject_group::text),
            r.level,
            CASE WHEN TG_OP = 'UPDATE' THEN OLD.level END,
            r.services,
            COALESCE(NULLIF(current_setting('oto.acteur', true), ''),
                     CASE WHEN TG_OP = 'INSERT' THEN r.created_by END),
            r.subject_user IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM org_members m
                 WHERE m.org_id = r.org_id AND m.sub = r.subject_user));
    RETURN NULL;
END $$"""

#: Le journal des partages, AVANT la reprise (qui l'écrit) : il dit si le sujet d'une
#: règle est hors de l'org du compte (D1), et note un changement de propriétaire.
_JOURNAL = (
    "ALTER TABLE connector_account_share_events ADD COLUMN IF NOT EXISTS hors_org "
    "BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE connector_account_share_events ADD COLUMN IF NOT EXISTS owner_before TEXT",
    "ALTER TABLE connector_account_share_events DROP CONSTRAINT IF EXISTS "
    "connector_account_share_events_action_check",
    "ALTER TABLE connector_account_share_events ADD CONSTRAINT "
    "connector_account_share_events_action_check "
    "CHECK (action IN ('set', 'removed', 'transferred'))",
    _JOURNAL_DE_LA_BASCULE,
)

#: La marque des structures recréées vides par le retour arrière.
_RECREEES = f"recreee_vide_par_{revision}"

_RECREATIONS = (
    """CREATE TABLE IF NOT EXISTS connector_account_grants (
    owner_sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    grantee_sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
    granted_by TEXT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (owner_sub, provider, grantee_sub))""",
    "CREATE INDEX IF NOT EXISTS idx_account_grants_grantee "
    "ON connector_account_grants(grantee_sub, provider)",
    """CREATE TABLE IF NOT EXISTS unipile_operated_accounts (
    sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    owner_sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
    selected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sub, provider))""",
    """CREATE TABLE IF NOT EXISTS connector_account_group_grants (
    owner_sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    grantee_group_id BIGINT NOT NULL REFERENCES org_groups(id) ON DELETE CASCADE,
    granted_by TEXT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (owner_sub, provider, grantee_group_id))""",
    "CREATE INDEX IF NOT EXISTS idx_account_group_grants_group "
    "ON connector_account_group_grants(grantee_group_id, provider)",
    "ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS share_side JSONB "
    "NOT NULL DEFAULT '[]'::jsonb",
    "ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS share_services JSONB "
    "NOT NULL DEFAULT '{}'::jsonb",
    "CREATE INDEX IF NOT EXISTS idx_conn_cred_share_side ON connector_credentials "
    "USING gin (share_side)",
    "ALTER TABLE connector_instances ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL "
    "DEFAULT 'inherited'",
    "ALTER TABLE connector_instances ADD CONSTRAINT connector_instances_visibility_check "
    "CHECK (visibility IN ('inherited', 'hidden', 'org'))",
    f"COMMENT ON TABLE connector_account_grants IS '{_RECREEES}'",
    # Un transfert ne se défait pas : sa ligne de journal part avec la colonne.
    "DELETE FROM connector_account_share_events WHERE action = 'transferred'",
    "ALTER TABLE connector_account_share_events DROP CONSTRAINT IF EXISTS "
    "connector_account_share_events_action_check",
    "ALTER TABLE connector_account_share_events ADD CONSTRAINT "
    "connector_account_share_events_action_check CHECK (action IN ('set', 'removed'))",
    "ALTER TABLE connector_account_share_events DROP COLUMN IF EXISTS owner_before",
    _JOURNAL_D_AVANT,
    "ALTER TABLE connector_account_share_events DROP COLUMN IF EXISTS hors_org",
)


# ── La reprise, figée (seule lecture des anciennes tables) ───────────────────────

NIVEAU_REPRIS = "sensitive"
ORIGINE_REPRISE = "reprise"
#: Les porteurs dont un prêt ne prêtait qu'une liste de services (Google, Microsoft).
_PORTEURS_A_SERVICES = frozenset({"google", "microsoft"})

_JOINTURE = ("si.owner_type = 'member' AND si.owner_id = ua.org_id::text || ':' || ua.sub "
             "AND si.connector = 'unipile' AND si.account = 'canal:' || ua.provider "
             "AND si.revoked_at IS NULL")

_PRETS_UNIPILE = f"""
SELECT g.owner_sub, g.provider, g.grantee_sub AS cible, ua.org_id, si.id AS instance_id
  FROM connector_account_grants g
  LEFT JOIN unipile_accounts ua ON ua.sub = g.owner_sub AND ua.provider = g.provider
  LEFT JOIN connector_instances si ON {_JOINTURE}
 ORDER BY 1, 2, 3, 4
"""

_PRETS_UNIPILE_EQUIPE = f"""
SELECT gg.owner_sub, gg.provider, gg.grantee_group_id AS cible, gr.org_id AS org_cible,
       ua.org_id, si.id AS instance_id
  FROM connector_account_group_grants gg
  JOIN org_groups gr ON gr.id = gg.grantee_group_id
  LEFT JOIN unipile_accounts ua ON ua.sub = gg.owner_sub AND ua.provider = gg.provider
  LEFT JOIN connector_instances si ON {_JOINTURE}
 ORDER BY 1, 2, 3, 5
"""

_PRETS_DU_COFFRE = """
SELECT c.entity_type, c.entity_id, c.connector, c.account, t.cible,
       c.share_services -> t.cible AS services, i.id AS instance_id
  FROM connector_credentials c
 CROSS JOIN LATERAL jsonb_array_elements_text(
       CASE WHEN jsonb_typeof(c.share_side) = 'array' THEN c.share_side
            ELSE '[]'::jsonb END) AS t(cible)
  LEFT JOIN connector_instances i
    ON i.owner_type = c.entity_type AND i.owner_id = c.entity_id
   AND i.connector = c.connector AND i.account = c.account AND i.revoked_at IS NULL
 ORDER BY 1, 2, 3, 4, 5
"""

_COMPTES_D_ORG = """
SELECT i.id AS instance_id, o.id AS org_id
  FROM connector_instances i JOIN orgs o ON o.id::text = i.owner_id
 WHERE i.owner_type = 'org' AND i.revoked_at IS NULL
 ORDER BY 1
"""


def _sujet(cible: str) -> tuple:
    genre, _, ident = cible.partition(":")
    if genre == "user" and ident:
        return (ident, None, False)
    if genre == "group" and ident.isdigit():
        return (None, int(ident), False)
    if genre == "org" and ident.isdigit():
        return (None, None, True)
    raise ValueError(cible)


def _org_et_proprietaire(groupes: dict, entity_type: str, entity_id: str):
    if entity_type == "member":
        org, _, sub = entity_id.partition(":")
        return (int(org) if org.isdigit() else None), sub
    if entity_type == "org":
        return (int(entity_id) if entity_id.isdigit() else None), None
    if entity_type == "group" and entity_id.isdigit():
        return groupes.get(int(entity_id)), None
    return None, (entity_id if entity_type == "user" else None)


def calculer_les_regles(cur) -> tuple[dict, list]:
    """`({(instance_id, user, group, org): {org_id, services}}, non_reprises)` — les règles
    que les anciens partages donnent, et ce qu'ils ne donnent pas.

    Un prêt à une PERSONNE hors de l'org du compte devient une règle hors org (D1 tranchée
    le 04/11/2026 : le partage entre orgs est gardé, à une personne) ; un prêt à une
    équipe ou à une org d'une autre org n'est pas repris (une règle hors org ne vise
    qu'une personne)."""
    membres = {(r["org_id"], r["sub"]) for r in cur.execute(
        "SELECT m.org_id, m.sub FROM org_members m JOIN users u ON u.sub = m.sub")}
    personnes = {r["sub"] for r in cur.execute("SELECT sub FROM users")}
    groupes = {r["id"]: r["org_id"] for r in cur.execute("SELECT id, org_id FROM org_groups")}
    regles: dict = {}
    ecartes: list = []

    def ajouter(instance_id, org_id, sujet, services):
        cle = (instance_id, *sujet)
        deja = regles.get(cle)
        if deja is None:
            regles[cle] = {"org_id": org_id, "services": services}
        elif deja["services"] is None or services is None:
            deja["services"] = None    # deux partages vers le même sujet : le plus large
        else:
            deja["services"] = sorted(set(deja["services"]) | set(services))

    def ecarter(source, cible, motif, **quoi):
        ecartes.append({"source": source, "cible": cible, "motif": motif, **quoi})

    for r in cur.execute(_PRETS_UNIPILE).fetchall():
        quoi = {"proprietaire": r["owner_sub"], "canal": r["provider"],
                "org_id": r["org_id"], "instance_id": r["instance_id"]}
        if r["instance_id"] is None:
            ecarter("unipile_pret", f"user:{r['cible']}", "sans_compte", **quoi)
        elif r["cible"] == r["owner_sub"]:
            ecarter("unipile_pret", f"user:{r['cible']}", "proprietaire", **quoi)
        elif (r["org_id"], r["cible"]) not in membres and r["cible"] not in personnes:
            ecarter("unipile_pret", f"user:{r['cible']}", "personne_inconnue", **quoi)
        else:
            ajouter(r["instance_id"], r["org_id"], (r["cible"], None, False), None)

    for r in cur.execute(_PRETS_UNIPILE_EQUIPE).fetchall():
        quoi = {"proprietaire": r["owner_sub"], "canal": r["provider"],
                "org_id": r["org_id"], "instance_id": r["instance_id"]}
        if r["instance_id"] is None:
            ecarter("unipile_pret_equipe", f"group:{r['cible']}", "sans_compte", **quoi)
        elif r["org_cible"] != r["org_id"]:
            ecarter("unipile_pret_equipe", f"group:{r['cible']}", "hors_org", **quoi)
        else:
            ajouter(r["instance_id"], r["org_id"], (None, r["cible"], False), None)

    for r in cur.execute(_PRETS_DU_COFFRE).fetchall():
        cible = r["cible"]
        org, proprietaire = _org_et_proprietaire(groupes, r["entity_type"], r["entity_id"])
        quoi = {"connecteur": r["connector"], "org_id": org, "instance_id": r["instance_id"]}
        try:
            sujet = _sujet(cible)
        except ValueError:
            ecarter("pret_du_coffre", cible, "cible_inconnue", **quoi)
            continue
        if r["instance_id"] is None or org is None:
            ecarter("pret_du_coffre", cible,
                    "sans_instance" if r["instance_id"] is None else "sans_org", **quoi)
            continue
        user, groupe, _ = sujet
        dans_org = ((user is not None and (org, user) in membres)
                    or (groupe is not None and groupes.get(groupe) == org)
                    or (sujet[2] and cible == f"org:{org}"))
        if user is not None and user == proprietaire:
            ecarter("pret_du_coffre", cible, "proprietaire", **quoi)
            continue
        if not dans_org and not (user is not None and user in personnes):
            ecarter("pret_du_coffre", cible,
                    "hors_org" if user is None else "personne_inconnue", **quoi)
            continue
        services = r["services"]
        if isinstance(services, list):
            services = sorted({str(s) for s in services})
        elif r["connector"] in _PORTEURS_A_SERVICES:
            services = []          # porteur à services sans liste : rien de prêté
        else:
            services = None
        ajouter(r["instance_id"], org, sujet, services)

    for r in cur.execute(_COMPTES_D_ORG).fetchall():
        ajouter(r["instance_id"], r["org_id"], (None, None, True), None)
    return regles, ecartes


def reprendre_les_prets(conn, *, retirer_perimees: bool = True) -> dict:
    """RECALCULE les règles reprises, dans la transaction de la révision. Idempotente.
    Une règle reprise que plus aucun partage ne donne est retirée (sauf
    `retirer_perimees=False` : structures recréées vides par le retour arrière) ; une
    règle `manuelle` n'est jamais touchée. Rend le bilan."""
    from oto_mcp.db import sous_instances

    cur = conn.cursor(row_factory=dict_row)
    bilan = {"sous_instances": sous_instances.poser_toutes(cur)}
    voulues, non_reprises = calculer_les_regles(cur)
    existantes = {
        (r["instance_id"], r["subject_user"], r["subject_group"], r["subject_org"]): r
        for r in cur.execute(
            "SELECT id, instance_id, subject_user, subject_group, subject_org, level, "
            "services, origine FROM connector_account_rules").fetchall()}
    posees = mises_a_jour = gardees = 0
    for cle, regle in sorted(voulues.items(), key=lambda kv: repr(kv[0])):
        deja = existantes.get(cle)
        if deja is None:
            cur.execute(
                "INSERT INTO connector_account_rules (org_id, instance_id, subject_user, "
                "subject_group, subject_org, level, services, origine) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (regle["org_id"], *cle, NIVEAU_REPRIS, regle["services"], ORIGINE_REPRISE))
            posees += 1
        elif deja["origine"] != ORIGINE_REPRISE:
            gardees += 1
        elif deja["level"] != NIVEAU_REPRIS or deja["services"] != regle["services"]:
            cur.execute("UPDATE connector_account_rules SET level = %s, services = %s, "
                        "updated_at = NOW() WHERE id = %s",
                        (NIVEAU_REPRIS, regle["services"], deja["id"]))
            mises_a_jour += 1
    perimees = [r["id"] for cle, r in existantes.items()
                if retirer_perimees and r["origine"] == ORIGINE_REPRISE
                and cle not in voulues]
    if perimees:
        cur.execute("DELETE FROM connector_account_rules WHERE id = ANY(%s)", (perimees,))
    bilan.update(posees=posees, mises_a_jour=mises_a_jour, retirees=len(perimees),
                 manuelles_gardees=gardees, non_reprises=non_reprises)
    return bilan


def _recreees_par_le_retour_arriere(conn) -> bool:
    return conn.cursor(row_factory=tuple_row).execute(
        "SELECT obj_description('connector_account_grants'::regclass, 'pg_class') = %s",
        (_RECREEES,)).fetchone()[0] is True


def _anciennes_tables_presentes(conn) -> bool:
    return conn.cursor(row_factory=tuple_row).execute(
        "SELECT to_regclass('connector_account_grants') IS NOT NULL "
        "AND to_regclass('connector_account_group_grants') IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = "
        "current_schema() AND table_name = 'connector_credentials' "
        "AND column_name = 'share_side')").fetchone()[0]


_ARCHIVE_DES_CLES_DE_PROJET = (
    "UPDATE grants SET revoked_at = NOW() "
    "WHERE resource_kind = 'project_credentials' AND revoked_at IS NULL")


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    for ordre in _JOURNAL:
        op.execute(ordre)
    if not context.is_offline_mode():
        conn = op.get_bind().connection.driver_connection
        # Rejouée sur une base déjà basculée (tables parties) : rien à reprendre.
        if _anciennes_tables_presentes(conn):
            bilan = reprendre_les_prets(
                conn, retirer_perimees=not _recreees_par_le_retour_arriere(conn))
            logger.info("%s : %s", revision,
                        {k: v for k, v in bilan.items() if k != "non_reprises"}
                        | {"non_reprises": len(bilan["non_reprises"])})
            for e in bilan["non_reprises"]:
                logger.warning("%s : prêt NON repris (%s) %s", revision, e["motif"], e)
    for ordre in _RETRAITS:
        op.execute(ordre)
    # Le partage des clés d'un projet (#480) n'existe plus : ses grants vivants sont
    # archivés (la ligne reste, datée). Le retour arrière ne les rouvre pas — on ne
    # distinguerait pas un grant archivé ici d'un grant révoqué avant.
    op.execute(_ARCHIVE_DES_CLES_DE_PROJET)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    for ordre in _RECREATIONS:
        op.execute(ordre)
