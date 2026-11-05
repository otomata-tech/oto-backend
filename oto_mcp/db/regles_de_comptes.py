"""Les RÈGLES d'accès des comptes de connecteur : leur journal, leur garde, leur lecture
et leurs gestes (oto-backend#1499, étape 3).

Le modèle est celui d'oto-core (`access_rules` sur un compte) : une règle par
(compte, sujet), le sujet étant une personne, une équipe ou l'org, à un niveau
`none` < `read` < `write` < `sensitive` < `manage`. Les tables vivent dans
`db/schema/connectors.ACCOUNT_RULES` ; le modèle et ses écarts dans
`docs/partage-de-compte.md` § Les règles de comptes.

Depuis la bascule (lot L3), la résolution d'un compte et la garde de niveau lisent les
règles (`access/niveaux`, `access/comptes`) ; les anciens partages n'existent plus. Leur
reprise vit, figée, dans la révision de la bascule (`20261105_1000_bascule_comptes`), la
seule à lire encore les anciennes tables.

**Deux déclencheurs**, posés par le démarrage (après `_SCHEMA`) et par la révision
`20261105_0900_regles_de_comptes` :
- le JOURNAL (`connector_account_share_events`) : une règle posée, son niveau ou ses
  services changés, ou retirée — dans la transaction du geste, quel que soit le code qui
  écrit. L'auteur est `oto.acteur` de la transaction (`db/estampille`), sinon, à la
  pose, `created_by` ; NULL pour un geste hors serveur (la reprise). Une règle partie
  avec son compte ou son org n'écrit rien : la ligne partirait avec eux ;
- la garde `owner_subject` : une règle qui nomme le propriétaire d'un compte personnel
  sur ce compte est refusée (`check_violation`, contrainte
  `connector_account_rules_owner_subject`) — elle ne changerait rien, son propriétaire le
  gère avant toute règle (oto-core, décision du 19/10).
Les fonctions sont reposées à chaque démarrage (`CREATE OR REPLACE`, catalogue seul) ;
les déclencheurs seulement s'ils manquent (même prix que `db/journal_revisions`).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from ._conn import _connect
from .lecture_d_appel import lecture_d_appel

logger = logging.getLogger(__name__)

ORIGINE_REPRISE = "reprise"
ORIGINE_MANUELLE = "manuelle"

_FONCTION_JOURNAL = """
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

_FONCTION_GARDE = """
CREATE OR REPLACE FUNCTION connector_account_rules_owner_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    t text;
    i text;
BEGIN
    IF NEW.subject_user IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT owner_type, owner_id INTO t, i FROM connector_instances WHERE id = NEW.instance_id;
    IF (t = 'user' AND i = NEW.subject_user)
       OR (t = 'member' AND substr(i, strpos(i, ':') + 1) = NEW.subject_user) THEN
        RAISE EXCEPTION 'owner_subject: a rule cannot name the owner of a personal account (instance %)',
            NEW.instance_id
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'connector_account_rules_owner_subject';
    END IF;
    RETURN NEW;
END $$"""

DECLENCHEUR_JOURNAL = "connector_account_rules_share_journal"
DECLENCHEUR_GARDE = "connector_account_rules_owner_guard"
_DECLENCHEURS = (
    (DECLENCHEUR_GARDE, "BEFORE INSERT OR UPDATE OF subject_user, instance_id",
     "connector_account_rules_owner_guard"),
    (DECLENCHEUR_JOURNAL, "AFTER INSERT OR DELETE OR UPDATE OF level, services",
     "connector_account_share_journal"),
)


def poser_journal_des_partages(conn) -> list[str]:
    """Fonctions et déclencheurs des règles, APRÈS `_SCHEMA` (qui crée les tables). Rend
    les déclencheurs créés (vide au démarrage ordinaire)."""
    conn.execute(_FONCTION_JOURNAL)
    conn.execute(_FONCTION_GARDE)
    poses = []
    for nom, quand, fonction in _DECLENCHEURS:
        if conn.execute("SELECT 1 FROM pg_trigger WHERE tgrelid = "
                        "'connector_account_rules'::regclass AND tgname = %s",
                        (nom,)).fetchone():
            continue
        conn.execute(f"CREATE TRIGGER {nom} {quand} ON connector_account_rules "
                     f"FOR EACH ROW EXECUTE FUNCTION {fonction}()")
        poses.append(nom)
    return poses


# ── La lecture : les comptes d'un connecteur, avec leurs règles ──────────────────

# Un compte vivant, son org (celle de l'équipe pour un compte d'équipe), son défaut
# (`meta.is_default` de sa ligne de coffre) et ses règles, en UNE requête.
_COMPTES = """
SELECT i.id AS instance_id, i.owner_type, i.owner_id, i.connector, i.account, i.label,
       CASE i.owner_type
            WHEN 'org' THEN CASE WHEN i.owner_id ~ '^[0-9]+$' THEN i.owner_id::bigint END
            WHEN 'member' THEN CASE WHEN i.owner_id ~ '^[0-9]+:'
                                    THEN split_part(i.owner_id, ':', 1)::bigint END
            WHEN 'group' THEN g.org_id END AS org_id,
       COALESCE(c.meta->>'is_default', '') = 'true' AS is_default,
       c.meta->>'default_set_by' AS default_par, c.meta->>'default_set_at' AS default_le,
       o.name AS org_nom, c.set_at AS pose_le,
       COALESCE((SELECT jsonb_agg(jsonb_build_object(
                     'user', r.subject_user, 'group', r.subject_group, 'org', r.subject_org,
                     'level', r.level, 'services', r.services) ORDER BY r.id)
                   FROM connector_account_rules r WHERE r.instance_id = i.id),
                '[]'::jsonb) AS regles
  FROM connector_instances i
  LEFT JOIN org_groups g ON i.owner_type = 'group' AND i.owner_id = g.id::text
  LEFT JOIN orgs o ON o.id = CASE i.owner_type
            WHEN 'org' THEN CASE WHEN i.owner_id ~ '^[0-9]+$' THEN i.owner_id::bigint END
            WHEN 'member' THEN CASE WHEN i.owner_id ~ '^[0-9]+:'
                                    THEN split_part(i.owner_id, ':', 1)::bigint END
            WHEN 'group' THEN g.org_id END
  LEFT JOIN connector_credentials c
    ON c.entity_type = i.owner_type AND c.entity_id = i.owner_id
   AND c.connector = i.connector AND c.account = i.account
 WHERE i.revoked_at IS NULL
   AND (%(connector)s::text IS NULL OR i.connector = %(connector)s) AND ({filtre})
 ORDER BY i.owner_type, i.owner_id, i.account
"""


# The rules that open an account to `%(sub)s`: one naming her, one on a team she is a
# member of, one on the whole org — what a LOAN is, whatever its origin.
_OUVRE_A_SUB = (
    "r.level <> 'none' AND (r.subject_user = %(sub)s OR r.subject_org "
    "OR r.subject_group IN (SELECT gm.group_id FROM org_group_members gm "
    "                        WHERE gm.sub = %(sub)s))")

# `_COMPTES` plus `prete_le`: when the most recent rule that opens the account to
# `%(sub)s` was set — the date of the loan, which orders step 5 (#1499).
_COMPTES_PRETES = _COMPTES.replace(
    "c.set_at AS pose_le,",
    "c.set_at AS pose_le, (SELECT max(r.created_at) FROM connector_account_rules r "
    f"WHERE r.instance_id = i.id AND {_OUVRE_A_SUB}) AS prete_le,")


def comptes_des_lignes(connector: str, lignes: list[tuple[str, str, str]]) -> list[dict]:
    """Les comptes vivants (instances) de ces lignes de coffre
    (`(entity_type, entity_id, account)` de `connector`), avec leurs règles."""
    if not lignes:
        return []
    filtre = ("(i.owner_type, i.owner_id, i.account) IN (SELECT * FROM unnest("
              "%(types)s::text[], %(ids)s::text[], %(comptes)s::text[]))")
    with _connect() as conn:
        return conn.execute(_COMPTES.format(filtre=filtre), {
            "connector": connector, "types": [t for t, _, _ in lignes],
            "ids": [str(i) for _, i, _ in lignes],
            "comptes": [a or "" for _, _, a in lignes]}).fetchall()


def comptes_d_autrui(connector: str, org_id: int, sub: str, account: str) -> list[dict]:
    """Les comptes de `connector` nommés `account` qu'aucune étape de l'org `org_id` ne
    tient pour `sub` : les comptes PERSONNELS que d'autres y possèdent, et ceux de l'org
    elle-même qu'une règle nommant `sub` lui ouvre quand elle n'en est pas membre
    (`_ORG_A_UN_NON_MEMBRE`)."""
    filtre = ("((i.owner_type = 'member' AND starts_with(i.owner_id, %(prefixe)s) "
              f"AND i.owner_id <> %(mien)s) OR {_ORG_A_UN_NON_MEMBRE}) "
              "AND i.account = %(account)s")
    with _connect() as conn:
        return conn.execute(_COMPTES_PRETES.format(filtre=filtre), {
            "connector": connector, "prefixe": f"{org_id}:", "mien": f"{org_id}:{sub}",
            "account": account, "sub": sub, "org": int(org_id)}).fetchall()


# Accounts of OTHER orgs than `%(org)s` that `%(sub)s` may name: her own key there (#172)
# and those a rule NAMING her opens (D1) — a person's only while its owner is a member.
_HORS_ORG = (
    "(i.id IN (SELECT r.instance_id FROM connector_account_rules r "
    "          WHERE r.subject_user = %(sub)s AND r.level <> 'none' "
    "            AND r.org_id <> %(org)s) "
    " OR (i.owner_type = 'member' AND NOT starts_with(i.owner_id, %(prefixe)s) "
    "     AND substr(i.owner_id, strpos(i.owner_id, ':') + 1) = %(sub)s)) "
    "AND (i.owner_type <> 'member' OR EXISTS (SELECT 1 FROM org_members m "
    "     WHERE i.owner_id ~ '^[0-9]+:' "
    "       AND m.org_id = split_part(i.owner_id, ':', 1)::bigint "
    "       AND m.sub = substr(i.owner_id, strpos(i.owner_id, ':') + 1)))")

# Accounts of the org `%(org)s` — another person's, or a team's — that a rule of that org
# OPENS to `%(sub)s`, whatever the rule's origin (#1499). Their level is judged by the
# resolver (a rule on a team counts only for her active team, `access/niveaux`).
_PRETS_DE_L_ORG = (
    f"(i.id IN (SELECT r.instance_id FROM connector_account_rules r "
    f"           WHERE r.org_id = %(org)s AND {_OUVRE_A_SUB}) "
    " AND ((i.owner_type = 'member' AND starts_with(i.owner_id, %(prefixe)s) "
    "       AND i.owner_id <> %(mien)s) "
    "      OR (i.owner_type = 'group' AND g.org_id = %(org)s))) "
    "OR {org_a_un_non_membre}")

# An account of the org `%(org)s` ITSELF that a rule NAMING `%(sub)s` opens to her while
# she is not a member of that org (a person outside it, D1; a beneficiary of a shared
# project, taken over from #480): the org's step is closed to her
# (`scope.org_d_appartenance`), so it serves at step 5 or named. A member is served at the
# org's step, never here.
_ORG_A_UN_NON_MEMBRE = (
    "(i.owner_type = 'org' AND i.owner_id = %(org)s::text "
    " AND i.id IN (SELECT r.instance_id FROM connector_account_rules r "
    "               WHERE r.subject_user = %(sub)s AND r.level <> 'none') "
    " AND NOT EXISTS (SELECT 1 FROM org_members m "
    "                  WHERE m.org_id = %(org)s AND m.sub = %(sub)s))")
_PRETS_DE_L_ORG = _PRETS_DE_L_ORG.replace("{org_a_un_non_membre}", _ORG_A_UN_NON_MEMBRE)


@lecture_d_appel
def comptes_hors_org(connector: Optional[str], sub: str, org_id: int,
                     account: Optional[str] = None) -> list[dict]:
    """Les comptes de `connector` d'AUTRES orgs que `org_id` que `sub` peut NOMMER : les
    siens (sa clé personnelle posée dans une autre org, #172) et ceux qu'une règle qui le
    NOMME lui ouvre (D1, #1499). Un compte personnel n'y figure que si son propriétaire
    est encore membre de son org. `connector` None : tous les connecteurs (la liste des
    comptes). `account` : seulement ceux de ce nom. Ils servent nommés, et sans nom à
    l'étape 5 (`comptes_de_l_etape5`). Lue UNE fois par appel (`lecture_d_appel`)."""
    filtre = _HORS_ORG + (" AND i.account = %(account)s" if account is not None else "")
    with _connect() as conn:
        return conn.execute(_COMPTES_PRETES.format(filtre=filtre), {
            "connector": connector, "sub": sub, "org": int(org_id),
            "prefixe": f"{org_id}:", "account": account}).fetchall()


@lecture_d_appel
def comptes_de_l_etape5(connector: Optional[str], sub: str, org_id: int) -> list[dict]:
    """Tout ce que l'étape 5 peut servir sans nom (#1499), en UNE requête : les comptes
    de l'org `org_id` qu'une règle de cette org ouvre à `sub` (d'une autre personne ou
    d'une équipe) et ceux d'autres orgs qu'il peut nommer (`comptes_hors_org`), chacun
    avec `prete_le`. `connector` None : tous (la sonde préchargée des listes).

    Lue UNE fois par appel (`lecture_d_appel`) : la sonde préchargée de l'étape 5
    (`cascade.preloaded_presence_probe`) est rebâtie à chaque `status_for` d'un même
    appel — la liste `include=card` en fait plusieurs, et sans cela relisait la base
    à chacun (#1245)."""
    filtre = f"(({_PRETS_DE_L_ORG}) OR ({_HORS_ORG}))"
    with _connect() as conn:
        return conn.execute(_COMPTES_PRETES.format(filtre=filtre), {
            "connector": connector, "sub": sub, "org": int(org_id),
            "prefixe": f"{org_id}:", "mien": f"{org_id}:{sub}"}).fetchall()


def defauts_des_etapes(org_id: int, group_id: Optional[int]) -> list[dict]:
    """Les défauts multi-compte posés aux étapes de l'org et de l'équipe active : le
    compte, son auteur et sa date (`meta.default_set_by`/`default_set_at`, NULL avant
    #1499) — ce qu'`oto_context` montre (D3)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT entity_type, entity_id, connector, account, "
            "meta->>'default_set_by' AS default_par, meta->>'default_set_at' AS default_le "
            "FROM connector_credentials WHERE meta->>'is_default' = 'true' "
            "AND ((entity_type = 'org' AND entity_id = %s) "
            "  OR (entity_type = 'group' AND entity_id = %s)) "
            "ORDER BY entity_type DESC, connector, account",
            (str(org_id), str(group_id) if group_id is not None else None)).fetchall()


def compte_par_id(instance_id: int) -> Optional[dict]:
    """Un compte vivant par son identifiant, avec ses règles (même forme)."""
    with _connect() as conn:
        rows = conn.execute(
            _COMPTES.replace("i.connector = %(connector)s AND ", "").format(
                filtre="i.id = %(id)s"), {"id": int(instance_id)}).fetchall()
    return rows[0] if rows else None


# ── Les gestes : poser, retirer une règle, lire le journal ───────────────────────

def poser_regle(org_id: int, instance_id: int, *, subject_user: Optional[str] = None,
                subject_group: Optional[int] = None, subject_org: bool = False,
                level: str, services: Optional[list] = None, acteur: str) -> dict:
    """Pose (ou remplace) la règle MANUELLE du sujet sur le compte ; le journal l'écrit
    par déclencheur, l'auteur étant `oto.acteur`. Une règle reprise du même sujet devient
    manuelle : un geste l'emporte toujours sur la reprise."""
    with _connect() as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('oto.acteur', %s, true)", (acteur,))
            return conn.execute(
                "INSERT INTO connector_account_rules (org_id, instance_id, subject_user, "
                "subject_group, subject_org, level, services, origine, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT ON CONSTRAINT connector_account_rules_instance_subject_key "
                "DO UPDATE SET level = EXCLUDED.level, services = EXCLUDED.services, "
                "origine = EXCLUDED.origine, updated_at = NOW() "
                "RETURNING id, instance_id, subject_user, subject_group, subject_org, "
                "level, services, origine, created_by, created_at, updated_at",
                (org_id, instance_id, subject_user, subject_group, subject_org, level,
                 services, ORIGINE_MANUELLE, acteur)).fetchone()


def retirer_regle(instance_id: int, *, subject_user: Optional[str] = None,
                  subject_group: Optional[int] = None, subject_org: bool = False,
                  acteur: str) -> bool:
    """Retire la règle du sujet sur le compte (reprise ou manuelle). False si aucune."""
    with _connect() as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('oto.acteur', %s, true)", (acteur,))
            cur = conn.execute(
                "DELETE FROM connector_account_rules WHERE instance_id = %s "
                "AND subject_user IS NOT DISTINCT FROM %s "
                "AND subject_group IS NOT DISTINCT FROM %s AND subject_org = %s",
                (instance_id, subject_user, subject_group, subject_org))
            return (cur.rowcount or 0) > 0


def regles_du_compte(instance_id: int) -> list[dict]:
    with _connect() as conn:
        return conn.execute(
            "SELECT id, subject_user, subject_group, subject_org, level, services, origine, "
            "created_by, created_at, updated_at FROM connector_account_rules "
            "WHERE instance_id = %s ORDER BY id", (instance_id,)).fetchall()


def journal_du_compte(instance_id: int, limit: int = 100) -> list[dict]:
    """Le journal des partages d'un compte, le plus récent d'abord."""
    with _connect() as conn:
        return conn.execute(
            "SELECT id, action, subject_kind, subject_id, level, previous_level, services, "
            "owner_before, hors_org, actor_sub, at FROM connector_account_share_events "
            "WHERE instance_id = %s "
            "ORDER BY at DESC, id DESC LIMIT %s", (instance_id, limit)).fetchall()


# ── Le défaut multi-compte : son auteur et sa date (#1499, D3) ──────────────────
#
# `meta.is_default` d'une ligne de coffre désigne le compte qui sert sans nom à son
# palier ; D3 (`access/niveaux.defaut_qui_tranche`) ne le croit que posé par qui gère le
# compte. Chaque pose enregistre donc son auteur et sa date ; un défaut d'avant, sans
# auteur, a été posé sous les mêmes gardes (voir `defaut_qui_tranche`).

def marque_du_defaut(par: str) -> dict:
    """Ce qu'une pose du défaut écrit dans `meta`, à côté de `is_default: true`."""
    if not par:
        raise ValueError("a default is always set by someone (`par`)")
    return {"is_default": True, "default_set_by": par,
            "default_set_at": datetime.now(timezone.utc).isoformat()}


def poser_le_defaut(conn, entity_type: str, entity_id: str, connector: str, account: str,
                    *, par: str) -> None:
    """Le défaut UNIQUE du palier : `account` le devient (auteur, date), les autres
    lignes du même palier et connecteur le perdent, avec leur marque."""
    conn.execute(
        "UPDATE connector_credentials SET meta = CASE WHEN account = %s "
        "THEN meta || %s::jsonb "
        "ELSE (meta - 'default_set_by' - 'default_set_at') || '{\"is_default\": false}' END "
        "WHERE entity_type = %s AND entity_id = %s AND connector = %s",
        (account, json.dumps(marque_du_defaut(par)), entity_type, str(entity_id), connector))
