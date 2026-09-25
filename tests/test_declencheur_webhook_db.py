"""Le déclencheur par webhook EN BASE — ce qu'une doublure ne peut pas prouver.

Quatre choses vivent dans le SQL et nulle part ailleurs :

1. **Le haché est comparé DANS le WHERE.** Une doublure voit qu'on le passe ; seule
   la base montre qu'un mauvais secret ne rend rien.
2. **Le relâchement des `NOT NULL` est sûr sur la base PARTAGÉE.** Un déclencheur
   webhook (`next_due IS NULL`) doit être INVISIBLE au tick de la prod, qui tourne
   l'ancien code pendant la fenêtre de déploiement. C'est l'affirmation de sûreté
   de la migration — elle mérite un banc, pas une promesse.
3. **La fenêtre de lissage compte ce qu'il faut**, et seulement les livraisons qui
   ont enfilé.
4. **Un travail retardé qui a trop attendu PÉRIME** à la réservation, au lieu de
   partir en retard.

Patron de base éphémère repris de `test_runner_workers_db.py`.
"""
from __future__ import annotations

import os
import uuid

import pytest


@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_hook_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    avant_url, avant_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    avant_key = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["DATABASE_URL"] = dsn
    os.environ["OTO_MCP_MASTER_KEY"] = "4" * 64
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = avant_pool
        for cle, valeur in (("DATABASE_URL", avant_url),
                            ("OTO_MCP_MASTER_KEY", avant_key)):
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


ORG = 8100


def _webhook(db, procedure="veille", org=ORG, **kw):
    t = db.create_trigger(org, "alexis", procedure=procedure, tz="UTC",
                          tools=["a"], kind="webhook", **kw)
    from oto_mcp import runner_hook
    secret, hache = runner_hook.nouveau_secret()
    db.poser_secret_de_hook(t["id"], org, hache)
    return t, secret


# ── 1. le secret, comparé en SQL ──────────────────────────────────────────────

def test_le_bon_secret_trouve_le_declencheur(live):
    from oto_mcp import db, runner_hook
    t, secret = _webhook(db)
    trouve = db.trigger_par_secret(t["id"], runner_hook.hacher(secret))
    assert trouve and trouve["id"] == t["id"] and trouve["kind"] == "webhook"


def test_un_MAUVAIS_secret_ne_trouve_rien(live):
    """⚠️ LE banc de sécurité. La comparaison est dans le WHERE : un prédicat qui
    accepterait n'importe quoi ouvrirait chaque déclencheur à chaque secret."""
    from oto_mcp import db, runner_hook
    t, _ = _webhook(db, procedure="veille-2")
    assert db.trigger_par_secret(t["id"], runner_hook.hacher("otoh_faux")) is None


def test_le_bon_secret_sur_le_MAUVAIS_id_ne_trouve_rien(live):
    from oto_mcp import db, runner_hook
    t, secret = _webhook(db, procedure="veille-3")
    assert db.trigger_par_secret(t["id"] + 9999, runner_hook.hacher(secret)) is None


def test_un_agent_PROGRAMME_n_est_jamais_trouve_par_secret(live):
    """`kind = 'webhook'` est dans le WHERE : un agent programmé n'a pas de porte."""
    from oto_mcp import db, runner_hook
    import datetime
    t = db.create_trigger(ORG, "alexis", procedure="programme", cron="5 6 * * *",
                          tz="UTC", tools=["a"],
                          next_due=datetime.datetime.now(datetime.timezone.utc))
    db.poser_secret_de_hook(t["id"], ORG, runner_hook.hacher("otoh_x"))
    assert db.trigger_par_secret(t["id"], runner_hook.hacher("otoh_x")) is None


def test_un_declencheur_en_PAUSE_est_trouve_quand_meme(live):
    """Il doit l'être pour que la route réponde 409 (« il existe, il est en
    pause ») plutôt que 404 : c'est une information que son propriétaire a le
    droit de recevoir — c'est lui qui a donné le secret à la source."""
    from oto_mcp import db, runner_hook
    t, secret = _webhook(db, procedure="veille-pause")
    db.update_trigger(t["id"], ORG, {"enabled": False})
    trouve = db.trigger_par_secret(t["id"], runner_hook.hacher(secret))
    assert trouve and trouve["enabled"] is False


# ── 2. la migration est sûre sur la base partagée ─────────────────────────────

def test_un_webhook_est_INVISIBLE_au_tick(live):
    """⚠️ L'affirmation de sûreté de la migration, éprouvée plutôt que promise.

    `cron` et `next_due` deviennent NULLABLES. Pendant la fenêtre de déploiement,
    la PROD tourne l'ancien code et son tick lit
    `WHERE enabled AND next_due <= NOW()` — qu'un NULL ne satisfait jamais. Une
    ligne webhook lui est donc invisible, jamais mal traitée. Si ce banc rougit,
    la migration n'est pas déployable."""
    from oto_mcp import db
    t, _ = _webhook(db, procedure="veille-invisible")
    dus = [d["id"] for d in db.due_triggers(limit=500)]
    assert t["id"] not in dus, (
        "un déclencheur webhook ne doit JAMAIS être sélectionné par le tick")


def test_un_webhook_NAIT_sans_echeance_ni_cron(live):
    """⚠️ La propriété qui protège la fenêtre de déploiement, tenue DIRECTEMENT.

    `due_triggers` filtre désormais par genre, donc le banc ci-dessus passerait
    même si une ligne webhook portait une échéance — et l'ANCIEN code de prod, qui
    ne connaît pas le genre, la verrait. Ce qui le protège est `next_due IS NULL`,
    et c'est ce qu'on lit ici, dans la colonne."""
    from oto_mcp import db
    t, _ = _webhook(db, procedure="veille-nue")
    with db._connect() as conn:
        r = conn.execute("SELECT cron, next_due FROM runner_triggers WHERE id = %s",
                         (t["id"],)).fetchone()
    assert r["cron"] is None and r["next_due"] is None


def test_un_agent_programme_reste_VU_par_le_tick(live):
    """Le bord opposé : sans lui, le banc ci-dessus passerait si le tick ne voyait
    plus rien du tout."""
    from oto_mcp import db
    import datetime
    hier = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    t = db.create_trigger(ORG, "alexis", procedure="programme-du", cron="5 6 * * *",
                          tz="UTC", tools=["a"], next_due=hier)
    assert t["id"] in [d["id"] for d in db.due_triggers(limit=500)]


def test_un_webhook_qui_RECEVRAIT_une_echeance_reste_invisible_au_tick(live):
    """La garde est le GENRE, pas l'échéance NULL. Si une échéance atterrit sur
    un webhook par un chemin oublié, il ne doit pas partir à l'horloge."""
    from oto_mcp import db
    t, _ = _webhook(db, procedure="veille-echeance-fantome")
    with db._connect() as conn:
        conn.execute("UPDATE runner_triggers SET next_due = NOW() - INTERVAL '1 hour' "
                     "WHERE id = %s", (t["id"],))
    assert t["id"] not in [d["id"] for d in db.due_triggers(limit=500)]


def test_update_trigger_ECRIT_payload_fields_en_jsonb(live):
    """Un dict nu ne s'adapte pas : psycopg refusait, et toute retouche qui le
    portait échouait. Le chemin capacité ne l'appelait pas — jusqu'à ce lot."""
    from oto_mcp import db
    t, _ = _webhook(db, procedure="veille-fields-jsonb")
    lu = db.update_trigger(t["id"], ORG, {"payload_mode": "fields",
                                          "payload_fields": {"lead": "$.id"}})
    assert lu["payload_fields"] == {"lead": "$.id"}
    lu = db.update_trigger(t["id"], ORG, {"payload_mode": "ignore",
                                          "payload_fields": {}})
    assert lu["payload_fields"] is None, "vide se range en NULL, pas en `{}`"


# ── 3. le lissage, sur les CRÉNEAUX réservés ─────────────────────────────────

def _livrer(t, secret, n=1):
    """`n` livraisons réelles par `declencher` ; rend leurs retards annoncés."""
    from oto_mcp import runner_hook
    return [runner_hook.declencher(t["id"], secret, None)["delayed_seconds"] or 0
            for _ in range(n)]


def _creneaux(trigger_id):
    """Les créneaux réservés, dans l'ORDRE D'ARRIVÉE (id de livraison), en
    secondes epoch — le pool rend les datetimes en texte."""
    from oto_mcp import db
    with db._connect() as conn:
        rows = conn.execute("SELECT EXTRACT(EPOCH FROM due_at)::float8 AS e "
                            "FROM runner_hook_deliveries "
                            "WHERE trigger_id = %s AND due_at IS NOT NULL "
                            "ORDER BY id", (trigger_id,)).fetchall()
    return [r["e"] for r in rows]


def _reculer(trigger_id, heures):
    """Fait comme si tout était arrivé `heures` plus tôt — réceptions ET créneaux."""
    from oto_mcp import db
    with db._connect() as conn:
        conn.execute("UPDATE runner_hook_deliveries SET "
                     "received_at = received_at - make_interval(hours => %s), "
                     "due_at = due_at - make_interval(hours => %s) "
                     "WHERE trigger_id = %s", (heures, heures, trigger_id))


def test_sous_le_debit_tout_part_tout_de_suite(live):
    from oto_mcp import db
    t, secret = _webhook(db, procedure="lissage-sous", max_per_hour=5)
    assert _livrer(t, secret, 5) == [0, 0, 0, 0, 0]


def test_une_heure_glissante_ne_porte_JAMAIS_plus_que_le_debit(live):
    """La promesse de `max_per_hour`, lue sur les créneaux réels : pour tout i, le
    créneau i+débit tombe au moins une heure après le créneau i."""
    from oto_mcp import db
    t, secret = _webhook(db, procedure="lissage-glissant", max_per_hour=3)
    retards = _livrer(t, secret, 12)
    assert retards[:3] == [0, 0, 0] and all(r > 0 for r in retards[3:])
    c = _creneaux(t["id"])
    assert c == sorted(c), "l'ordre d'arrivée est l'ordre de départ"
    for i in range(len(c) - 3):
        # EXACT, à la précision du flottant près : l'arrondi vers le haut rend le
        # plancher d'une heure strict. Une tolérance d'une seconde laissait passer
        # un arrondi vers le bas (épreuve de chute du 13/09).
        assert c[i + 3] - c[i] >= 3600 - 1e-4, (
            f"les créneaux {i}..{i + 3} tiennent dans moins d'une heure")


def test_une_livraison_TARDIVE_ne_double_pas_la_file(live):
    """⚠️ LE bogue que « rien ne périme » a révélé. L'ancien lissage comptait les
    RÉCEPTIONS de l'heure écoulée : deux heures après une rafale, cette fenêtre
    est vide, et une livraison neuve partait tout de suite — DEVANT un arriéré qui
    attendait encore. Ici l'arriéré court encore deux heures ; la neuve doit
    passer derrière lui."""
    from oto_mcp import db
    t, secret = _webhook(db, procedure="lissage-tardif", max_per_hour=2)
    _livrer(t, secret, 10)                  # créneaux jusqu'à ~+4 h
    _reculer(t["id"], 2)                    # …on est deux heures plus tard
    avant = max(_creneaux(t["id"]))
    assert _livrer(t, secret)[0] > 0, "une file attend : la neuve ne part pas devant"
    assert _creneaux(t["id"])[-1] > avant, "elle prend le créneau APRÈS la file"


def test_RELEVER_le_debit_ne_fait_pas_doubler_la_file(live):
    """Le cas où la file en attente décide seule. Débit 1 : quatre créneaux à une
    heure d'écart. On relève à 10 : dans l'heure glissante il y a de la place, le
    plancher horaire ne dit plus rien — et une livraison neuve partirait tout de
    suite, DEVANT trois heures de file.

    ⚠️ Ce que le banc n'affirme PAS : relever le débit ne REPLANIFIE pas ce qui
    attend déjà. Les créneaux posés restent posés ; seuls les suivants se serrent.
    (Relevé par une épreuve de chute : le banc de la livraison tardive ne
    distinguait pas les deux termes.)"""
    from oto_mcp import db
    t, secret = _webhook(db, procedure="lissage-releve", max_per_hour=1)
    _livrer(t, secret, 4)
    db.update_trigger(t["id"], ORG, {"max_per_hour": 10})
    avant = max(_creneaux(t["id"]))
    assert _livrer(t, secret)[0] > 0
    assert _creneaux(t["id"])[-1] > avant, "derrière la file, jamais devant"


def test_une_fois_la_file_ecoulee_on_repart_tout_de_suite(live):
    """Le bord opposé : le lissage ne doit pas garder une mémoire au-delà de
    l'heure glissante."""
    from oto_mcp import db
    t, secret = _webhook(db, procedure="lissage-ecoule", max_per_hour=2)
    _livrer(t, secret, 6)
    _reculer(t["id"], 48)
    assert _livrer(t, secret) == [0]


def _statuts(trigger_id):
    from oto_mcp import db
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*)::int AS n FROM runner_jobs "
            "WHERE payload->>'trigger_id' = %s GROUP BY status",
            (str(trigger_id),)).fetchall()
    return {r["status"]: r["n"] for r in rows}


def test_METTRE_EN_PAUSE_ne_perd_RIEN_et_arrete_quand_meme_l_agent(live):
    """⚠️ Tranché le 13/09/2026 : la pause GÈLE, elle ne détruit pas. Un événement
    n'a pas de successeur — personne ne renverra le lead d'hier — donc le perdre
    parce qu'on met l'agent en pause ferait de la pause une destruction, alors
    qu'on s'en sert pour réparer.

    Les deux moitiés comptent : rien n'est périmé, ET rien ne tourne (la
    réservation ne prend que `pending`, ce que l'ancien code de prod filtre déjà).
    """
    from oto_mcp import db
    # Une org à elle seule : la réservation n'est pas scopée au déclencheur, et
    # les travaux des autres bancs de ce fichier la satisferaient.
    org = ORG + 71
    t, secret = _webhook(db, procedure="pause-gele", org=org, max_per_hour=1)
    _livrer(t, secret, 4)
    db.update_trigger(t["id"], org, {"enabled": False})
    assert _statuts(t["id"]) == {"held": 4}, "rien de périmé, tout retenu"
    assert db.claim_next_job(org, "w", lease_seconds=60) is None, (
        "un agent en pause ne tourne pas")


def test_RALLUMER_rend_la_file_sans_la_faire_partir_d_un_coup(live):
    """⚠️ Rendre les créneaux tels quels ferait partir la file ENTIÈRE à la
    seconde du rallumage — la rafale même que le lissage empêche, déclenchée par
    le geste de quelqu'un qui remet en marche. Tout est décalé du même délai :
    l'ordre et l'espacement sont conservés, rien ne part avant maintenant."""
    from oto_mcp import db
    t, secret = _webhook(db, procedure="pause-reprise", max_per_hour=1)
    _livrer(t, secret, 4)
    db.update_trigger(t["id"], ORG, {"enabled": False})
    _reculer(t["id"], 10)      # dix heures de pause : tous les créneaux sont passés
    with db._connect() as conn:
        conn.execute("UPDATE runner_jobs SET due_at = due_at - INTERVAL '10 hours' "
                     "WHERE payload->>'trigger_id' = %s", (str(t["id"]),))
    db.update_trigger(t["id"], ORG, {"enabled": True})
    assert _statuts(t["id"]) == {"pending": 4}, "la file est rendue"
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT EXTRACT(EPOCH FROM (due_at - NOW()))::float8 AS d "
            "FROM runner_jobs WHERE payload->>'trigger_id' = %s ORDER BY id",
            (str(t["id"]),)).fetchall()
    ecarts = [r["d"] for r in rows]
    assert ecarts[0] >= -1, "rien ne part avant maintenant"
    # Tolérance NOMMÉE, à la seconde : chaque livraison vit sa propre transaction (son
    # propre `NOW()`), donc l'écart entre deux créneaux consécutifs porte le temps RÉEL
    # écoulé entre deux appels Python successifs — quelques millisecondes ici, jusqu'à
    # une seconde sur une CI chargée. Une égalité stricte au flottant y voyait un
    # espacement « perdu » (ex. CI : [-0.002921, 3599.999433, 7199.00185, 10799.004216])
    # alors que l'heure était bien tenue à la milliseconde ou à la seconde près. On
    # affirme toujours l'espacement d'une heure, juste avec une marge explicite plutôt
    # qu'une précision d'horloge qu'aucune machine ne garantit.
    TOLERANCE_ESPACEMENT_S = 1.0
    assert all(b - a == pytest.approx(3600, abs=TOLERANCE_ESPACEMENT_S)
               for a, b in zip(ecarts, ecarts[1:])), (
        f"l'espacement d'une heure est perdu : {ecarts}")
    # Et le lissage suit : la livraison suivante passe derrière la file rendue.
    assert _livrer(t, secret)[0] > ecarts[-1] - 1


def test_VIDER_la_file_perime_ce_qui_attend_et_rend_les_creneaux(live):
    """Le geste EXPLICITE — le seul qui perde quelque chose."""
    from oto_mcp import db
    t, secret = _webhook(db, procedure="vider", max_per_hour=1)
    _livrer(t, secret, 4)
    n = db.perimer_travaux_du_declencheur(t["id"], ORG, raison="vidée")
    db.liberer_les_creneaux(t["id"])
    assert n == 4 and _statuts(t["id"]) == {"expired": 4}
    # Les trois créneaux FUTURS sont rendus : la livraison suivante attend au pire
    # l'heure glissante du créneau déjà passé, jamais les trois heures de file.
    assert _livrer(t, secret)[0] <= 3600, "les créneaux futurs sont rendus"


def test_VIDER_atteint_aussi_ce_que_la_PAUSE_a_retenu(live):
    """⚠️ En pause est le cas le plus courant : on arrête l'agent qui s'emballe,
    PUIS on jette. Oublier `held` laisserait le seul geste de purge sans effet
    exactement là où on s'en sert."""
    from oto_mcp import db
    t, secret = _webhook(db, procedure="vider-en-pause", max_per_hour=1)
    _livrer(t, secret, 4)
    db.update_trigger(t["id"], ORG, {"enabled": False})
    assert _statuts(t["id"]) == {"held": 4}
    assert db.perimer_travaux_du_declencheur(t["id"], ORG, raison="vidée") == 4
    assert _statuts(t["id"]) == {"expired": 4}


def test_un_agent_PROGRAMME_perime_toujours_a_la_pause(live):
    """L'asymétrie est VOULUE, et ce banc la tient : l'occurrence d'un agent
    programmé a un successeur, et la jouer treize jours trop tard rend un résultat
    FAUX (#814). Un événement n'a pas de successeur. Rien de #814 n'est défait."""
    import datetime
    from oto_mcp import db
    t = db.create_trigger(ORG, "alexis", procedure="programme-pause",
                          cron="5 6 * * *", tz="UTC", tools=["a"],
                          next_due=datetime.datetime.now(datetime.timezone.utc))
    db.enqueue_job(ORG, "start", payload={"procedure": "p", "trigger_id": t["id"]})
    db.update_trigger(t["id"], ORG, {"enabled": False})
    assert _statuts(t["id"]) == {"expired": 1}


def test_le_lissage_est_par_DECLENCHEUR(live):
    """La file d'un agent ne doit pas retarder celle d'un autre."""
    from oto_mcp import db
    a, sa = _webhook(db, procedure="lissage-a", max_per_hour=1)
    b, sb = _webhook(db, procedure="lissage-b", max_per_hour=1)
    _livrer(a, sa, 5)
    assert _livrer(b, sb) == [0]


def test_les_livraisons_se_lisent_et_se_comptent(live):
    from oto_mcp import db
    t, _ = _webhook(db, procedure="veille-journal")
    with db._connect() as conn:
        db.enregistrer(conn, t["id"], ORG, db.QUEUED, job_id=7, source="n8n/1.0")
        db.enregistrer(conn, t["id"], ORG, db.REFUSE_PAUSED)
    lues = db.livraisons(t["id"], ORG)
    assert [l["outcome"] for l in lues] == [db.REFUSE_PAUSED, db.QUEUED]
    assert lues[1]["job_id"] == 7 and lues[1]["source"] == "n8n/1.0"
    compte = db.comptage_livraisons(t["id"], ORG)
    assert compte == {"recues_24h": 2, "refusees_24h": 1,
                      "derniere": compte["derniere"]}
    assert compte["derniere"] is not None


def _job_de(trigger_id):
    from oto_mcp import db
    return db.livraisons(trigger_id, ORG)[0]["job_id"]


def _poser_statut(job_id, statut):
    from oto_mcp import db
    with db._connect() as conn:
        conn.execute("UPDATE runner_jobs SET status = %s WHERE id = %s",
                     (statut, job_id))


def test_la_livraison_reste_FIGEE_mais_l_etat_du_travail_SUIT(live):
    """`outcome` dit ce qui est arrivé à la PORTE et ne bouge plus ; ce que le
    travail est devenu se lit sur le travail, joint à chaque lecture. Sans ça,
    une livraison dont le déroulé est fini depuis des heures reste « queued »
    (16/09/2026)."""
    from oto_mcp import db
    t, secret = _webhook(db, procedure="journal-etat-du-travail")
    _livrer(t, secret)
    job = _job_de(t["id"])
    lue = db.livraisons(t["id"], ORG)[0]
    assert (lue["outcome"], lue["job_status"]) == (db.QUEUED, "pending")

    _poser_statut(job, "done")
    lue = db.livraisons(t["id"], ORG)[0]
    assert lue["outcome"] == db.QUEUED, "la ligne de livraison n'est JAMAIS réécrite"
    assert lue["job_status"] == "done", "l'état du travail est lu, pas recopié"


def test_un_REFUS_n_a_aucun_travail_donc_aucun_etat(live):
    from oto_mcp import db
    t, _ = _webhook(db, procedure="journal-refus-sans-travail")
    with db._connect() as conn:
        db.enregistrer(conn, t["id"], ORG, db.REFUSE_PAUSED)
    lue = db.livraisons(t["id"], ORG)[0]
    assert (lue["job_id"], lue["job_status"]) == (None, None)
    assert db.livraisons(t["id"], ORG, en_attente=True) == [], (
        "un refus n'a pas de travail, donc n'est jamais dans la file")


def test_en_attente_ne_rend_QUE_ce_qui_n_a_pas_tourne_du_plus_ancien(live):
    """La file seule : `pending` et `held`, dans l'ordre où elles partiront.
    Ce qui a tourné (terminé, échoué, en cours, périmé) se lit dans les déroulés."""
    from oto_mcp import db
    t, secret = _webhook(db, procedure="file-seulement-en-attente", max_per_hour=100)
    _livrer(t, secret, 5)
    ids = [l["job_id"] for l in db.livraisons(t["id"], ORG)][::-1]  # ordre d'arrivée
    _poser_statut(ids[0], "done")
    _poser_statut(ids[1], "claimed")
    _poser_statut(ids[2], "held")
    _poser_statut(ids[4], "expired")

    file = db.livraisons(t["id"], ORG, en_attente=True)
    assert [l["job_id"] for l in file] == [ids[2], ids[3]], "held + pending, plus ancien d'abord"
    assert [l["job_status"] for l in file] == ["held", "pending"]
    assert all(l["job_due_at"] for l in file), "l'échéance du travail est servie"
    assert len(db.livraisons(t["id"], ORG)) == 5, "sans le filtre, le journal entier"


def test_la_file_compte_ce_que_VIDER_viderait(live):
    """Même prédicat que `perimer_travaux_du_declencheur` : le nombre affiché à
    côté du bouton est exactement ce que le bouton périme."""
    from oto_mcp import db
    t, secret = _webhook(db, procedure="file-ce-qui-attend", max_per_hour=100)
    autre, secret_autre = _webhook(db, procedure="file-autre-agent", max_per_hour=100)
    _livrer(t, secret, 3)
    _livrer(autre, secret_autre, 2)
    jobs = [l["job_id"] for l in db.livraisons(t["id"], ORG)]
    _poser_statut(jobs[0], "held")
    _poser_statut(jobs[1], "done")

    assert db.file_du_declencheur(t["id"], ORG) == {"pending": 1, "held": 1}
    assert db.file_du_declencheur(autre["id"], ORG) == {"pending": 2, "held": 0}, (
        "la file d'un autre agent ne se mélange pas")
    assert db.file_du_declencheur(t["id"], ORG + 1) == {"pending": 0, "held": 0}, (
        "org-scopé")

    assert db.perimer_travaux_du_declencheur(t["id"], ORG) == 2
    assert db.file_du_declencheur(t["id"], ORG) == {"pending": 0, "held": 0}


def test_les_livraisons_d_une_AUTRE_org_sont_invisibles(live):
    from oto_mcp import db
    t, _ = _webhook(db, procedure="veille-autre-org")
    with db._connect() as conn:
        db.enregistrer(conn, t["id"], ORG, db.QUEUED, job_id=1)
    assert db.livraisons(t["id"], ORG + 1) == []
    assert db.comptage_livraisons(t["id"], ORG + 1)["recues_24h"] == 0


def test_deux_livraisons_SIMULTANEES_ne_reservent_pas_le_meme_creneau(live):
    """⚠️ Le banc qu'aucun test mono-fil ne peut rendre.

    Une rafale est CONCURRENTE par définition. Sans le verrou sur la ligne du
    déclencheur, deux livraisons simultanées lisent en READ COMMITTED les mêmes
    créneaux, se croient toutes deux sous le débit, et partent ensemble : le
    lissage serait inerte exactement au moment où il sert.

    Ici, débit 1 : A prend le verrou et réserve ; B doit ATTENDRE, puis voir le
    créneau de A et passer une heure plus tard.
    """
    import threading
    from oto_mcp import db
    t, _ = _webhook(db, procedure="veille-concurrente")

    vus, erreurs = [], []
    depart = threading.Barrier(2)

    def livrer(tag, lent):
        try:
            with db._connect() as conn:
                db.verrouiller_le_declencheur(conn, t["id"])
                depart.wait(timeout=5) if lent else None
                retard = db.retard_de_lissage(conn, t["id"], 1, 3600)
                vus.append((tag, retard))
                maintenant = conn.execute("SELECT NOW() AS n").fetchone()["n"]
                db.enregistrer(conn, t["id"], ORG, db.QUEUED, job_id=1,
                               due_at=maintenant)
        except Exception as e:  # noqa: SILENT — relayé par l'assert final
            erreurs.append(f"{tag}: {e!r}")

    A = threading.Thread(target=livrer, args=("A", True))
    A.start()
    depart.wait(timeout=5)
    B = threading.Thread(target=livrer, args=("B", False))
    B.start()
    A.join(timeout=10); B.join(timeout=10)

    assert not erreurs, erreurs
    retards = sorted(r for _, r in vus)
    assert retards[0] == 0 and retards[1] >= 3500, (
        f"les deux livraisons ont lu {retards} — sans sérialisation elles "
        "partiraient toutes deux tout de suite")


# ── 4. le retard et la péremption, à la réservation ───────────────────────────

def test_un_travail_RETARDE_n_est_pas_reservable_avant_l_heure(live):
    from oto_mcp import db
    db.enqueue_job(8201, "start", payload={"procedure": "p"}, delai_s=3600)
    assert db.claim_next_job(8201, "w", lease_seconds=60) is None, (
        "un travail lissé ne part pas avant son heure")


def test_sans_delai_le_travail_part_tout_de_suite(live):
    from oto_mcp import db
    j = db.enqueue_job(8202, "start", payload={"procedure": "p"})
    pris = db.claim_next_job(8202, "w", lease_seconds=60)
    assert pris and pris["id"] == j["id"]


def test_un_travail_trop_VIEUX_perime_au_lieu_de_partir(live):
    """⚠️ Ce qui empêche le lissage de devenir un ARRIÉRÉ. Sans lui, une rafale
    retardée se déverserait le lendemain et un agent traiterait l'événement
    d'hier comme s'il venait d'arriver — un résultat faux, pas tardif."""
    from oto_mcp import db
    j = db.enqueue_job(8203, "start", payload={"procedure": "p"}, perime_apres_s=60)
    with db._connect() as conn:
        conn.execute("UPDATE runner_jobs SET created_at = NOW() - INTERVAL '2 hours' "
                     "WHERE id = %s", (j["id"],))
    assert db.claim_next_job(8203, "w", lease_seconds=60) is None
    assert db.get_job(j["id"], 8203)["status"] == "expired"


def test_un_travail_FRAIS_avec_une_peremption_part_normalement(live):
    """Le bord opposé : la péremption ne doit pas manger ce qui est à l'heure."""
    from oto_mcp import db
    j = db.enqueue_job(8204, "start", payload={"procedure": "p"}, perime_apres_s=3600)
    pris = db.claim_next_job(8204, "w", lease_seconds=60)
    assert pris and pris["id"] == j["id"]


def test_un_travail_SANS_peremption_ne_perime_jamais(live):
    """Tout ce qui existait avant ce lot : aucune charge ne porte la clé."""
    from oto_mcp import db
    j = db.enqueue_job(8205, "start", payload={"procedure": "p"})
    with db._connect() as conn:
        conn.execute("UPDATE runner_jobs SET created_at = NOW() - INTERVAL '30 days' "
                     "WHERE id = %s", (j["id"],))
    pris = db.claim_next_job(8205, "w", lease_seconds=60)
    assert pris and pris["id"] == j["id"]


# ── 5. la RÉPÉTITION du déploiement, sur une base d'AVANT ─────────────────────

def test_un_declencheur_DEJA_POSE_survit_a_la_migration_et_tique_encore(pg_dsn):
    """⚠️⚠️ LE banc du déploiement, et le plus dangereux du lot.

    `due_triggers` filtre désormais `kind = 'schedule'`. Si les lignes DÉJÀ EN
    PRODUCTION n'héritaient pas de cette valeur, **toutes les automatisations
    programmées cesseraient de partir** à la seconde du déploiement — sans erreur,
    sans trace, juste un tick qui ne trouve plus rien.

    La garde est le `NOT NULL DEFAULT 'schedule'` de l'ALTER. Ce banc ne le lit
    pas : il FABRIQUE l'état d'avant (une ligne posée, puis les colonnes du lot
    retirées), rejoue la migration du boot, et vérifie la CONSÉQUENCE — la ligne
    porte le genre, et le tick la voit toujours.

    Même méthode que `test_boot_order_replay` §4 : on défait l'état d'après plutôt
    que d'exhumer un DDL figé qui se périmerait.
    """
    import datetime
    import uuid as _uuid
    import os
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    nom = "oto_hook_migr_" + _uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + nom
    url_avant, pool_avant = os.environ.get("DATABASE_URL"), dbconn._pool
    cle_avant = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["DATABASE_URL"] = dsn
    os.environ["OTO_MCP_MASTER_KEY"] = "4" * 64
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        from oto_mcp import db as dbf
        init_db()
        hier = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
        t = dbf.create_trigger(9100, "alexis", procedure="veille-historique",
                               cron="5 6 * * *", tz="UTC", tools=["a"],
                               next_due=hier)
        # ── l'état d'AVANT : les colonnes du lot n'existent pas encore ──
        with dbf._connect() as conn:
            for col in ("kind", "hook_secret_hash", "payload_mode",
                        "payload_fields", "max_per_hour", "fraicheur_s"):
                conn.execute(f"ALTER TABLE runner_triggers DROP COLUMN {col}")
            conn.execute("ALTER TABLE runner_triggers ALTER COLUMN cron SET NOT NULL")
            conn.execute("ALTER TABLE runner_triggers ALTER COLUMN next_due SET NOT NULL")
        # ── on rejoue la migration du boot, comme le fera le déploiement ──
        init_db()
        with dbf._connect() as conn:
            row = conn.execute("SELECT kind, payload_mode FROM runner_triggers "
                               "WHERE id = %s", (t["id"],)).fetchone()
        assert row["kind"] == "schedule", (
            "une ligne d'avant le lot DOIT hériter du genre — sinon le tick, qui "
            "filtre `kind = 'schedule'`, ne la voit plus jamais")
        assert row["payload_mode"] == "ignore"
        assert t["id"] in [d["id"] for d in dbf.due_triggers(limit=500)], (
            "le déclencheur historique doit toujours être DÛ après la migration")
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = pool_avant
        for cle, valeur in (("DATABASE_URL", url_avant),
                            ("OTO_MCP_MASTER_KEY", cle_avant)):
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()


# ── l'authentification PAR SIGNATURE, en base (25/09/2026) ─────────────────────

def _signe(db, procedure, org=ORG):
    """Un webhook passé en mode signature, par le même geste que l'écran."""
    from oto_mcp import runner_hook
    t, porteur = _webhook(db, procedure=procedure, org=org)
    secret = "whsec_" + __import__("base64").b64encode(b"cle " + procedure.encode()).decode()
    db.poser_auth_de_hook(t["id"], org, "standard_webhooks",
                          secret_enc=runner_hook.chiffrer_secret_de_signature(t["id"], secret))
    db.poser_secret_de_hook(t["id"], org, None)
    return t, porteur, secret


def test_un_agent_NAIT_au_porteur_et_le_lit(live):
    from oto_mcp import db
    t, _ = _webhook(db, procedure="auth-defaut")
    lu = db.get_trigger(t["id"], ORG)
    assert lu["hook_auth"] == "bearer" and lu["signing_secret_set"] is False
    assert "hook_signing_secret_enc" not in lu, "le chiffré ne se sert JAMAIS"


def test_le_mode_signature_ETEINT_le_porteur_EN_SQL(live):
    """⚠️ LE banc de sécurité du lot : le bon porteur, sur un agent passé en
    signature, ne trouve plus rien — la garde est dans le WHERE."""
    from oto_mcp import db, runner_hook
    t, porteur = _webhook(db, procedure="auth-eteint")
    db.poser_auth_de_hook(t["id"], ORG, "standard_webhooks", secret_enc="x")
    assert db.trigger_par_secret(t["id"], runner_hook.hacher(porteur)) is None


def test_trigger_signe_ne_trouve_QUE_le_mode_signature(live):
    from oto_mcp import db
    au_porteur, _ = _webhook(db, procedure="auth-porteur")
    t, _, _ = _signe(db, "auth-signe")
    assert db.trigger_signe(au_porteur["id"]) is None
    trouve = db.trigger_signe(t["id"])
    assert trouve and trouve["hook_signing_secret_enc"]
    assert trouve["signing_secret_set"] is True


def test_revenir_au_porteur_EFFACE_le_chiffre(live):
    from oto_mcp import db
    t, _, _ = _signe(db, "auth-retour")
    db.poser_auth_de_hook(t["id"], ORG, "bearer", effacer_le_secret=True)
    lu = db.get_trigger(t["id"], ORG)
    assert lu["hook_auth"] == "bearer" and lu["signing_secret_set"] is False
    assert db.trigger_signe(t["id"]) is None


def test_bout_en_bout_une_livraison_SIGNEE_enfile_puis_sa_RETENTATIVE_non(live):
    """La route telle qu'elle tourne : vraie base, vrai chiffrement, vrai index."""
    import base64, hashlib, hmac, time
    from oto_mcp import db, runner_hook
    t, _, secret = _signe(db, "auth-bout-en-bout")
    corps = b'{"event_type": "note.generated", "note_id": "not_1"}'
    ts = str(int(time.time()))
    cle = base64.b64decode(secret[len("whsec_"):])
    sig = "v1," + base64.b64encode(hmac.new(cle, f"evt_1.{ts}.".encode() + corps,
                                           hashlib.sha256).digest()).decode()
    recue = runner_hook.SignatureRecue("evt_1", ts, sig, corps)
    premier = runner_hook.declencher(t["id"], None, {"note_id": "not_1"}, "Granola",
                                     signature=recue)
    assert premier["job_id"] and not premier.get("duplicate")
    second = runner_hook.declencher(t["id"], None, {"note_id": "not_1"}, "Granola",
                                    signature=recue)
    assert second["duplicate"] is True and second["job_id"] == premier["job_id"]
    livrees = db.livraisons(t["id"], ORG)
    assert len(livrees) == 1, "une retentative ne laisse ni travail ni livraison"


def test_l_index_refuse_DEUX_livraisons_acceptees_du_meme_identifiant(live):
    """Le filet sous la lecture : si deux écritures passaient quand même, la base
    en refuse la seconde."""
    import psycopg
    from oto_mcp import db
    t, _, _ = _signe(db, "auth-index")
    with db._connect() as conn:
        db.enregistrer(conn, t["id"], ORG, db.QUEUED, external_id="evt_x")
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db._connect() as conn:
            db.enregistrer(conn, t["id"], ORG, db.QUEUED, external_id="evt_x")


def test_un_REFUS_ne_bloque_pas_la_retentative(live):
    """Un 409 (en pause) n'a produit aucun travail : la même livraison, rejouée
    une fois l'agent rallumé, doit passer."""
    from oto_mcp import db
    t, _, _ = _signe(db, "auth-refus")
    with db._connect() as conn:
        db.enregistrer(conn, t["id"], ORG, db.REFUSE_PAUSED, external_id="evt_y")
        assert db.livraison_acceptee(conn, t["id"], "evt_y") is None
        db.enregistrer(conn, t["id"], ORG, db.QUEUED, external_id="evt_y")
        assert db.livraison_acceptee(conn, t["id"], "evt_y")


def test_les_livraisons_AU_PORTEUR_n_ont_pas_d_identifiant_et_ne_se_genent_pas(live):
    """Sans identifiant, l'index partiel ne s'applique pas : deux livraisons au
    porteur restent deux livraisons, comme avant ce lot."""
    from oto_mcp import db
    t, _ = _webhook(db, procedure="auth-sans-id")
    with db._connect() as conn:
        db.enregistrer(conn, t["id"], ORG, db.QUEUED)
        db.enregistrer(conn, t["id"], ORG, db.QUEUED)
    assert len(db.livraisons(t["id"], ORG)) == 2


# ── le plafond journalier et l'adresse privée, en base (25/09/2026) ────────────

def test_un_agent_NAIT_sans_plafond_ni_adresse_privee(live):
    from oto_mcp import db
    t, _ = _webhook(db, procedure="opt-defaut")
    lu = db.get_trigger(t["id"], ORG)
    assert lu["max_per_day"] is None and lu["hook_slug"] is None


def test_le_plafond_s_ecrit_a_la_creation_et_se_RETIRE(live):
    from oto_mcp import db
    t, _ = _webhook(db, procedure="opt-plafond", max_per_day=5)
    assert db.get_trigger(t["id"], ORG)["max_per_day"] == 5
    db.update_trigger(t["id"], ORG, {"max_per_day": None})
    assert db.get_trigger(t["id"], ORG)["max_per_day"] is None


def test_la_fenetre_ne_compte_que_les_ACCEPTEES_des_24_dernieres_heures(live):
    from oto_mcp import db
    t, _ = _webhook(db, procedure="opt-fenetre")
    with db._connect() as conn:
        for outcome in (db.QUEUED, db.DELAYED, db.REFUSE_PAUSED, db.REFUSE_DAILY_CAP):
            db.enregistrer(conn, t["id"], ORG, outcome)
        # Une acceptée d'il y a 25 h : hors fenêtre.
        conn.execute("INSERT INTO runner_hook_deliveries (trigger_id, org_id, outcome, "
                     "received_at) VALUES (%s, %s, 'queued', NOW() - INTERVAL '25 hours')",
                     (t["id"], ORG))
    with db._connect() as conn:
        n, sortie = db.acceptees_sur_24h(conn, t["id"])
    assert n == 2
    assert 86_000 < sortie <= 86_400, "la plus ancienne sort dans ~24 h"


def test_une_fenetre_vide_rend_zero_et_zero(live):
    from oto_mcp import db
    t, _ = _webhook(db, procedure="opt-vide")
    with db._connect() as conn:
        assert db.acceptees_sur_24h(conn, t["id"]) == (0, 0)


def test_bout_en_bout_le_plafond_REFUSE_la_livraison_de_trop(live):
    from oto_mcp import db, runner_hook
    t, porteur = _webhook(db, procedure="opt-bout-en-bout", max_per_day=2)
    for _ in range(2):
        assert runner_hook.declencher(t["id"], porteur, {}, "src")["job_id"]
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(t["id"], porteur, {}, "src")
    assert (e.value.statut, e.value.code) == (429, "hook_daily_cap")
    issues = [l["outcome"] for l in db.livraisons(t["id"], ORG)]
    assert issues.count("queued") == 2 and issues.count("refused_daily_cap") == 1


def test_l_adresse_privee_se_resout_et_ferme_l_id(live):
    from oto_mcp import db, runner_hook
    t, porteur = _webhook(db, procedure="opt-adresse")
    adresse = runner_hook.nouvelle_adresse()
    db.poser_adresse_de_hook(t["id"], ORG, adresse)
    assert runner_hook.resoudre_adresse(adresse) == (t["id"], True)
    with pytest.raises(runner_hook.HookRefus):
        runner_hook.declencher(t["id"], porteur, {}, "src")
    assert runner_hook.declencher(t["id"], porteur, {}, "src",
                                  par_adresse_privee=True)["job_id"]
    db.poser_adresse_de_hook(t["id"], ORG, None)
    assert runner_hook.resoudre_adresse(adresse) == (None, True)
    assert runner_hook.declencher(t["id"], porteur, {}, "src")["job_id"]


def test_deux_agents_ne_partagent_JAMAIS_une_adresse(live):
    import psycopg
    from oto_mcp import db
    a, _ = _webhook(db, procedure="opt-unique-a")
    b, _ = _webhook(db, procedure="opt-unique-b")
    db.poser_adresse_de_hook(a["id"], ORG, "h_meme")
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.poser_adresse_de_hook(b["id"], ORG, "h_meme")
