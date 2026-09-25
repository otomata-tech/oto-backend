"""Les LIVRAISONS d'un déclencheur par webhook — ce qu'un tiers a envoyé, et ce
qu'on en a fait.

Une ligne par appel, accepté ou non. Trois lecteurs, un seul écrivain (la route
`/api/hooks`) :

1. le **lissage** — les CRÉNEAUX déjà réservés (`due_at`), pour savoir si
   celle-ci part tout de suite ou derrière la file ;
2. l'**écran** — qui a appelé, quand, et quel déroulé en est sorti. Sans lui, une
   source mal configurée est un mystère plutôt qu'un diagnostic : c'est la leçon
   de `expired_count` sur un déclencheur programmé, où quarante-et-une occurrences
   perdues n'ont été découvertes qu'en préparant autre chose ;
3. le **refus visible** — l'appelant reçoit un 404 sans oracle, le propriétaire
   lit ici « secret périmé » sur son propre écran.

⚠️ **Le corps reçu n'est pas stocké ici.** Il voyage dans la charge du travail
(`runner_jobs.payload`), déjà domicile de ce qu'un travail emporte. Le garder deux
fois doublerait le volume ET la surface de fuite d'une donnée tierce.

⚠️ **Déduplication : seulement quand la source SIGNE** (Standard Webhooks). Sa
livraison porte un identifiant signé (`webhook-id`), gardé en `external_id`, et une
retentative du même identifiant déjà ACCEPTÉ ne refait pas de déroulé
(`livraison_acceptee`). Une source au porteur n'envoie pas d'identifiant : pour
elle, ce qui tient lieu de déduplication reste l'ACQUITTEMENT RAPIDE (décidé le
12/09/2026) — une coupure réseau après notre écriture y fait un second déroulé.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from ._conn import _connect

#: Ce qu'une livraison est devenue. `queued`/`delayed` ont produit un travail ; les
#: `refused_*` n'en produisent aucun et gardent leur motif — c'est ce motif qui rend
#: une source mal branchée réparable.
#: ⚠️ Un mauvais secret ne s'écrit PAS, et c'est une conséquence, pas un oubli :
#: journaliser exigerait de retrouver le déclencheur SANS son secret, donc de
#: faire de cette route un oracle sur les identifiants qui existent. Le refus est
#: muet des deux côtés — l'appelant voit un 404, le propriétaire ne voit rien.
QUEUED, DELAYED = "queued", "delayed"
REFUSE_PAUSED, REFUSE_TOO_LARGE, REFUSE_RATE = (
    "refused_paused", "refused_too_large", "refused_rate")
#: Une signature VALIDE dont l'horodatage sort de la fenêtre : un rejeu, ou une
#: horloge dérivée chez la source. Journalisé parce que la source a prouvé qui
#: elle est — contrairement à une signature fausse, qui ne s'écrit pas.
REFUSE_STALE = "refused_stale"
#: Le PLAFOND journalier déclaré sur l'agent (`max_per_day`) est atteint : la
#: livraison est REFUSÉE (429), pas retardée. C'est la borne de dépense d'un
#: credential fuité, là où le lissage ne fait que repousser.
REFUSE_DAILY_CAP = "refused_daily_cap"

#: Ce qu'on rend du corps reçu quand on RELIT une livraison — borné parce qu'une
#: page de livraisons en sert jusqu'à 200, et qu'un corps entier × 200 n'est plus
#: un écran mais un transfert. Le corps complet reste sur le travail
#: (`runner_jobs.payload`), qui est son domicile ; ceci en est la VUE.
#: ⚠️ Le corps ne se recopie toujours pas sur la livraison — cf. l'en-tête. On le
#: LIT en joignant le travail, ce qui garde un seul exemplaire et une seule vérité.
_CORPS_MAX = 4000


def verrouiller_le_declencheur(conn, trigger_id: int) -> None:
    """Prend le verrou de CE déclencheur pour le reste de la transaction.

    ⚠️ **Sans lui, le lissage ne lisse rien au moment exact où il sert.** Une
    rafale est CONCURRENTE par définition : en READ COMMITTED, un `SELECT COUNT`
    ne verrouille rien, donc deux cents livraisons simultanées lisent toutes le
    même compte (zéro), se croient toutes sous le débit, et partent toutes en même
    temps. Le compteur aurait l'air juste après coup, et la rafale serait passée
    entière — la panne que ce mécanisme existe pour empêcher.

    Le verrou est pris sur la LIGNE du déclencheur, donc deux sources différentes
    ne se gênent jamais ; seules les livraisons d'un même agent se sérialisent, ce
    qui est précisément la file qu'on veut ordonner. La transaction qui suit est
    courte (un compte, un INSERT, un INSERT).
    """
    conn.execute("SELECT id FROM runner_triggers WHERE id = %s FOR UPDATE",
                 (trigger_id,)).fetchone()


def retard_de_lissage(conn, trigger_id: int, debit: int, fenetre_s: int = 3600) -> int:
    """Dans combien de secondes le travail de CETTE livraison peut partir. `0` =
    maintenant.

    La règle, en deux lignes :
    - **au plus `debit` départs dans toute heure glissante**, jugé sur les créneaux
      RÉSERVÉS (`due_at`), pas sur les réceptions ;
    - **au-delà, en file** : chaque travail part au moins `fenetre_s / debit` après
      le précédent, et jamais AVANT lui.

    ⚠️ **Pourquoi les créneaux, et plus les réceptions** (13/09/2026). Le premier
    lot comptait les livraisons REÇUES dans l'heure. Ça tenait tant qu'un retard
    ne pouvait pas dépasser une heure — la fraîcheur par défaut refusait au-delà.
    Le jour où rien ne périme, un retard dure des jours ; une heure après la
    rafale, la fenêtre des réceptions est VIDE, et une livraison neuve partait
    tout de suite, DEVANT un arriéré encore en attente : débit non tenu, ordre
    d'arrivée non tenu. Les créneaux futurs, eux, restent visibles tant qu'ils
    n'ont pas eu lieu.

    ⚠️ Prend la connexion de l'appelant, qui a pris `verrouiller_le_declencheur`
    AVANT : sans le verrou, deux livraisons simultanées liraient les mêmes
    créneaux et réserveraient le même.

    ⚠️ `NOW()` est lu dans la transaction, jamais sur l'horloge du process : c'est
    la même horloge que `enqueue_job` utilise pour poser `due_at`, et deux boxes
    (prod, preprod) partagent la base sans partager d'horloge.

    Borné : l'index `(trigger_id, due_at DESC)` et `LIMIT debit` — la lecture ne
    grandit pas avec l'arriéré.
    """
    debit = max(1, int(debit))
    row = conn.execute(
        """
        SELECT COUNT(*)::int AS n,
               EXTRACT(EPOCH FROM (MAX(due_at) - NOW()))::float8 AS dernier_s,
               EXTRACT(EPOCH FROM (MIN(due_at) - NOW()))::float8 AS kieme_s
          FROM (SELECT due_at FROM runner_hook_deliveries
                 WHERE trigger_id = %s AND due_at IS NOT NULL
                   AND due_at > NOW() - make_interval(secs => %s)
                 ORDER BY due_at DESC
                 LIMIT %s) recents
        """,
        (trigger_id, fenetre_s, debit),
    ).fetchone()
    if not row or not row["n"]:
        return 0
    # Tout est en secondes RELATIVES au `NOW()` de la transaction — `0` est
    # maintenant, un nombre positif un créneau à venir. Pas de datetime en Python :
    # la fabrique de lignes du pool les rend en TEXTE (`_str_dict_row`).
    dernier = row["dernier_s"]
    # Le `debit`-ième créneau le plus récent : le prochain ne peut pas tomber moins
    # d'une heure après lui, sinon cette heure-là en porterait `debit + 1`.
    plancher = row["kieme_s"] + fenetre_s if row["n"] >= debit else None
    file_en_attente = dernier > 0
    if not file_en_attente and (plancher is None or plancher <= 0):
        return 0
    echeance = max(x for x in (0.0, dernier + fenetre_s / debit, plancher)
                   if x is not None)
    # Arrondi VERS LE HAUT : `enqueue_job` compte en secondes entières, et tronquer
    # ferait partir chaque travail une fraction de seconde trop tôt — assez, sur
    # une file longue, pour qu'une heure en porte un de trop.
    return max(1, math.ceil(echeance))


def suspendre_la_file(trigger_id: int, org_id: int) -> int:
    """GÈLE la file d'un agent déclenché — appelé quand on le met en pause.

    ⚠️ Mettre en pause ne PERD rien (tranché le 13/09/2026). Un agent programmé
    périme ce qui attend, et c'est juste pour lui : son occurrence a un
    successeur, et une veille jouée treize jours trop tard rend un résultat FAUX
    (#814). **Un événement n'a pas de successeur** — personne ne renverra le lead
    d'hier. Le perdre parce qu'on a mis l'agent en pause une heure ferait de la
    pause une destruction, alors qu'on s'en sert pour réparer.

    `pending` → `held` : la réservation ne prend que `pending`, donc les travaux
    retenus deviennent invisibles aux workers — **l'ancien code de prod compris**,
    puisque sa requête filtre déjà `status = 'pending'`. La pause arrête donc bien
    l'agent ; elle ne vide pas sa file.

    ⚠️ Les CRÉNEAUX ne sont PAS rendus : les travaux qui les occupent existent
    toujours. C'est `perimer_travaux_du_declencheur` + `liberer_les_creneaux`,
    ensemble, qui vident — et ce geste-là est explicite (`op=clear_queue`).
    """
    from ._conn import _connect as _c
    with _c() as conn:
        cur = conn.execute(
            """
            UPDATE runner_jobs SET status = 'held'
             WHERE org_id = %s AND status = 'pending'
               AND payload->>'trigger_id' = %s
            """,
            (org_id, str(trigger_id)),
        )
        return cur.rowcount or 0


def reprendre_la_file(trigger_id: int, org_id: int) -> int:
    """Rend à la file ce que la pause avait gelé — et la REDÉCALE.

    `held` → `pending`, mais pas à leur ancienne échéance : pendant la pause, tous
    leurs créneaux sont devenus du passé. Les rendre tels quels ferait partir la
    file ENTIÈRE d'un coup à la seconde du rallumage — précisément la rafale que
    le lissage existe pour empêcher, et déclenchée par le geste de quelqu'un qui
    remet en marche.

    Tout est donc décalé du **même** délai (le retard du plus ancien créneau
    retenu) : l'ordre d'arrivée et l'espacement sont conservés à la seconde près,
    et rien ne part avant maintenant.

    ⚠️ Les créneaux des LIVRAISONS suivent le même décalage, à partir du même
    point : ce sont eux que le lissage lit pour placer les livraisons à venir.
    Décaler les travaux sans eux ferait placer la prochaine livraison au milieu de
    la file rendue. Ceux d'AVANT ce point ne bougent pas — ils ont pu donner un
    départ réel, et ils comptent encore dans l'heure glissante.
    """
    from ._conn import _connect as _c
    with _c() as conn:
        row = conn.execute(
            """
            SELECT EXTRACT(EPOCH FROM MIN(due_at))::float8 AS depart,
                   GREATEST(EXTRACT(EPOCH FROM (NOW() - MIN(due_at)))::float8, 0)
                       AS decalage
              FROM runner_jobs
             WHERE org_id = %s AND status = 'held'
               AND payload->>'trigger_id' = %s
            """,
            (org_id, str(trigger_id)),
        ).fetchone()
        if not row or row["depart"] is None:
            return 0
        decalage = float(row["decalage"])
        cur = conn.execute(
            """
            UPDATE runner_jobs
               SET status = 'pending',
                   due_at = due_at + make_interval(secs => %s)
             WHERE org_id = %s AND status = 'held'
               AND payload->>'trigger_id' = %s
            """,
            (decalage, org_id, str(trigger_id)),
        )
        conn.execute(
            """
            UPDATE runner_hook_deliveries
               SET due_at = due_at + make_interval(secs => %s)
             WHERE trigger_id = %s AND due_at >= to_timestamp(%s)
            """,
            (decalage, trigger_id, float(row["depart"])),
        )
        return cur.rowcount or 0


def liberer_les_creneaux(trigger_id: int) -> int:
    """Rend les créneaux FUTURS d'un déclencheur — appelé quand on VIDE sa file.

    Vider périme ce qui attendait (`perimer_travaux_du_declencheur`). Sans ce
    geste, les créneaux de ces travaux morts resteraient réservés : l'agent ferait
    attendre ses livraisons neuves derrière une file qui n'existe plus — des
    heures, pour rien, sur un agent qu'on vient justement de désengorger.

    Seuls les créneaux FUTURS : un créneau passé a pu donner un départ réel, et il
    compte toujours dans l'heure glissante.
    """
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE runner_hook_deliveries SET due_at = NULL "
            "WHERE trigger_id = %s AND due_at > NOW()",
            (trigger_id,),
        )
        return cur.rowcount or 0


def enregistrer(conn, trigger_id: int, org_id: int, outcome: str,
                job_id: Optional[int] = None, source: Optional[str] = None,
                due_at: Any = None, external_id: Optional[str] = None) -> int:
    """Écrit la livraison. Rend son id.

    Prend aussi la connexion de l'appelant : la livraison et le travail qu'elle
    produit sont posés ENSEMBLE ou pas du tout. Sans ça, un incident entre les
    deux laisserait soit un travail qu'aucune livraison n'explique, soit une
    livraison qui prétend avoir enfilé un travail qui n'existe pas.
    """
    row = conn.execute(
        """
        INSERT INTO runner_hook_deliveries (trigger_id, org_id, outcome, job_id,
                                            source, due_at, external_id)
             VALUES (%s, %s, %s, %s, %s, %s::timestamptz, %s)
          RETURNING id
        """,
        (trigger_id, org_id, outcome, job_id, (source or None), due_at,
         (external_id or None)),
    ).fetchone()
    return int(row["id"])


def acceptees_sur_24h(conn, trigger_id: int) -> tuple[int, int]:
    """Combien de livraisons ACCEPTÉES sur les 24 dernières heures, et dans combien
    de secondes la plus ancienne d'entre elles sort de la fenêtre (`0` si aucune).

    Fenêtre GLISSANTE, pas un jour calendaire : un plafond remis à zéro à minuit
    laisserait passer deux plafonds en une heure, de part et d'autre de minuit.
    Seules les acceptées comptent — un refus n'a coûté aucun déroulé.

    ⚠️ Prend la connexion de l'appelant, APRÈS `verrouiller_le_declencheur` : deux
    livraisons simultanées au bord du plafond se sérialisent, et une seule passe.
    Borné par l'index `(trigger_id, received_at DESC)`.
    """
    row = conn.execute(
        """
        SELECT COUNT(*)::int AS n,
               COALESCE(CEIL(EXTRACT(EPOCH FROM (MIN(received_at)
                        + INTERVAL '24 hours' - NOW()))), 0)::int AS sortie_s
          FROM runner_hook_deliveries
         WHERE trigger_id = %s AND outcome IN ('queued', 'delayed')
           AND received_at > NOW() - INTERVAL '24 hours'
        """,
        (trigger_id,),
    ).fetchone()
    return int(row["n"] or 0), max(0, int(row["sortie_s"] or 0))


def livraison_acceptee(conn, trigger_id: int, external_id: str) -> Optional[dict]:
    """La livraison déjà ACCEPTÉE (`queued`/`delayed`) de cet identifiant, ou None.

    ⚠️ Seules les livraisons acceptées comptent : une livraison REFUSÉE (en pause,
    périmée, hors débit) n'a produit aucun travail, et sa retentative doit pouvoir
    passer. L'index unique partiel sur ces deux états tient la même règle en base.

    ⚠️ Prend la connexion de l'appelant, APRÈS `verrouiller_le_declencheur` : deux
    retentatives simultanées se sérialisent sur le verrou, et la seconde lit la
    première.
    """
    return conn.execute(
        """
        SELECT id, job_id FROM runner_hook_deliveries
         WHERE trigger_id = %s AND external_id = %s
           AND outcome IN ('queued', 'delayed')
         LIMIT 1
        """,
        (trigger_id, external_id),
    ).fetchone()


def livraisons(trigger_id: int, org_id: int, limit: int = 50,
               en_attente: bool = False, avec_corps: bool = False) -> list[dict]:
    """Ce que ce déclencheur a reçu — ou, `en_attente`, ce qui ATTEND de tourner.

    Org-scopé : un déclencheur d'une autre org rend une liste vide, jamais les
    livraisons d'autrui.

    ⚠️ **`outcome` dit ce que la LIVRAISON est devenue, figé à la réception** —
    `queued` veut dire « acceptée, son travail a été enfilé », pas « encore en
    attente ». Rien ne réécrit cette ligne quand le travail tourne, et c'est voulu :
    elle reste le journal de ce qui est arrivé à la porte. Ce que le travail est
    devenu ENSUITE se lit sur le travail lui-même (`job_status`, `job_due_at`),
    joint ici à la lecture, jamais recopié. `job_status` est `NULL` pour un refus
    (aucun travail) ou un travail disparu.

    **`en_attente`** ne garde que les livraisons dont le travail est `pending` ou
    `held` — la FILE, exactement ce que `clear_queue` périme ; ⚠️ `pending` compte aussi
    un travail ÉCHOUÉ remis en file pour une nouvelle tentative (`complete_job`), donc
    « en attente » ne veut pas dire « n'a jamais tourné » — de la plus ancienne à
    la plus récente, dans l'ordre où elles partiront. Ce qui a déjà tourné se lit
    dans les déroulés, pas ici : un journal qui répète la liste des runs sous un
    bouton « vider la file » se lit comme la file (vécu le 16/09/2026 — deux
    livraisons aux déroulés terminés depuis des heures, affichées « queued »).

    ⚠️ **`job_input` est le CORPS REÇU tel que l'agent l'a lu** — l'instruction
    augmentée que porte le travail (`runner_jobs.payload->>'input'`), bornée ici,
    et servie SUR DEMANDE (`avec_corps`) : sans elle la clé est absente, pas nulle.
    Une page fait jusqu'à 200 lignes ; 200 corps de 4 Ko servis à un agent qui
    demande « a-t-il tourné » sont des dizaines de milliers de jetons pour une
    question oui/non — le coût d'un relevé ne doit jamais dépasser ce qu'il relève.
    Le corps n'est pas stocké sur la livraison (cf. le schéma : il voyage dans la
    charge du travail, et le garder deux fois doublerait volume et surface de
    fuite) : il se LIT en joignant, jamais en recopiant. Sans lui, rejouer une
    livraison morte demandait de deviner ce qu'elle portait — et une livraison
    qu'on ne peut pas rejouer à l'identique ne se diagnostique pas (trois
    livraisons perdues sur un même agent événementiel, 18-22/09/2026). `NULL` pour
    un refus (aucun travail)
    ou un travail disparu.

    ⚠️ **`job_attempt_errors` porte les motifs de TOUTES les tentatives**, pas
    seulement la dernière : trois essais qui échouent différemment racontent autre
    chose que trois essais identiques, et `last_error` ne gardait que le troisième.
    `[]` = aucune tentative échouée, un vrai vide ; `NULL` = pas de travail.
    """
    filtre = "AND j.status IN ('pending', 'held')" if en_attente else ""
    ordre = "ASC" if en_attente else "DESC"
    corps = (f"LEFT(j.payload->>'input', {_CORPS_MAX}) AS job_input," if avec_corps
             else "")
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT d.id, d.trigger_id, d.received_at, d.outcome, d.job_id, d.source,
                   j.status AS job_status, j.due_at AS job_due_at,
                   {corps}
                   j.attempt_errors AS job_attempt_errors
              FROM runner_hook_deliveries d
              LEFT JOIN runner_jobs j ON j.id = d.job_id AND j.org_id = d.org_id
             WHERE d.trigger_id = %s AND d.org_id = %s {filtre}
             ORDER BY d.id {ordre}
             LIMIT %s
            """,
            (trigger_id, org_id, max(1, min(int(limit), 200))),
        ).fetchall()
    return [dict(r) for r in rows]


def comptage_livraisons(trigger_id: int, org_id: int) -> dict:
    """Ce que l'écran dit AU-DESSUS de la liste : combien reçues sur 24 h, combien
    refusées, et la dernière. ⚠️ `0` est un vrai zéro (rien n'est arrivé), jamais
    une absence de mesure — même règle que `expired_count`."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*)::int AS recues_24h,
                   COUNT(*) FILTER (WHERE outcome LIKE 'refused%%')::int AS refusees_24h,
                   MAX(received_at) AS derniere
              FROM runner_hook_deliveries
             WHERE trigger_id = %s AND org_id = %s
               AND received_at > NOW() - INTERVAL '24 hours'
            """,
            (trigger_id, org_id),
        ).fetchone()
    d = dict(row) if row else {}
    return {"recues_24h": d.get("recues_24h") or 0,
            "refusees_24h": d.get("refusees_24h") or 0,
            "derniere": d.get("derniere")}
