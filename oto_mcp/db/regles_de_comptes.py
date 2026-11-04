"""Les RÈGLES d'accès des comptes de connecteur : leur journal, leur garde, et la reprise
des anciens partages (oto-backend#1499, étape 3, lot L1).

Le modèle est celui d'oto-core (`access_rules` sur un compte) : une règle par
(compte, sujet), le sujet étant une personne, une équipe ou l'org, à un niveau
`none` < `read` < `write` < `sensitive` < `manage`. Les tables vivent dans
`db/schema/connectors.ACCOUNT_RULES` ; le modèle et ses écarts dans
`docs/partage-de-compte.md` § Les règles de comptes.

⚠️ **Rien ne lit encore les règles.** La résolution passe toujours par les anciens
partages (`share_side`, prêts Unipile) ; la bascule est le lot L3, qui rappelle
`reprendre_les_prets` juste avant de retirer les anciennes tables.

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

import logging

from psycopg.rows import dict_row

from . import sous_instances

logger = logging.getLogger(__name__)

NIVEAU_REPRIS = "sensitive"
ORIGINE_REPRISE = "reprise"

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


# ── La reprise des anciens partages ──────────────────────────────────────────────

_PRETS_UNIPILE = f"""
SELECT g.owner_sub, g.provider, g.grantee_sub AS cible, ua.org_id, si.id AS instance_id
  FROM connector_account_grants g
  LEFT JOIN unipile_accounts ua ON ua.sub = g.owner_sub AND ua.provider = g.provider
  LEFT JOIN connector_instances si ON {sous_instances.JOINTURE}
 ORDER BY 1, 2, 3, 4
"""

_PRETS_UNIPILE_EQUIPE = f"""
SELECT gg.owner_sub, gg.provider, gg.grantee_group_id AS cible, gr.org_id AS org_cible,
       ua.org_id, si.id AS instance_id
  FROM connector_account_group_grants gg
  JOIN org_groups gr ON gr.id = gg.grantee_group_id
  LEFT JOIN unipile_accounts ua ON ua.sub = gg.owner_sub AND ua.provider = gg.provider
  LEFT JOIN connector_instances si ON {sous_instances.JOINTURE}
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


class _Voulues:
    """Les règles que les anciens partages donnent, et ce qu'ils ne donnent pas."""

    def __init__(self):
        self.regles: dict[tuple, dict] = {}
        self.non_reprises: list[dict] = []

    def ajouter(self, instance_id: int, org_id: int, sujet: tuple, services):
        cle = (instance_id, *sujet)
        deja = self.regles.get(cle)
        if deja is None:
            self.regles[cle] = {"org_id": org_id, "services": services}
            return
        # Deux partages vers le même sujet : le plus large l'emporte (NULL = tout).
        if deja["services"] is None or services is None:
            deja["services"] = None
        else:
            deja["services"] = sorted(set(deja["services"]) | set(services))

    def ecarter(self, source: str, cible: str, motif: str, **quoi):
        self.non_reprises.append({"source": source, "cible": cible, "motif": motif, **quoi})


def _sujet(cible: str) -> tuple:
    """`(subject_user, subject_group, subject_org)` d'une cible `user:`/`group:`/`org:`."""
    genre, _, ident = cible.partition(":")
    if genre == "user" and ident:
        return (ident, None, False)
    if genre == "group" and ident.isdigit():
        return (None, int(ident), False)
    if genre == "org" and ident.isdigit():
        return (None, None, True)
    raise ValueError(cible)


def _org_et_proprietaire(cur, entity_type: str, entity_id: str):
    """L'org d'un compte du coffre et la personne qui le possède (None si collectif)."""
    if entity_type == "member":
        org, _, sub = entity_id.partition(":")
        return (int(org) if org.isdigit() else None), sub
    if entity_type == "org":
        return (int(entity_id) if entity_id.isdigit() else None), None
    if entity_type == "group" and entity_id.isdigit():
        r = cur.execute("SELECT org_id FROM org_groups WHERE id = %s",
                        (int(entity_id),)).fetchone()
        return (r["org_id"] if r else None), None
    return None, (entity_id if entity_type == "user" else None)


def _calculer(cur) -> _Voulues:
    from ..access.prets import PORTEURS_A_SERVICES  # paresseux : `access` importe `db`

    v = _Voulues()
    membres = {(r["org_id"], r["sub"]) for r in cur.execute(
        "SELECT m.org_id, m.sub FROM org_members m JOIN users u ON u.sub = m.sub")}
    groupes = {r["id"]: r["org_id"] for r in cur.execute("SELECT id, org_id FROM org_groups")}

    for r in cur.execute(_PRETS_UNIPILE).fetchall():
        quoi = {"proprietaire": r["owner_sub"], "canal": r["provider"],
                "org_id": r["org_id"], "instance_id": r["instance_id"]}
        if r["instance_id"] is None:
            v.ecarter("unipile_pret", f"user:{r['cible']}", "sans_compte", **quoi)
        elif r["cible"] == r["owner_sub"]:
            v.ecarter("unipile_pret", f"user:{r['cible']}", "proprietaire", **quoi)
        elif (r["org_id"], r["cible"]) not in membres:
            v.ecarter("unipile_pret", f"user:{r['cible']}", "hors_org", **quoi)
        else:
            v.ajouter(r["instance_id"], r["org_id"], (r["cible"], None, False), None)

    for r in cur.execute(_PRETS_UNIPILE_EQUIPE).fetchall():
        quoi = {"proprietaire": r["owner_sub"], "canal": r["provider"],
                "org_id": r["org_id"], "instance_id": r["instance_id"]}
        if r["instance_id"] is None:
            v.ecarter("unipile_pret_equipe", f"group:{r['cible']}", "sans_compte", **quoi)
        elif r["org_cible"] != r["org_id"]:
            v.ecarter("unipile_pret_equipe", f"group:{r['cible']}", "hors_org", **quoi)
        else:
            v.ajouter(r["instance_id"], r["org_id"], (None, r["cible"], False), None)

    for r in cur.execute(_PRETS_DU_COFFRE).fetchall():
        cible = r["cible"]
        org, proprietaire = _org_et_proprietaire(cur, r["entity_type"], r["entity_id"])
        quoi = {"connecteur": r["connector"], "org_id": org, "instance_id": r["instance_id"]}
        try:
            sujet = _sujet(cible)
        except ValueError:
            v.ecarter("pret_du_coffre", cible, "cible_inconnue", **quoi)
            continue
        if r["instance_id"] is None or org is None:
            v.ecarter("pret_du_coffre", cible,
                      "sans_instance" if r["instance_id"] is None else "sans_org", **quoi)
            continue
        user, groupe, _ = sujet
        dans_org = ((user is not None and (org, user) in membres)
                    or (groupe is not None and groupes.get(groupe) == org)
                    or (sujet[2] and cible == f"org:{org}"))
        if user is not None and user == proprietaire:
            v.ecarter("pret_du_coffre", cible, "proprietaire", **quoi)
            continue
        if not dans_org:
            v.ecarter("pret_du_coffre", cible, "hors_org", **quoi)
            continue
        services = r["services"]
        if isinstance(services, list):
            services = sorted({str(s) for s in services})
        elif r["connector"] in PORTEURS_A_SERVICES:
            services = []          # porteur à services sans liste : rien de prêté
        else:
            services = None
        v.ajouter(r["instance_id"], org, sujet, services)

    # (a) Un compte d'org sert aujourd'hui à tout membre : une règle d'org le dit.
    for r in cur.execute(_COMPTES_D_ORG).fetchall():
        v.ajouter(r["instance_id"], r["org_id"], (None, None, True), None)
    return v


def reprendre_les_prets(conn) -> dict:
    """RECALCULE les règles issues des anciens partages, dans la transaction de
    l'appelant. Idempotente : rejouée, elle ne change rien.

    - une sous-instance par compte Unipile (`sous_instances.poser_toutes`) ;
    - prêts Unipile (personne, équipe) et `share_side` du coffre → règles `sensitive`,
      avec la liste de `share_services` pour un compte Google ou Microsoft ; une cible
      `org:<id>` → règle d'org ;
    - chaque compte d'org vivant → règle d'org `sensitive` (écart (a)) ;
    - un prêt dont le bénéficiaire n'est pas de l'org du compte (prêt entre orgs), ou
      que rien ne désigne, n'est PAS repris : il est rendu dans `non_reprises` et
      journalisé (écart (d)).

    Les règles reprises portent `origine = 'reprise'` : une règle reprise que plus aucun
    partage ne donne est retirée, une règle `manuelle` n'est jamais touchée (elle gagne
    sur la règle reprise du même sujet). Rend le bilan."""
    cur = conn.cursor(row_factory=dict_row)
    bilan = {"sous_instances": sous_instances.poser_toutes(cur)}
    voulues = _calculer(cur)
    existantes = {
        (r["instance_id"], r["subject_user"], r["subject_group"], r["subject_org"]): r
        for r in cur.execute(
            "SELECT id, instance_id, subject_user, subject_group, subject_org, level, "
            "services, origine FROM connector_account_rules").fetchall()}
    posees = mises_a_jour = gardees = 0
    for cle, regle in sorted(voulues.regles.items(), key=lambda kv: repr(kv[0])):
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
                if r["origine"] == ORIGINE_REPRISE and cle not in voulues.regles]
    if perimees:
        cur.execute("DELETE FROM connector_account_rules WHERE id = ANY(%s)", (perimees,))
    bilan.update(posees=posees, mises_a_jour=mises_a_jour, retirees=len(perimees),
                 manuelles_gardees=gardees, non_reprises=voulues.non_reprises)
    for e in voulues.non_reprises:
        logger.warning("reprise des prêts : NON repris (%s) %s", e["motif"], e)
    logger.info("reprise des prêts : %s",
                {k: v for k, v in bilan.items() if k != "non_reprises"}
                | {"non_reprises": len(voulues.non_reprises)})
    return bilan
