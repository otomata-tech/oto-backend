"""Le POOL d'org des abonnements : le MODE de l'org, et les PRÊTS de ses membres.

Deux tables (`db/schema/runs.py::MODEL_SUBSCRIPTION_POOL`) :

- `org_model_subscription_modes` — une ligne par (org, famille) qui a réglé son mode
  (ou son seul repli API : `mode` NULL). Sans mode réglé, le pool COMPLÈTE :
  l'abonnement du demandeur sert s'il le peut, le pool sert les autres — cf.
  `sql_par_le_pool` ;
- `user_model_subscription_loans` — une ligne par (personne, famille, org) : ce
  membre prête son abonnement au pool de CETTE org. Opt-in, par org.

La réservation (`runner_jobs.claim_next_job`) lit les deux tables en SQL ; ce module
porte les lectures et écritures des surfaces (réglage d'org, écran de la personne) et
celle de la pose (`taille_du_pool_a_la_pose` — PAS `taille_du_pool`, qui juge ce qui
peut servir à la seconde près ; voir leurs docstrings).

⚠️ Aucune session, aucun secret ici, comme dans `user_subscriptions` : un prêt dit
QUEL sandbox peut servir, jamais comment s'y connecter.
"""
from __future__ import annotations

import os
from typing import Iterable, Optional

from .. import interrupteurs
from ._conn import _connect

PERSONNEL = "personnel"
POOL = "pool"
MODES = (PERSONNEL, POOL)

# Un prêteur SERVABLE : abonnement avec sandbox, connecté — ou au plafond dont
# l'échéance est passée ou inconnue, exactement comme la clause « la personne qui ne
# peut pas servir attend » de la réservation (sans échéance connue, rien ne freine).
# `ab` = `user_model_subscriptions`. Une seule écriture, lue par la pose ET la file.
PRETEUR_SERVABLE = (
    "ab.sandbox_id IS NOT NULL "
    "AND ab.statut IN ('connected', 'paused_limit') "
    "AND (ab.statut = 'connected' OR ab.limit_reset_at IS NULL "
    "OR ab.limit_reset_at <= NOW())")

# Le prêt `l` vaut : son abonnement existe, et son auteur est TOUJOURS membre de
# l'org. Quitter l'org rend le prêt inerte sans qu'aucun chemin de départ n'ait à
# penser à lui.
PRET_VIVANT = (
    "JOIN user_model_subscriptions ab ON ab.sub = l.sub AND ab.famille = l.famille "
    "JOIN org_members om ON om.org_id = l.org_id AND om.sub = l.sub")


def get_mode(org_id: Optional[int], famille: str) -> Optional[dict]:
    """La ligne de réglage de l'org pour cette famille, ou None. Son `mode` peut être
    NULL : une ligne née du seul repli API n'a choisi aucun mode."""
    if not org_id:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT org_id, famille, mode, repli_api, updated_at, updated_by "
            "FROM org_model_subscription_modes WHERE org_id = %s AND famille = %s",
            (org_id, famille)).fetchone()
    return dict(row) if row else None


def mode_regle(org_id: Optional[int], famille: str) -> Optional[str]:
    """Le mode que l'org a CHOISI (`personnel` ou `pool`), ou None : rien choisi."""
    return (get_mode(org_id, famille) or {}).get("mode")


def en_pool(org_id: Optional[int], famille: str) -> bool:
    """L'org a-t-elle CHOISI le pool exclusif ? Un réglage d'admin. Sans choix, le pool
    ne sert que ceux dont l'abonnement ne sert pas (`sql_par_le_pool`) — la question
    se pose alors par travail, jamais pour toute l'org."""
    return mode_regle(org_id, famille) == POOL


#: Un prêt qui COMPTE pour qu'un travail sans mode réglé passe par le pool : vivant
#: (`PRET_VIVANT`), sandbox posé, connecté ou au plafond — le plafond tombe à son
#: échéance. Un prêt à reconnecter ou déconnecté ne compte pas : il ne fait attendre
#: personne. Les mêmes prêteurs que `taille_du_pool_a_la_pose`.
PRET_QUI_COMPTE = ("ab.sandbox_id IS NOT NULL "
                   "AND ab.statut IN ('connected', 'paused_limit')")


def sql_propre_sert(sub: str, famille: str, ouvert_ici: str) -> str:
    """SQL : l'abonnement du demandeur `sub` peut-il servir ce travail — ouvert dans
    son org (`ouvert_ici`, l'expression de `runner_jobs`), ni à reconnecter, ni
    déconnecté, ni au plafond non échu ? Exactement la négation de ce qui fait
    ATTENDRE un travail en mode personnel."""
    return f"""({ouvert_ici} AND EXISTS (
                SELECT 1 FROM user_model_subscriptions ab
                 WHERE ab.sub = {sub} AND ab.famille = {famille}
                   AND ab.statut NOT IN ('needs_login', 'disconnected')
                   AND NOT (ab.statut = 'paused_limit'
                            AND ab.limit_reset_at > NOW())))"""


def sql_par_le_pool(org: str, famille: str, flotte: str, propre_sert: str) -> str:
    """SQL : ce travail passe-t-il par le POOL de son org ? `org`, `famille` et
    `flotte` (« le travail est d'une flotte ») sont des expressions SQL ;
    `propre_sert` vient de `sql_propre_sert`.

    - Mode `pool` réglé : toujours (pool exclusif, un réglage d'admin).
    - Mode `personnel` réglé : jamais.
    - Rien réglé — le défaut (revue du 19/10/2026) : le pool COMPLÈTE. Un membre
      dont l'abonnement sert le garde ; le pool sert les autres (pas d'abonnement
      ouvert ici, ou à reconnecter, ou au plafond), s'il a au moins un prêt qui
      compte (`PRET_QUI_COMPTE`). Une flotte appartient à l'org : elle passe
      toujours par le pool, jamais par le forfait de son créateur.

    Prêter ne demande aucun admin ; passer l'org en pool exclusif, si."""
    return f"""(CASE (SELECT m.mode FROM org_model_subscription_modes m
                       WHERE m.org_id = {org} AND m.famille = {famille})
                 WHEN 'pool' THEN TRUE
                 WHEN 'personnel' THEN FALSE
                 ELSE {org} IS NOT NULL AND ({flotte} OR (NOT {propre_sert} AND EXISTS (
                        SELECT 1 FROM user_model_subscription_loans l {PRET_VIVANT}
                         WHERE l.org_id = {org} AND l.famille = {famille}
                           AND {PRET_QUI_COMPTE})))
               END)"""


ENV_REPLI_PAR_DEFAUT = "OTO_REPLI_API_PAR_DEFAUT"


def repli_api_par_defaut() -> bool:
    """Le repli d'une org qui ne l'a jamais réglé : un choix de l'INSTANCE
    (`ENV_REPLI_PAR_DEFAUT`, décision du 08/10/2026). Fermé si elle ne déclare rien —
    les clés d'org déposées l'ont été pour des agents API, et y déplacer des travaux
    d'abonnement serait une dépense que personne n'a demandée (décision du 28/09) ;
    ouvert sur une instance qui le déclare. Lu à chaque appel, sans redémarrage ; une
    valeur illisible lève (`interrupteurs`)."""
    return interrupteurs.oui_non(ENV_REPLI_PAR_DEFAUT,
                                 os.environ.get("OTO_REPLI_API_PAR_DEFAUT"))


def repli_api_actif(org_id: Optional[int], famille: str) -> bool:
    """Un travail d'abonnement épuisé de cette org rejoue-t-il sur SA clé API plutôt
    que d'attendre la réinitialisation (OTO-130) ? Le réglage de l'org s'il existe,
    sinon le défaut de l'instance (`repli_api_par_defaut`).

    Le repli exige toujours la clé déposée PAR l'org (`runner_jobs._cle_ok_pour_repli`) :
    sans clé, rien ne change. La réservation lit la même règle en SQL, avec le MÊME
    défaut passé en paramètre (`runner_jobs.candidats_repli_abonnement`) ; cette
    lecture sert l'écran."""
    ligne = get_mode(org_id, famille)
    return repli_api_par_defaut() if ligne is None else bool(ligne["repli_api"])


def set_repli_api(org_id: int, famille: str, actif: bool, par: Optional[str]) -> dict:
    """Ouvre ou referme le repli API de cette org. Fait NAÎTRE la ligne SANS mode
    (NULL) si l'org n'en a jamais réglé : rien ne change d'autre que l'interrupteur,
    le pool continue de compléter (`sql_par_le_pool`).

    ⚠️ Exige la révision `mode_pool_non_choisi` (la colonne accepte NULL) : elle se
    joue AVANT ce code."""
    sql = ("INSERT INTO org_model_subscription_modes "
           "  (org_id, famille, mode, repli_api, updated_by) "
           "VALUES (%s, %s, %s, %s, %s) "
           "ON CONFLICT (org_id, famille) DO UPDATE "
           "   SET repli_api = EXCLUDED.repli_api, updated_at = NOW(), "
           "       updated_by = EXCLUDED.updated_by "
           "RETURNING org_id, famille, mode, repli_api, updated_at, updated_by")
    with _connect() as conn:
        row = conn.execute(sql, (org_id, famille, None, bool(actif), par)).fetchone()
    return dict(row)


def poser_mode(org_id: int, famille: str, mode: str, par: str) -> dict:
    if mode not in MODES:
        raise ValueError(f"mode hors contrat : {mode!r}")
    with _connect() as conn:
        row = conn.execute(
            # Une ligne qui NAÎT ici n'a rien choisi du repli : elle naît au défaut de
            # l'instance, comme l'absence de ligne. Un réglage existant n'est pas touché.
            """INSERT INTO org_model_subscription_modes
                           (org_id, famille, mode, repli_api, updated_by)
                    VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (org_id, famille) DO UPDATE
                       SET mode = EXCLUDED.mode, updated_by = EXCLUDED.updated_by,
                           updated_at = NOW()
            RETURNING org_id, famille, mode, updated_at, updated_by""",
            (org_id, famille, mode, repli_api_par_defaut(), par)).fetchone()
    return dict(row)


def taille_du_pool(org_id: int, famille: str) -> int:
    """Combien de membres de l'org lui prêtent un abonnement SERVABLE (connecté, ou
    au plafond) — ce que la pose exige non nul, et ce que le réglage d'org montre."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM user_model_subscription_loans l {PRET_VIVANT} "
            f"WHERE l.org_id = %s AND l.famille = %s AND {PRETEUR_SERVABLE}",
            (org_id, famille)).fetchone()
    return int(row["n"]) if row else 0


def taille_du_pool_a_la_pose(org_id: int, famille: str) -> int:
    """Combien de membres prêtent un abonnement qui POURRA servir — connecté, ou au
    plafond QUEL QUE SOIT son échéance. `PRETEUR_SERVABLE` sans sa clause d'échéance.

    ⚠️ La pose et la RÉSERVATION ne posent pas la même question, et les confondre a
    bloqué une org réelle (27/09/2026). La réservation demande « qui peut servir CE
    travail, maintenant ? » — un prêteur au plafond non échu est sauté, le travail
    attend. La pose demande « cet agent pourra-t-il tourner un jour ? » — et un
    plafond est TEMPORAIRE : il tombe à son échéance, l'agent partira. Refuser la
    pose parce que l'unique prêteur est au plafond jusqu'à demain matin, c'est
    refuser pour une raison qui aura disparu avant la prochaine occurrence.

    C'est déjà la règle en mode PERSONNEL, où `servable()` rend `True` pour un
    `paused_limit` quelle que soit son échéance, délibérément. Le pool s'aligne :
    seule l'ABSENCE durable (personne ne prête, ou tous déconnectés / sans sandbox)
    refuse la pose — `subscription_pool_empty` veut dire « ça ne tournera jamais »,
    pas « pas tout de suite »."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM user_model_subscription_loans l {PRET_VIVANT} "
            f"WHERE l.org_id = %s AND l.famille = %s "
            f"  AND ab.sandbox_id IS NOT NULL "
            f"  AND ab.statut IN ('connected', 'paused_limit')",
            (org_id, famille)).fetchone()
    return int(row["n"]) if row else 0


def taille_totale_du_pool(org_id: int, famille: str) -> int:
    """Combien de membres de l'org lui prêtent un abonnement, SERVABLE ou pas —
    à distinguer de `taille_du_pool` (0 est ambigu : personne ne prête, ou tout
    le monde est momentanément au plafond ?). Sert au DIAGNOSTIC d'une
    péremption (`capabilities._abonnement.raison_de_peremption`, 27/09/2026),
    jamais à une garde de pose — celle-ci lit `taille_du_pool_a_la_pose`."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM user_model_subscription_loans l {PRET_VIVANT} "
            f"WHERE l.org_id = %s AND l.famille = %s",
            (org_id, famille)).fetchone()
    return int(row["n"]) if row else 0


def pool_au_plafond(org_id: int, famille: str, depot: Optional[str]) -> dict:
    """Ce qui retient un pool AU PLAFOND, en UNE lecture bornée à l'org — le diagnostic
    d'une péremption (`capabilities._abonnement.raison_de_peremption`, 21/10/2026),
    jamais une garde :

    - `reprise` : la PLUS PROCHE échéance d'un prêteur au plafond (`limit_reset_at`
      futur), ou None ;
    - `repli_api` : l'interrupteur de repli de l'org, None si elle ne l'a jamais réglé
      (l'appelant lit alors `repli_api_par_defaut`, comme `repli_api_actif`) ;
    - `cle_org` : l'org a-t-elle déposé une clé `depot` — la présence, la même lecture
      que les candidats au repli (`runner_jobs.candidats_repli_abonnement`)."""
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT (SELECT MIN(ab.limit_reset_at)
                      FROM user_model_subscription_loans l {PRET_VIVANT}
                     WHERE l.org_id = %s AND l.famille = %s
                       AND ab.statut = 'paused_limit' AND ab.limit_reset_at > NOW())
                       AS reprise,
                   (SELECT m.repli_api FROM org_model_subscription_modes m
                     WHERE m.org_id = %s AND m.famille = %s) AS repli_api,
                   EXISTS (SELECT 1 FROM connector_credentials c
                            WHERE c.entity_type = 'org' AND c.entity_id = %s::text
                              AND c.connector = %s) AS cle_org
            """,
            (org_id, famille, org_id, famille, org_id, depot or "")).fetchone()
    return dict(row)


def preteurs(org_id: int, famille: str) -> list[dict]:
    """QUI prête un abonnement au pool de cette org — une ligne par prêteur, et
    `servable` : la même clause que la réservation (`PRETEUR_SERVABLE`). Les prêts
    VIVANTS seulement (abonnement existant, auteur toujours membre), servables ou
    non. Rien d'autre n'est LU : ni palier, ni état, ni échéance, ni plafond perso —
    un abonnement appartient à qui le paie, prêter n'en partage que la capacité."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT l.sub, ({PRETEUR_SERVABLE}) AS servable "
            f"FROM user_model_subscription_loans l {PRET_VIVANT} "
            f"WHERE l.org_id = %s AND l.famille = %s ORDER BY l.sub",
            (org_id, famille)).fetchall()
    return [dict(r) for r in rows]


def orgs_pretees(sub: str, famille: str) -> list[int]:
    """Les orgs au pool desquelles cette personne prête cet abonnement."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT org_id FROM user_model_subscription_loans "
            "WHERE sub = %s AND famille = %s ORDER BY org_id",
            (sub, famille)).fetchall()
    return [r["org_id"] for r in rows]


def poser_prets(sub: str, famille: str, org_ids: Iterable[int]) -> list[int]:
    """Remplace l'ensemble des orgs auxquelles l'abonnement est prêté. Un prêt
    conservé garde son `servi_at` (sa place dans le tourniquet) ; un prêt retiré
    part tout de suite — le travail SUIVANT ne le voit plus."""
    voulus = sorted({int(o) for o in org_ids})
    with _connect() as conn:
        conn.execute(
            "DELETE FROM user_model_subscription_loans "
            "WHERE sub = %s AND famille = %s AND NOT (org_id = ANY(%s))",
            (sub, famille, voulus))
        for org_id in voulus:
            conn.execute(
                "INSERT INTO user_model_subscription_loans (sub, famille, org_id) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (sub, famille, org_id))
    return voulus


def oublier_prets(sub: str, famille: str, conn=None) -> None:
    """Retire tous les prêts de cet abonnement (le sandbox est effacé)."""
    sql = "DELETE FROM user_model_subscription_loans WHERE sub = %s AND famille = %s"
    if conn is not None:
        conn.execute(sql, (sub, famille))
        return
    with _connect() as c:
        c.execute(sql, (sub, famille))
