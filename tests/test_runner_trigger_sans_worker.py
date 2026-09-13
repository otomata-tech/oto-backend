"""Ne pas PROMETTRE une exécution que personne n'assure.

Le tick enfile un job à chaque échéance ; l'exécution appartient au worker. Sans
worker armé pour l'org, le job reste `pending` pour toujours — **sans une
erreur**. Le déclencheur, lui, rend un `next_due` que l'agent rapporte comme une
promesse tenue. C'est le pire des deux malentendus : ça ressemble à un succès.

Vécu, et c'est ce qui date ce lot : dans l'org 196, cinq déclencheurs enfilent
chaque matin (relevé du 02/09/2026, dernier enfilement le jour même à 07:00) et
un sixième porte sa propre autopsie **dans son libellé** — « DISABLED 26 Aug,
oto_trigger jobs do not execute ». Quelqu'un a diagnostiqué la panne et n'a eu
que le nom de l'objet pour l'écrire.

Deux familles ici :

1. **la garde suit le VERBE** — poser (et rallumer) est refusé sans runner ;
   lire, corriger, éteindre et supprimer restent ouverts, parce que c'est
   exactement ce dont a besoin quelqu'un qui hérite d'un déclencheur mort ;
2. **le SONDAGE vaut présence** — et c'est le seul signal qui parle avant le
   premier job. Éprouvé en base : un claim sur file VIDE inscrit le worker.
"""
from __future__ import annotations

import os
import uuid

import pytest

from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


@pytest.fixture(autouse=True)
def _cle_de_modele_non_exigee(monkeypatch):
    """Ce fichier ne parle pas de la garde de clé de modèle — elle a son propre banc
    (`test_cle_de_modele_exigee.py`). Le réglage est lu ÉTEINT, comme sur toute
    plateforme qui ne l'a pas allumé : sans cette doublure, la lecture irait
    chercher la vraie base et chaque banc tomberait sur une raison qui n'est pas
    la sienne."""
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **k: None)



def _ctx(sub="alexis", org_id=2):
    return ResolvedCtx(sub=sub, org_id=org_id)


def _appel(ctx, **kw):
    return RT._triggers(ctx, RT.TriggerInput(**kw))


def _arme(monkeypatch, armed=True, workers=1, last_seen="2026-09-02 07:00:00"):
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: {"armed": armed, "workers": workers,
                                     "last_seen": last_seen})


# ── la garde suit le verbe ────────────────────────────────────────────────────

def test_create_est_refuse_quand_aucun_worker_nest_jamais_venu(monkeypatch):
    _arme(monkeypatch, armed=False, workers=0, last_seen=None)
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="create", procedure="veille", cron="5 6 * * *",
               tools=["data_write"])
    assert (e.value.status, e.value.code) == (400, "no_runner_armed")
    # Le message doit dire QUOI FAIRE, pas seulement que c'est refusé.
    assert "jamais sondé" in e.value.message
    assert "pending" in e.value.message


def test_le_refus_distingue_le_silence_recent_de_labsence_totale(monkeypatch):
    """`last_seen=None` et une date ancienne n'appellent pas le même geste :
    monter un runner, ou aller voir pourquoi celui qui existe s'est tu."""
    _arme(monkeypatch, armed=False, workers=0, last_seen="2026-08-30 04:00:00")
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="create", procedure="veille", cron="5 6 * * *",
               tools=["data_write"])
    assert "2026-08-30 04:00:00" in e.value.message
    assert "jamais" not in e.value.message


def test_create_passe_quand_un_worker_sonde(monkeypatch):
    _arme(monkeypatch)
    # ⚠️ Un objet ne porte qu'UN agent : la création lit d'abord ce qui
    # existe. Sans doublure, ce banc irait interroger la vraie base.
    monkeypatch.setattr(RT.db, "triggers_for_procedure", lambda o, p: [])
    monkeypatch.setattr(RT.db, "create_trigger",
                        lambda org, sub, **kw: {"id": 1, **kw})
    out = _appel(_ctx(), op="create", procedure="veille", cron="5 6 * * *",
                 tools=["data_write"])
    assert out["trigger"]["id"] == 1


def test_le_cadencement_est_juge_AVANT_la_presence_du_runner(monkeypatch):
    """Un cron fautif se répare sans quitter l'appel ; une org sans runner
    demande un autre geste. On rend d'abord le refus réparable — sinon poser un
    cron d'arrosage dans une org sans worker cacherait la faute de frappe."""
    _arme(monkeypatch, armed=False, workers=0, last_seen=None)
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="create", procedure="x", cron="* * * * *",
               tools=["a"])
    assert e.value.code == "invalid_schedule"


def test_rallumer_est_refuse_comme_poser(monkeypatch):
    """Rallumer, c'est promettre à nouveau — même geste, même garde."""
    _arme(monkeypatch, armed=False, workers=0, last_seen=None)
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="update", trigger_id=6, enabled=True)
    assert e.value.code == "no_runner_armed"


def test_eteindre_reste_ouvert_sans_runner(monkeypatch):
    """LA garantie du lot : un déclencheur mort doit pouvoir être rangé. Le
    refuser enfermerait l'utilisateur avec l'objet qui lui ment."""
    _arme(monkeypatch, armed=False, workers=0, last_seen=None)
    vu = {}
    monkeypatch.setattr(RT.db, "update_trigger",
                        lambda i, o, champs: vu.update(champs) or {"id": i})
    _appel(_ctx(), op="update", trigger_id=6, enabled=False)
    assert vu == {"enabled": False}


def test_corriger_et_supprimer_restent_ouverts_sans_runner(monkeypatch):
    _arme(monkeypatch, armed=False, workers=0, last_seen=None)
    monkeypatch.setattr(RT.db, "update_trigger", lambda i, o, c: {"id": i, **c})
    _appel(_ctx(), op="update", trigger_id=6, label="mort, à ranger")
    monkeypatch.setattr(RT.db, "delete_trigger", lambda i, o: True)
    assert _appel(_ctx(), op="delete", trigger_id=6) == {"ok": True}


# ── ce que la lecture DIT, pour les déclencheurs déjà posés ───────────────────

def test_list_porte_letat_du_runner(monkeypatch):
    """Le refus protège les NOUVEAUX déclencheurs ; ceux qui existent déjà n'ont
    que cette lecture pour se distinguer d'un déclencheur vivant."""
    _arme(monkeypatch, armed=False, workers=0, last_seen=None)
    monkeypatch.setattr(RT.db, "list_triggers", lambda org: [{"id": 6}])
    # Ce test regarde l'état du RUNNER ; le comptage des pertes est un autre sujet
    # et sa lecture en base n'a pas sa place ici.
    monkeypatch.setattr(RT.db, "comptage_perime", lambda org, tid: {})
    out = _appel(_ctx(), op="list")
    runner = out["runner"]
    assert {k: runner[k] for k in ("armed", "workers", "last_seen")} == {
        "armed": False, "workers": 0, "last_seen": None}
    # Et le catalogue servi avec : sans worker, aucun modèle ne l'est.
    assert runner["families"] == []
    assert runner["models"] and not any(m["served"] for m in runner["models"])


def test_get_porte_letat_du_runner(monkeypatch):
    _arme(monkeypatch)
    monkeypatch.setattr(RT.db, "get_trigger", lambda i, o: {"id": i})
    monkeypatch.setattr(RT.db, "comptage_perime", lambda org, tid: {})
    assert _appel(_ctx(), op="get", trigger_id=6)["runner"]["armed"] is True


def test_le_404_ne_revele_pas_letat_du_runner(monkeypatch):
    """Un déclencheur d'une autre org rend 404 AVANT toute lecture — la garde ne
    doit pas devenir un oracle sur les orgs voisines."""
    _arme(monkeypatch)
    monkeypatch.setattr(RT.db, "get_trigger", lambda i, o: None)
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="get", trigger_id=999)
    assert e.value.code == "trigger_not_found"


# ── le sondage vaut présence — éprouvé en base ────────────────────────────────

@pytest.fixture(scope="module")
def base_bootee(pg_dsn):
    """Une base jetable portant le FRAGMENT `runs` — celui que ce lot modifie.

    ⚠️ Le fragment est joué tel quel, jamais recopié : c'est lui qui doit créer
    `runner_workers`, et un test qui poserait sa propre table prouverait que le
    SQL de lecture marche sans rien dire du DDL SERVI. Ses FK sont toutes
    internes (`runs`, `runner_fleets`), donc il tient debout seul — le boot
    complet demanderait `pgvector`, qui n'apprendrait rien de plus ici et que
    `test_schema_assembly_frozen` + le rejeu de boot couvrent déjà."""
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn
    from oto_mcp.db.schema import runs as fragment_runs

    nom = "oto_worker_vu_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + nom

    url_avant, pool_avant = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        with dbconn._connect() as c:
            c.execute(fragment_runs.RUNS)
        yield dsn
    finally:
        dbconn._pool = pool_avant
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()


def test_un_claim_sur_file_VIDE_inscrit_le_worker(base_bootee):
    """⚠️ Le cas qui justifie la table entière.

    Un claim à vide n'écrit rien d'autre : lu par `runner_jobs.claimed_by`, un
    worker vivant sur une org sans travail serait indistinguable d'une org sans
    runner. Pire, la lecture se BOUCLERAIT — aucun job avant un déclencheur,
    aucun déclencheur sans job."""
    from oto_mcp import db

    assert db.claim_next_job(4242, "worker:solo") is None, "file vide attendue"
    etat = db.runner_arme(4242)
    assert etat["armed"] is True and etat["workers"] == 1
    assert etat["last_seen"] is not None


def test_une_org_jamais_sondee_rend_last_seen_None(base_bootee):
    from oto_mcp import db

    etat = db.runner_arme(4343)
    # `families` (12/09/2026) : les familles de modèles servies — aucune ici.
    assert etat == {"armed": False, "workers": 0, "last_seen": None, "families": []}


def test_un_worker_tu_depuis_trop_longtemps_ne_compte_plus(base_bootee):
    """La fenêtre MORD — sans quoi `armed` resterait vrai à jamais après un seul
    sondage, et la garde ne garderait plus rien."""
    from oto_mcp import db
    from oto_mcp.db import _conn as dbconn

    db.claim_next_job(4444, "worker:parti")
    with dbconn._connect() as c:
        c.execute(
            "UPDATE runner_workers SET last_seen_at = NOW() - make_interval(secs => %s)"
            " WHERE org_id = %s",
            (db.ARME_FENETRE_S + 60, 4444))
    etat = db.runner_arme(4444)
    assert etat["armed"] is False and etat["workers"] == 0
    assert etat["last_seen"] is not None, "il EST venu — le distinguer de jamais"


def test_deux_workers_de_la_meme_org_se_comptent(base_bootee):
    from oto_mcp import db

    db.claim_next_job(4545, "worker:a")
    db.claim_next_job(4545, "worker:b")
    db.claim_next_job(4545, "worker:a")   # re-sondage : pas un second pair
    assert db.runner_arme(4545)["workers"] == 2
