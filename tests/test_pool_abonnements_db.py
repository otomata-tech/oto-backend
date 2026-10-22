"""Le POOL d'org des abonnements, EN BASE (25/09/2026).

Une org en mode `pool` fait tourner ses travaux sur l'abonnement d'un membre qui l'a
PRÊTÉ à cette org. Ce qui ne se juge qu'en SQL, sur une vraie base :

1. le choix du prêteur — de CETTE org, membre, servable, libre, le MOINS récemment
   servi d'abord ;
2. l'attente : aucun prêteur libre = le travail reste `pending`, aucune tentative
   brûlée ;
3. un travail à la fois PAR ABONNEMENT, y compris sous deux workers simultanés, et
   quel que soit le mode qui l'y a mis (un travail perso du prêteur compte) ;
4. le forfait rapporté au PRÊTEUR, sous SON seuil.

Le mode personnel a ses bancs (`test_abonnement_personnel*.py`), inchangés.
"""
from __future__ import annotations

import os
import threading
import uuid

import pytest

_F = "claude_subscription"


@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_pool_abo_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    avant_url, avant_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    avant_key = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["DATABASE_URL"] = dsn
    os.environ["OTO_MCP_MASTER_KEY"] = "5" * 64
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


def _personne(sub):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("INSERT INTO users (sub) VALUES (%s) ON CONFLICT DO NOTHING", (sub,))
    return sub


def _org(nom, *membres, mode="pool"):
    """Une org, ses membres, et son mode pour la famille."""
    from oto_mcp import org_store
    from oto_mcp.db import org_subscription_pool as P
    admin = _personne(f"{nom}-admin")
    oid = org_store.create_org(f"Org {nom}", created_by=admin)
    org_store.add_org_member(oid, admin, "org_admin")
    for m in membres:
        org_store.add_org_member(oid, _personne(m), "org_member")
    if mode:
        P.poser_mode(oid, _F, mode, admin)
    return oid


def _abonne(sub, *, statut="connected", reset=None, pret_a=()):
    from oto_mcp.db import org_subscription_pool as P
    from oto_mcp.db import user_subscriptions as US
    _personne(sub)
    US.upsert_sandbox(sub, _F, f"sandbox-{sub}")
    US.marquer_statut(sub, _F, statut, limit_reset_at=reset, ok=statut == "connected")
    if pret_a:
        P.poser_prets(sub, _F, pret_a)
    return sub


def _ouvert_ici(org, sub, famille):
    """Depuis le 08/10/2026 un abonnement ne sert que les orgs où la personne l'a
    ouvert, membre : ces bancs jugent la file, ils ouvrent donc l'org du travail."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("INSERT INTO orgs (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                     (org, f"Org {org}"))
        conn.execute("INSERT INTO org_members (org_id, sub) VALUES (%s, %s) "
                     "ON CONFLICT DO NOTHING", (org, sub))
        conn.execute("INSERT INTO user_model_subscription_orgs (sub, famille, org_id) "
                     "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (sub, famille, org))


def _travail(org, sub, famille=_F):
    from oto_mcp import db
    _ouvert_ici(org, sub, famille)
    return db.enqueue_job(org, "start", sub=sub,
                          payload={"procedure": "p", "model": "sub:sonnet",
                                   "model_family": famille})["id"]


def _claim(org, worker="w-pool"):
    from oto_mcp import db
    return db.claim_next_job(org, worker, lease_seconds=60,
                             depot=_F, famille_seule=True)


def _forfait(job):
    from oto_mcp import db
    return db.porteur_du_forfait(job)


def _etat(job_id):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return dict(conn.execute("SELECT status, attempts FROM runner_jobs WHERE id = %s",
                                 (job_id,)).fetchone())


def _dans_une_heure():
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute(
            "SELECT EXTRACT(EPOCH FROM NOW() + interval '1 hour')::bigint AS t"
        ).fetchone()["t"]


def test_le_pool_prend_le_preteur_le_MOINS_recemment_servi(live):
    from oto_mcp import db
    oid = _org("lru", "lru-dem", "lru-p1", "lru-p2")
    p1 = _abonne("lru-p1", pret_a=[oid])
    p2 = _abonne("lru-p2", pret_a=[oid])
    # Le demandeur n'a AUCUN abonnement : en pool, il ne paie pas.
    jobs = [_travail(oid, "lru-dem") for _ in range(4)]

    un = _claim(oid)
    assert (un["id"], _forfait(un)) == (jobs[0], p1), "jamais servis : le premier prêt"
    deux = _claim(oid)
    assert (deux["id"], _forfait(deux)) == (jobs[1], p2), "p1 est en vol : p2"
    assert _claim(oid) is None, "les deux prêteurs sont en vol : le troisième attend"

    db.complete_job(un["id"], "w-pool", ok=True)
    db.complete_job(deux["id"], "w-pool", ok=True)
    trois = _claim(oid)
    assert _forfait(trois) == p1, "p1 a servi AVANT p2 : c'est lui le moins récent"
    db.complete_job(trois["id"], "w-pool", ok=True)
    assert _forfait(_claim(oid)) == p2, "le tourniquet continue, p2 n'est pas affamé"


def test_sans_preteur_le_travail_ATTEND_sans_bruler_de_tentative(live):
    from oto_mcp.db import org_subscription_pool as P
    oid = _org("vide", "vide-dem", "vide-p")
    travail = _travail(oid, "vide-dem")
    assert _claim(oid) is None, "pool vide : rien ne part"
    assert _etat(travail) == {"status": "pending", "attempts": 0}

    # Un abonnement connecté mais NON prêté ne sert pas le pool.
    _abonne("vide-p")
    assert _claim(oid) is None, "connecté ne veut pas dire prêté : opt-in"
    P.poser_prets("vide-p", _F, [oid])
    pris = _claim(oid)
    assert pris["id"] == travail and _forfait(pris) == "vide-p"
    assert pris["attempts"] == 1, "une seule tentative : celle qui part"


def test_deux_workers_SIMULTANES_ne_font_pas_servir_deux_travaux_au_meme_preteur(live):
    oid = _org("course", "course-dem", "course-p")
    _abonne("course-p", pret_a=[oid])
    for _ in range(6):
        _travail(oid, "course-dem")
    pris, barriere = [], threading.Barrier(3)

    def _un(i):
        barriere.wait()
        j = _claim(oid, worker=f"w-course-{i}")
        if j:
            pris.append(_forfait(j))

    fils = [threading.Thread(target=_un, args=(i,)) for i in range(3)]
    [f.start() for f in fils]
    [f.join() for f in fils]
    assert pris == ["course-p"], f"{len(pris)} travaux en vol sur un seul prêteur : {pris}"


def test_deux_workers_SIMULTANES_deux_preteurs_chacun_le_sien(live):
    oid = _org("duo", "duo-dem", "duo-p1", "duo-p2")
    _abonne("duo-p1", pret_a=[oid])
    _abonne("duo-p2", pret_a=[oid])
    for _ in range(6):
        _travail(oid, "duo-dem")
    pris, barriere = [], threading.Barrier(4)

    def _un(i):
        barriere.wait()
        j = _claim(oid, worker=f"w-duo-{i}")
        if j:
            pris.append(_forfait(j))

    fils = [threading.Thread(target=_un, args=(i,)) for i in range(4)]
    [f.start() for f in fils]
    [f.join() for f in fils]
    # La course peut défaire une prise (le second sur le même prêteur recule) : jamais
    # DEUX travaux sur un même abonnement, et le sondage suivant sert l'autre.
    assert len(pris) == len(set(pris)), f"un prêteur sert deux travaux : {pris}"
    while len(pris) < 2:
        j = _claim(oid, worker="w-duo-rattrapage")
        assert j, "un prêteur libre reste : le sondage suivant le sert"
        pris.append(_forfait(j))
    assert sorted(pris) == ["duo-p1", "duo-p2"]


def test_un_travail_PERSO_du_preteur_le_rend_indisponible_au_pool(live):
    """Un abonnement, un bac à sable : la sérialisation porte sur l'ABONNEMENT, quel
    que soit le mode de l'org qui l'y a mis — dans les deux sens."""
    from oto_mcp import db
    perso = _org("perso-p", "mixte-p", mode=None)          # mode personnel
    pool = _org("pool-p", "mixte-dem", "mixte-p")
    _abonne("mixte-p", pret_a=[pool])
    a_moi = _travail(perso, "mixte-p")
    au_pool = _travail(pool, "mixte-dem")

    assert _claim(perso)["id"] == a_moi
    assert _claim(pool) is None, "le prêteur tourne pour lui-même : le pool attend"
    db.complete_job(a_moi, "w-pool", ok=True)
    assert _claim(pool)["id"] == au_pool

    second_a_moi = _travail(perso, "mixte-p")
    assert _claim(perso) is None, "et son travail perso attend celui qu'il sert au pool"
    db.complete_job(au_pool, "w-pool", ok=True)
    assert _claim(perso)["id"] == second_a_moi


def test_un_pret_RETIRE_ne_sert_plus_le_travail_suivant(live):
    from oto_mcp import db
    from oto_mcp.db import org_subscription_pool as P
    oid = _org("retrait", "retrait-dem", "retrait-p")
    _abonne("retrait-p", pret_a=[oid])
    en_cours, suivant = _travail(oid, "retrait-dem"), _travail(oid, "retrait-dem")
    assert _claim(oid)["id"] == en_cours
    P.poser_prets("retrait-p", _F, [])
    assert _etat(en_cours)["status"] == "claimed", "le run en cours n'est jamais coupé"
    db.complete_job(en_cours, "w-pool", ok=True)
    assert _claim(oid) is None, "le suivant ne le voit plus"
    assert _etat(suivant) == {"status": "pending", "attempts": 0}


def test_un_pret_ne_sert_QUE_l_org_a_laquelle_il_est_fait(live):
    une = _org("une", "une-dem", "deux-orgs-p")
    autre = _org("autre", "autre-dem", "deux-orgs-p")
    _abonne("deux-orgs-p", pret_a=[une])
    _travail(autre, "autre-dem")
    assert _claim(autre) is None, "membre des deux, prêté à une seule : par org"


def test_un_membre_PARTI_ne_prete_plus(live):
    from oto_mcp import org_store
    oid = _org("depart", "depart-dem", "depart-p")
    _abonne("depart-p", pret_a=[oid])
    _travail(oid, "depart-dem")
    org_store.remove_org_member(oid, "depart-p")
    assert _claim(oid) is None, "le prêt survit en base mais ne sert plus : non membre"


def test_une_org_en_PERSONNEL_ignore_les_prets(live):
    """Le mode personnel à l'octet : le travail attend la connexion de SON demandeur,
    même si un collègue prête à l'org."""
    from oto_mcp.db import org_subscription_pool as P
    oid = _org("perso", "perso-dem", "perso-p", mode="personnel")
    _abonne("perso-p", pret_a=[oid])
    _abonne("perso-dem", statut="needs_login")
    travail = _travail(oid, "perso-dem")
    assert _claim(oid) is None, "le demandeur doit se reconnecter : il attend"
    P.poser_mode(oid, _F, "pool", "perso-admin")
    pris = _claim(oid)
    assert pris["id"] == travail and _forfait(pris) == "perso-p", (
        "en pool, sa connexion ne compte plus : le prêteur sert")


def _voie(job):
    from oto_mcp import db
    return db.par_le_pool(job)


def test_sans_mode_REGLE_le_pool_sert_qui_n_a_pas_d_abonnement(live):
    """Le défaut (revue du 19/10/2026) : le pool COMPLÈTE. Un membre sans abonnement
    est servi par un prêt, sans qu'un admin passe l'org en pool."""
    oid = _org("cpl-sans", "cpl-sans-dem", "cpl-sans-p", mode=None)
    _abonne("cpl-sans-p", pret_a=[oid])
    travail = _travail(oid, "cpl-sans-dem")
    pris = _claim(oid)
    assert pris["id"] == travail and _forfait(pris) == "cpl-sans-p"
    assert _voie(pris) is True


def test_sans_mode_REGLE_un_membre_garde_SON_abonnement(live):
    """Un prêt ne remplace l'abonnement de personne : le demandeur connecté tourne
    sur le sien, et le prêteur n'est pas consommé (son tour de pool ne bouge pas)."""
    from oto_mcp.db._conn import _connect
    oid = _org("cpl-garde", "cpl-garde-dem", "cpl-garde-p", mode=None)
    _abonne("cpl-garde-dem")
    _abonne("cpl-garde-p", pret_a=[oid])
    _travail(oid, "cpl-garde-dem")
    pris = _claim(oid)
    assert _forfait(pris) == "cpl-garde-dem" and _voie(pris) is False
    with _connect() as conn:
        servi = conn.execute("SELECT servi_at FROM user_model_subscription_loans "
                             "WHERE sub = 'cpl-garde-p'").fetchone()["servi_at"]
    assert servi is None
    # Occupé, il ATTEND le sien : le pool ne sert pas un membre dont l'abonnement sert.
    _travail(oid, "cpl-garde-dem")
    assert _claim(oid) is None


def test_sans_mode_REGLE_un_abonnement_AU_PLAFOND_passe_par_le_pool(live):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        apres = conn.execute("SELECT NOW() + interval '1 hour' AS t").fetchone()["t"]
    oid = _org("cpl-plafond", "cpl-plafond-dem", "cpl-plafond-p", mode=None)
    _abonne("cpl-plafond-dem", statut="paused_limit", reset=apres)
    _abonne("cpl-plafond-p", pret_a=[oid])
    _travail(oid, "cpl-plafond-dem")
    pris = _claim(oid)
    assert _forfait(pris) == "cpl-plafond-p" and _voie(pris) is True


def test_sans_mode_REGLE_un_pret_DECONNECTE_ne_compte_pas(live):
    """Un prêteur qui se déconnecte n'arrête l'agent de personne : le demandeur
    connecté tourne sur le sien, celui qui n'a rien suit la voie personnelle comme
    sans pool (la remise dit pourquoi)."""
    oid = _org("cpl-deco", "cpl-deco-dem", "cpl-deco-nu", "cpl-deco-p", mode=None)
    _abonne("cpl-deco-dem")
    _abonne("cpl-deco-p", statut="disconnected", pret_a=[oid])
    _travail(oid, "cpl-deco-dem")
    assert _forfait(_claim(oid)) == "cpl-deco-dem"
    _travail(oid, "cpl-deco-nu")
    pris = _claim(oid)
    assert _forfait(pris) == "cpl-deco-nu" and _voie(pris) is False


def test_le_repli_API_ne_fige_pas_le_defaut(live):
    """Ouvrir le repli fait naître la ligne de l'org SANS mode : le pool complète
    toujours."""
    from oto_mcp.db import org_subscription_pool as P
    oid = _org("repli-defaut", "rd-dem", "rd-p", mode=None)
    assert P.set_repli_api(oid, _F, True, "rd-dem")["mode"] is None
    _abonne("rd-p", pret_a=[oid])
    assert P.en_pool(oid, _F) is False, "le pool EXCLUSIF reste un choix d'admin"
    _travail(oid, "rd-dem")
    assert _forfait(_claim(oid)) == "rd-p"


def test_un_PERSONNEL_regle_garde_les_prets_sans_effet(live):
    oid = _org("perso-regle", "pr-dem", "pr-p", mode="personnel")
    _abonne("pr-p", pret_a=[oid])
    _travail(oid, "pr-dem")
    pris = _claim(oid)
    assert _forfait(pris) == "pr-dem" and _voie(pris) is False


def test_sans_mode_REGLE_la_pose_suit_la_meme_regle(live, monkeypatch):
    """La pose lit la règle de la réservation (`_abonnement.par_le_pool`) : sans
    abonnement, un prêt suffit ; une flotte exige un prêt, jamais le forfait de son
    créateur."""
    from oto_mcp.capabilities import _abonnement
    monkeypatch.setattr(_abonnement.access, "has_option", lambda *a, **k: True)
    oid = _org("cpl-pose", "cpl-pose-dem", "cpl-pose-moi", "cpl-pose-p", mode=None)
    _abonne("cpl-pose-moi")
    _ouvert_ici(oid, "cpl-pose-moi", _F)
    _abonnement.exiger_a_la_pose("cpl-pose-moi", None, _F, org_id=oid)
    with pytest.raises(Exception) as e:
        _abonnement.exiger_a_la_pose("cpl-pose-moi", None, _F, org_id=oid, flotte=True)
    assert e.value.code == "subscription_personal_only"
    with pytest.raises(Exception) as e:
        _abonnement.exiger_a_la_pose("cpl-pose-dem", None, _F, org_id=oid)
    assert e.value.code == "subscription_not_connected"
    _abonne("cpl-pose-p", pret_a=[oid])
    _abonnement.exiger_a_la_pose("cpl-pose-dem", None, _F, org_id=oid)
    _abonnement.exiger_a_la_pose("cpl-pose-moi", None, _F, org_id=oid, flotte=True)


def test_un_preteur_AU_PLAFOND_est_saute_jusqu_a_son_echeance(live):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        futur = conn.execute("SELECT NOW() + interval '1 hour' AS t").fetchone()["t"]
        passe = conn.execute("SELECT NOW() - interval '1 minute' AS t").fetchone()["t"]
    from oto_mcp.db import user_subscriptions as US
    oid = _org("cap", "cap-dem", "cap-plein", "cap-libre")
    _abonne("cap-plein", statut="paused_limit", reset=futur, pret_a=[oid])
    _travail(oid, "cap-dem")
    assert _claim(oid) is None, "seul prêteur, au plafond : on attend"
    _abonne("cap-libre", pret_a=[oid])
    assert _forfait(_claim(oid)) == "cap-libre", "le plafonné est sauté, l'autre sert"
    _travail(oid, "cap-dem")
    US.marquer_statut("cap-plein", _F, US.PLAFOND, limit_reset_at=passe)
    assert _forfait(_claim(oid)) == "cap-plein", "échéance passée : il sert de nouveau"


def test_le_forfait_est_rapporte_au_PRETEUR_sous_SON_seuil(live):
    """Le rapport de fin écrit sur la connexion qui a SERVI, et son plafond perso
    compte — jamais le demandeur, qui n'a rien payé."""
    from oto_mcp import db
    from oto_mcp.capabilities import _abonnement
    from oto_mcp.db import user_subscriptions as US
    oid = _org("rapport", "rapport-dem", "rapport-p")
    _abonne("rapport-p", pret_a=[oid])
    US.poser_limite("rapport-p", _F, 50)
    _abonne("rapport-dem")
    travail = _travail(oid, "rapport-dem")
    assert _forfait(_claim(oid)) == "rapport-p"
    db.complete_job(travail, "w-pool", ok=True)
    conclu = db.porteur_et_famille(travail)
    assert conclu["abonnement"] == "rapport-p" and conclu["sub"] == "rapport-dem"

    _abonnement.noter_rapport_du_travail(travail, True, {"abonnement": {
        "etat": "allowed",
        "fenetres": {"five_hour": {"utilization": 0.6, "resetsAt": _dans_une_heure()}}}})
    assert US.get_subscription("rapport-p", _F)["statut"] == US.PLAFOND, (
        "0.6 franchit SON plafond perso de 50 % (l'org est au défaut, 80)")
    assert US.get_subscription("rapport-dem", _F)["statut"] == US.CONNECTE, (
        "le demandeur n'a rien payé : sa connexion n'a pas bougé")


def test_la_remise_donne_le_SANDBOX_du_preteur(live):
    from oto_mcp.capabilities import runner_jobs as RJ
    oid = _org("remise", "remise-dem", "remise-p")
    _abonne("remise-p", pret_a=[oid])
    _travail(oid, "remise-dem")
    servi = RJ._avec_cle(_claim(oid), _F, "w-pool", worker=True, org_key_only=True)
    assert servi["sandbox_id"] == "sandbox-remise-p"
    assert "delegation_refusee" not in servi and "model_key" not in servi


def test_a_la_remise_un_preteur_qui_a_decroche_REND_le_travail(live):
    """La course entre la prise et la garde : en pool, toujours rendu à la file (un
    autre prêteur le servira), jamais arrêté."""
    from oto_mcp.capabilities import runner_jobs as RJ
    from oto_mcp.db import user_subscriptions as US
    oid = _org("decroche", "decroche-dem", "decroche-p")
    _abonne("decroche-p", pret_a=[oid])
    travail = _travail(oid, "decroche-dem")
    pris = _claim(oid)
    US.marquer_statut("decroche-p", _F, US.A_RECONNECTER)
    servi = RJ._avec_cle(pris, _F, "w-pool", worker=True, org_key_only=True)
    assert "pool" in servi["delegation_refusee"]
    assert _etat(travail) == {"status": "pending", "attempts": 0}


def test_une_flotte_hors_pool_est_ARRETEE_a_la_remise(live, monkeypatch):
    """Une flotte armée pendant le pool, dont l'org repasse en personnel, ne tourne
    pas sur le forfait de son créateur."""
    from oto_mcp.capabilities import runner_jobs as RJ
    arrets = []
    monkeypatch.setattr(RJ.db, "arreter_definitivement",
                        lambda job_id, appelant, raison: arrets.append(raison))
    oid = _org("flotte", "flotte-dem", mode="personnel")
    _abonne("flotte-dem")
    job = {"id": 1, "org_id": oid, "sub": "flotte-dem", "fleet_id": 7,
           "payload": {"model_family": _F, "_plateforme": {"abonnement": "flotte-dem"}}}
    servi = RJ._avec_cle(job, _F, "w-pool", worker=True, org_key_only=True)
    assert "flotte" in servi["delegation_refusee"] and "sandbox_id" not in servi
    assert arrets


class TestPose:
    """Les gardes de pose en mode pool, sur la base."""

    @pytest.fixture(autouse=True)
    def _option(self, monkeypatch):
        from oto_mcp.capabilities import _abonnement
        monkeypatch.setattr(_abonnement.access, "has_option",
                            lambda sub, option, **k: option == _abonnement.OPTION)

    def test_pool_vide_refuse_NOMME(self, live):
        from oto_mcp.capabilities import _abonnement
        oid = _org("pose-vide", "pose-vide-dem")
        with pytest.raises(Exception) as e:
            _abonnement.exiger_a_la_pose("pose-vide-dem", None, _F, org_id=oid)
        assert e.value.code == "subscription_pool_empty"

    def test_pool_garni_le_demandeur_n_a_pas_besoin_de_connexion(self, live):
        from oto_mcp.capabilities import _abonnement
        oid = _org("pose-ok", "pose-ok-dem", "pose-ok-p")
        _abonne("pose-ok-p", pret_a=[oid])
        _abonnement.exiger_a_la_pose("pose-ok-dem", None, _F, org_id=oid)
        _abonnement.exiger_a_la_pose("pose-ok-dem", None, _F, org_id=oid, flotte=True)

    def test_en_personnel_la_flotte_reste_refusee(self, live):
        from oto_mcp.capabilities import _abonnement
        oid = _org("pose-perso", "pose-perso-dem", mode="personnel")
        _abonne("pose-perso-dem")
        with pytest.raises(Exception) as e:
            _abonnement.exiger_a_la_pose("pose-perso-dem", None, _F, org_id=oid,
                                         flotte=True)
        assert e.value.code == "subscription_personal_only"

    def test_en_pool_l_option_reste_exigee(self, live, monkeypatch):
        from oto_mcp.capabilities import _abonnement
        monkeypatch.setattr(_abonnement.access, "has_option", lambda *a, **k: False)
        oid = _org("pose-option", "pose-option-dem", "pose-option-p")
        _abonne("pose-option-p", pret_a=[oid])
        with pytest.raises(Exception) as e:
            _abonnement.exiger_a_la_pose("pose-option-dem", None, _F, org_id=oid)
        assert e.value.code == "subscription_not_enabled"

    def test_en_pool_l_agent_d_un_autre_reste_a_lui(self, live):
        from oto_mcp.capabilities import _abonnement
        oid = _org("pose-autrui", "pose-autrui-dem", "pose-autrui-p")
        _abonne("pose-autrui-p", pret_a=[oid])
        with pytest.raises(Exception) as e:
            _abonnement.exiger_a_la_pose("pose-autrui-dem", "quelqu-un", _F, org_id=oid)
        assert e.value.code == "subscription_personal_only"

    def test_l_enfilage_d_une_flotte_passe_en_pool(self, live):
        from oto_mcp.capabilities import runner_jobs as RJ
        from oto_mcp.capabilities._types import ResolvedCtx
        oid = _org("pose-enq", "pose-enq-dem", "pose-enq-p")
        _abonne("pose-enq-p", pret_a=[oid])
        charge = RJ._charge_et_modele(
            ResolvedCtx(sub="pose-enq-dem", org_id=oid),
            RJ.JobsInput(op="enqueue", kind="start", fleet_id=9,
                         payload={"input": "vas-y", "model": "sub:sonnet"}))
        assert charge["model_family"] == _F


def test_effacer_le_sandbox_efface_ses_prets(live):
    from oto_mcp.db import org_subscription_pool as P
    from oto_mcp.db import user_subscriptions as US
    oid = _org("oubli", "oubli-p")
    _abonne("oubli-p", pret_a=[oid])
    assert P.orgs_pretees("oubli-p", _F) == [oid]
    US.oublier("oubli-p", _F)
    assert P.orgs_pretees("oubli-p", _F) == []


def test_un_preteur_OCCUPE_est_saute_au_profit_d_un_libre(live):
    """Le moins récemment servi n'est pris que s'il est LIBRE : occupé par son propre
    travail (qui ne le fait pas avancer dans le tourniquet), il laisse la place."""
    perso = _org("occupe-perso", "occupe-p1", mode=None)
    oid = _org("occupe", "occupe-dem", "occupe-p1", "occupe-p2")
    _abonne("occupe-p1", pret_a=[oid])     # prêté le premier, jamais servi
    _abonne("occupe-p2", pret_a=[oid])
    _travail(perso, "occupe-p1")
    assert _claim(perso) is not None
    _travail(oid, "occupe-dem")
    assert _forfait(_claim(oid)) == "occupe-p2"


# ── La pose juge « ça tournera un jour ? », pas « à la seconde près ? » ───────

def _plus_tard(h=36):
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=h)


@pytest.fixture()
def _option(monkeypatch):
    from oto_mcp.capabilities import _abonnement
    monkeypatch.setattr(_abonnement.access, "has_option", lambda *a, **k: True)


def test_un_pool_dont_les_preteurs_sont_au_plafond_laisse_POSER(live, _option):
    """Le cas vécu le 27/09/2026 : un seul prêteur, au plafond jusqu'au surlendemain.
    `taille_du_pool` (la réservation) le compte à zéro — c'est juste, il ne peut pas
    servir MAINTENANT. Mais refuser la POSE pour ça rendait le pool strictement pire
    que le mode personnel, où `servable()` dit oui à un `paused_limit` quelle que soit
    son échéance. Un plafond est temporaire : l'agent partira à la réinitialisation."""
    from oto_mcp.capabilities import _abonnement
    from oto_mcp.db import org_subscription_pool as P

    oid = _org("pose-plafond", "pp-preteur")
    _abonne("pp-preteur", statut="paused_limit", reset=_plus_tard(), pret_a=[oid])

    assert P.taille_du_pool(oid, _F) == 0, "il ne peut pas servir maintenant"
    assert P.taille_du_pool_a_la_pose(oid, _F) == 1, "il pourra servir à l'échéance"
    _abonnement.exiger_a_la_pose("pp-preteur", None, _F, org_id=oid)  # ne lève pas


def test_un_pool_dont_les_preteurs_sont_DECONNECTES_refuse_la_pose(live, _option):
    """L'autre moitié : déconnecté n'est pas temporaire, rien ne le résout tout seul.
    Le refus reste — `subscription_pool_empty` veut dire « ça ne tournera jamais »."""
    from oto_mcp.capabilities import _abonnement
    from oto_mcp.capabilities._types import AuthzDenied
    from oto_mcp.db import org_subscription_pool as P
    import pytest as _pytest

    oid = _org("pose-deco", "pd-preteur")
    _abonne("pd-preteur", statut="disconnected", pret_a=[oid])

    assert P.taille_du_pool_a_la_pose(oid, _F) == 0
    with _pytest.raises(AuthzDenied) as e:
        _abonnement.exiger_a_la_pose("pd-preteur", None, _F, org_id=oid)
    assert e.value.code == "subscription_pool_empty"


def test_un_pool_sans_aucun_pret_refuse_toujours_la_pose(live, _option):
    """Et le cas d'origine, inchangé : personne ne prête."""
    from oto_mcp.capabilities import _abonnement
    from oto_mcp.capabilities._types import AuthzDenied
    import pytest as _pytest

    oid = _org("pose-vide", "pv-membre")
    _abonne("pv-membre", statut="connected")  # connecté, mais ne prête à personne

    with _pytest.raises(AuthzDenied) as e:
        _abonnement.exiger_a_la_pose("pv-membre", None, _F, org_id=oid)
    assert e.value.code == "subscription_pool_empty"
