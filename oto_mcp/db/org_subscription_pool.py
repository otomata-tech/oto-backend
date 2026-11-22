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
from .user_subscriptions import pause_en_vigueur

PERSONNEL = "personnel"
POOL = "pool"
MODES = (PERSONNEL, POOL)

# Un prêteur SERVABLE : abonnement avec sandbox, connecté — ou au plafond dont
# l'échéance est passée ou inconnue, exactement comme la clause « la personne qui ne
# peut pas servir attend » de la réservation (sans échéance connue, rien ne freine).
# `ab` = `user_model_subscriptions`. Une seule écriture, lue par la pose ET la file.
def preteur_servable(sonde: bool = False) -> str:
    """`PRETEUR_SERVABLE`, pour un worker qui sonde le forfait (`pause_en_vigueur`)."""
    return ("ab.sandbox_id IS NOT NULL "
            "AND ab.statut IN ('connected', 'paused_limit') "
            f"AND NOT {pause_en_vigueur('ab', sonde)}")


PRETEUR_SERVABLE = preteur_servable()

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


#: Un prêt qui compte pour qu'une FLOTTE sans mode réglé passe par le pool : vivant
#: (`PRET_VIVANT`), sandbox posé, connecté ou au plafond QUELLE QUE SOIT son échéance —
#: les mêmes prêteurs que `taille_du_pool_a_la_pose`. Une flotte n'a pas d'autre voie :
#: sur la voie personnelle, la remise l'ARRÊTE (`_FLOTTE_HORS_POOL`). Un prêteur au
#: plafond la fait donc attendre son échéance, il ne la tue pas.
PRET_POUR_UNE_FLOTTE = ("ab.sandbox_id IS NOT NULL "
                        "AND ab.statut IN ('connected', 'paused_limit')")


def sql_propre_sert(sub: str, famille: str, ouvert_ici: str, sonde: bool = False) -> str:
    """SQL : l'abonnement du demandeur `sub` peut-il servir ce travail — il existe,
    ouvert dans son org (`ouvert_ici`, l'expression de `runner_jobs`), ni à
    reconnecter, ni déconnecté, ni en pause qui freine (`pause_en_vigueur`, avec la
    même `sonde` que la réservation) ? La négation de ce qui fait ATTENDRE un travail
    sur la voie personnelle."""
    return f"""({ouvert_ici} AND EXISTS (
                SELECT 1 FROM user_model_subscriptions ab
                 WHERE ab.sub = {sub} AND ab.famille = {famille}
                   AND ab.statut NOT IN ('needs_login', 'disconnected')
                   AND NOT {pause_en_vigueur('ab', sonde)}))"""


def sql_par_le_pool(org: str, famille: str, flotte: str, propre_sert: str,
                    sonde: bool = False) -> str:
    """SQL : ce travail passe-t-il par le POOL de son org ? `org`, `famille` et
    `flotte` (« le travail est d'une flotte ») sont des expressions SQL ;
    `propre_sert` vient de `sql_propre_sert`.

    - Mode `pool` réglé : toujours (pool exclusif, un réglage d'admin).
    - Mode `personnel` réglé : jamais.
    - Rien réglé — le défaut (revue du 19/10/2026) : le pool COMPLÈTE. Un membre
      dont l'abonnement sert le garde ; le pool sert les autres (pas d'abonnement
      ouvert ici, à reconnecter, ou au plafond) s'il a un prêteur SERVABLE
      maintenant (`preteur_servable`) : un prêt déconnecté, à reconnecter ou au
      plafond ne compte pas, et ne fait attendre personne. Une flotte appartient à
      l'org : elle passe par le pool dès qu'un prêt pourra la servir
      (`PRET_POUR_UNE_FLOTTE`) ; sans aucun, elle reste sur la voie personnelle, où
      la remise la refuse en le disant.

    Prêter ne demande aucun admin ; passer l'org en pool exclusif, si."""
    def _un_pret(clause: str) -> str:
        return f"""EXISTS (SELECT 1 FROM user_model_subscription_loans l {PRET_VIVANT}
                            WHERE l.org_id = {org} AND l.famille = {famille}
                              AND {clause})"""
    return f"""(CASE (SELECT m.mode FROM org_model_subscription_modes m
                       WHERE m.org_id = {org} AND m.famille = {famille})
                 WHEN 'pool' THEN TRUE
                 WHEN 'personnel' THEN FALSE
                 ELSE {org} IS NOT NULL AND CASE
                        WHEN {flotte} THEN {_un_pret(PRET_POUR_UNE_FLOTTE)}
                        ELSE NOT {propre_sert} AND {_un_pret(preteur_servable(sonde))}
                      END
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

    ⚠️ Exige la révision `20261024_1100_pool_sans_mode` (la colonne accepte NULL) : elle
    se joue AVANT ce code."""
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
            f"WHERE l.org_id = %s AND l.famille = %s AND {PRET_POUR_UNE_FLOTTE}",
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


# ── ce qui fait tourner les agents de l'org : l'ÉTAT de chaque forfait ─────────
#
# Ce que l'admin d'org lit pour savoir POURQUOI ses agents attendent (`model_subscriptions.
# _servi`, admin seulement) : un forfait déconnecté et un forfait au plafond ne se
# distinguaient pas — `usable_now: false` dans les deux cas. Chaque état est écrit ICI, sur
# les clauses mêmes de la réservation, jamais recopiées : un écran qui jugerait autrement
# que la file dirait « tout va bien » d'un forfait qu'elle fait attendre.

#: Le forfait sert maintenant (la clause de réservation de sa voie est vraie).
ETAT_OK = "ok"
#: À (re)connecter : déconnecté, session perdue, ou sans bac à sable.
ETAT_DECONNECTE = "signed_out"
#: En pause jusqu'à son échéance (`user_subscriptions.pause_en_vigueur`).
ETAT_AU_PLAFOND = "at_limit"
#: Voie personnelle : l'abonnement n'est pas ouvert dans CETTE org (`used_in`).
ETAT_NON_OUVERT = "not_open"
#: Voie personnelle : le porteur d'agents n'a aucun abonnement de cette famille.
ETAT_AUCUN = "none"
ETATS = (ETAT_OK, ETAT_DECONNECTE, ETAT_AU_PLAFOND, ETAT_NON_OUVERT, ETAT_AUCUN)

#: L'état d'un PRÊTEUR (`ab`, joint par `PRET_VIVANT`). `ok` est EXACTEMENT
#: `PRETEUR_SERVABLE` — le `servable` de la même ligne, donc le `usable_now` servi à côté.
#: Sinon : sans bac à sable ou hors `connected`/`paused_limit` → `signed_out` ; il ne
#: reste alors qu'une pause en vigueur → `at_limit`. Un prêteur n'est jamais `not_open`
#: (prêter EST ouvrir au pool de cette org) ni `none` (le prêt joint son abonnement).
#:
#: ⚠️ La pause est celle de la RÉSERVATION (`pause_en_vigueur`, sans sonde — comme
#: `usable_now`), pas celle du repli (`runner_jobs._pause_pour_l_org`) : une pause posée
#: par le seuil plus serré d'une AUTRE org fait attendre les travaux d'ici aussi, et
#: l'écran le dit `at_limit`. Le repli, lui, ne part pas de cette pause-là.
ETAT_DU_PRETEUR = (f"CASE WHEN {PRETEUR_SERVABLE} THEN '{ETAT_OK}' "
                   "WHEN ab.sandbox_id IS NULL "
                   "  OR ab.statut NOT IN ('connected', 'paused_limit') "
                   f"THEN '{ETAT_DECONNECTE}' ELSE '{ETAT_AU_PLAFOND}' END")


def preteurs(org_id: int, famille: str) -> list[dict]:
    """QUI prête un abonnement au pool de cette org — une ligne par prêteur, et
    `servable` : la même clause que la réservation (`PRETEUR_SERVABLE`). Les prêts
    VIVANTS seulement (abonnement existant, auteur toujours membre), servables ou
    non.

    Le reste n'est SERVI qu'à l'org_admin, le tri se fait à la sortie
    (`model_subscriptions._servi`) : `limite_pct` (le plafond PERSO, NULL sans réglage —
    la capacité choisie, pas l'usage), et `etat` / `last_ok_at` / `limit_reset_at`, ce qui
    dit à qui gouverne le pool pourquoi un prêteur ne sert pas. `etat` est lu dans la
    MÊME ligne que `servable` (`ETAT_DU_PRETEUR`) : `ok` et `servable` ne peuvent pas
    diverger. Ni palier (`plan`), ni usage (`limit_utilisation`), ni fenêtre : jamais
    servis."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT l.sub, ({PRETEUR_SERVABLE}) AS servable, ab.limite_pct, "
            f"({ETAT_DU_PRETEUR}) AS etat, ab.last_ok_at, ab.limit_reset_at "
            f"FROM user_model_subscription_loans l {PRET_VIVANT} "
            f"WHERE l.org_id = %s AND l.famille = %s ORDER BY l.sub",
            (org_id, famille)).fetchall()
    return [dict(r) for r in rows]


def forfaits_de_l_org(org_id: int, famille: str) -> dict:
    """Les forfaits PERSONNELS qui font tourner les agents de l'org, et les travaux qui
    attendent chacun — en UNE lecture, sans requête par agent ni par personne. Le
    complément de `preteurs` pour l'écran de l'admin (`model_subscriptions._servi`).

    - `porteurs` : une ligne par personne dont un travail passerait par SON abonnement
      (voie personnelle) — le propriétaire d'un agent ALLUMÉ de l'org sur un modèle de
      `famille`, plus quiconque a un travail en file sur cette voie (un agent éteint
      depuis, un lancement à la main) : sans lui, des travaux en attente ne seraient
      comptés nulle part. Pour chacune, `etat` :

        `none` (aucun abonnement) > `ok` (`sql_propre_sert`, la clause de la file, mot
        pour mot) > `signed_out` (`needs_login`/`disconnected`) > `at_limit` (pause en
        vigueur) > `not_open` (ce qui reste : pas ouvert ici, `abonnement_ouvert_ici`) ;

      `ok` est donc EXACTEMENT « la file servirait ce forfait maintenant » ; une personne
      déconnectée ET non ouverte ici se lit `signed_out` — se reconnecter d'abord. Plus
      `last_ok_at`, `limit_reset_at` et `en_attente`.
    - `pool_en_attente` : les travaux en file que la réservation enverrait au POOL
      (`runner_jobs._par_le_pool`, la clause de la file, mot pour mot) — comptés UNE fois
      pour le pool, jamais par prêteur : la file prend le premier prêteur libre, aucun
      travail n'attend un prêteur nommé.

    « En file » = `pending` et dû (`due_at <= NOW()`), de la famille, comme la réservation
    les voit ; un travail gelé (`held`, agent en pause) n'attend aucun forfait. Ni `sonde`
    ici : la pause STOCKÉE, comme `usable_now`.

    ⚠️ En mode `pool` réglé, `porteurs` sort quand même (sans travail : tout passe par le
    pool) ; c'est l'appelant qui ne les montre pas — leur forfait ne sert pas ici."""
    # Paresseux : `runner_jobs` importe ce module.
    from .. import runner_models
    from .runner_jobs import _par_le_pool, abonnement_ouvert_ici
    modeles = [m.id for m in runner_models.MODELES if m.family == famille]
    # Les clauses partagées prennent des EXPRESSIONS SQL : l'org et la famille y entrent
    # en paramètres nommés, jamais en valeurs formatées dans le texte — et l'org reste
    # une constante que le planificateur porte jusqu'aux index (`idx_runner_jobs_live`,
    # file d'une org).
    org, fam = "%(org)s::bigint", "%(famille)s::text"
    propre_sert = sql_propre_sert("o.sub", fam, abonnement_ouvert_ici("o.sub", fam, org))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            WITH attente AS (
                SELECT viap.actif AS pool, rj.sub, COUNT(*)::int AS n
                  FROM runner_jobs rj
                 CROSS JOIN LATERAL (SELECT {_par_le_pool()} AS actif) viap
                 WHERE rj.org_id = {org} AND rj.status = 'pending' AND rj.due_at <= NOW()
                   AND rj.payload->>'model_family' = {fam}
                 GROUP BY 1, 2),
            porteurs AS (
                SELECT t.sub FROM runner_triggers t
                 WHERE t.org_id = {org} AND t.enabled AND t.model = ANY(%(modeles)s)
                UNION
                SELECT a.sub FROM attente a WHERE NOT a.pool AND a.sub IS NOT NULL)
            SELECT FALSE AS pool, o.sub,
                   CASE WHEN f.sub IS NULL THEN '{ETAT_AUCUN}'
                        WHEN {propre_sert} THEN '{ETAT_OK}'
                        WHEN f.statut IN ('needs_login', 'disconnected')
                        THEN '{ETAT_DECONNECTE}'
                        WHEN {pause_en_vigueur('f')} THEN '{ETAT_AU_PLAFOND}'
                        ELSE '{ETAT_NON_OUVERT}' END AS etat,
                   f.last_ok_at, f.limit_reset_at,
                   COALESCE((SELECT SUM(a.n) FROM attente a
                              WHERE NOT a.pool AND a.sub = o.sub), 0)::int AS en_attente
              FROM porteurs o
              LEFT JOIN user_model_subscriptions f ON f.sub = o.sub AND f.famille = {fam}
            UNION ALL
            SELECT TRUE, NULL, NULL, NULL, NULL,
                   COALESCE((SELECT SUM(a.n) FROM attente a WHERE a.pool), 0)::int
             ORDER BY 1, 2
            """,
            {"org": org_id, "famille": famille, "modeles": modeles}).fetchall()
    porteurs = [dict(r) for r in rows if not r["pool"]]
    (pool,) = [r["en_attente"] for r in rows if r["pool"]]
    return {"porteurs": porteurs, "pool_en_attente": int(pool)}


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
