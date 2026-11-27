"""Une boucle de fond ne tourne que dans un processus à la fois — `db/verrou_de_boucle.py`.

Deux processus du serveur sur la même base (bleu/vert pendant la vidange, plusieurs
processus pour une instance) composent les mêmes boucles. Le scheduler d'emails réclame
ses lignes sans les sortir de `pending` : sans verrou, le second processus reprend le même
email pendant que le premier l'envoie, et le destinataire le reçoit deux fois.

Les épreuves de base tournent sur une VRAIE base (`live`) : le verrou vit dans le serveur
PostgreSQL, un double ne prouverait rien. La doublure est l'ENVOI, compté par email."""
from __future__ import annotations

import inspect
import logging
import threading
from collections import Counter

import pytest

from oto_mcp import boucles_de_fond, scheduler
from oto_mcp.db import verrou_de_boucle

psycopg = pytest.importorskip("psycopg")

# Les boucles tierces que protège une réservation LIGNE À LIGNE qui sort le travail de la
# file (l'état change dans le même UPDATE que le claim) : le verrou global n'y ajoute rien.
_RESERVEES = {
    # `claim_next_transcription_job` : pending → running, FOR UPDATE SKIP LOCKED.
    "transcription_worker",
    # `claim_next_jev_job` : bail en base (`BAIL_S`), FOR UPDATE SKIP LOCKED.
    "jev_jobs_worker",
    # `db.youtube_uploads.reclamer` : bail en base (`BAIL_S`), FOR UPDATE SKIP LOCKED.
    "youtube_upload_worker",
}


# ══ 1. le registre : chaque boucle tierce a SA protection ═══════════════════

def _source_du_module(b: boucles_de_fond.Boucle) -> str:
    return inspect.getsource(inspect.getmodule(b.fonction()))


def test_chaque_boucle_tierce_est_verrouillee_ou_reservee():
    for b in boucles_de_fond.BOUCLES:
        if not b.tiers or b.nom in _RESERVEES:
            continue
        src = _source_du_module(b)
        assert f'"{b.nom}"' in src and "verrou_de_boucle.sous_verrou" in src, (
            f"boucle tierce {b.nom!r} : ni verrou de boucle, ni réservation déclarée — "
            "deux processus sur la même base agiraient deux fois")


def test_une_boucle_reservee_l_est_vraiment():
    # La liste ci-dessus n'est pas une dispense : chaque nom doit rester une boucle
    # tierce, sinon on dispenserait un fantôme.
    tierces = {b.nom for b in boucles_de_fond.BOUCLES if b.tiers}
    assert _RESERVEES <= tierces


def test_les_cles_sont_distinctes_et_figees():
    noms = [b.nom for b in boucles_de_fond.BOUCLES]
    cles = [verrou_de_boucle.cle(n) for n in noms]
    assert len(set(cles)) == len(cles)
    assert all(-(1 << 31) <= c < (1 << 31) for c in cles)
    # Figée d'un processus à l'autre (crc32, pas `hash()`) : deux versions du serveur
    # en bleu/vert doivent prendre LA MÊME clé.
    assert verrou_de_boucle.cle("scheduler") == verrou_de_boucle.cle("scheduler")
    assert verrou_de_boucle.cle("scheduler") == 1178397720


def test_une_base_injoignable_leve(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@127.0.0.1:9/z?connect_timeout=1")
    with pytest.raises(psycopg.OperationalError):
        verrou_de_boucle.sous_verrou("scheduler", lambda: 1, si_saute=0)


# ══ 2. sur une vraie base ═══════════════════════════════════════════════════

@pytest.fixture
def file_d_envoi(live, monkeypatch):
    """Une file d'emails dus, vidée à chaque épreuve, et un envoi doublé qui compte."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("DELETE FROM scheduled_emails")
    envois: Counter = Counter()
    etat = {"pendant": None}

    def envoyer(to, subject, html, **_):
        envois[subject] += 1
        if etat["pendant"] is not None:
            rappel, etat["pendant"] = etat["pendant"], None
            rappel()
        return True

    monkeypatch.setattr(scheduler.email, "_send", envoyer)
    verrou_de_boucle._SAUTEES_DITES.clear()
    return {"envois": envois, "etat": etat}


def _programmer(n: int) -> list[str]:
    from oto_mcp.db._conn import _connect
    sujets = [f"message {i}" for i in range(n)]
    with _connect() as conn:
        for s in sujets:
            conn.execute(
                "INSERT INTO scheduled_emails (to_email, subject, body_html, from_email, "
                "transport, scheduled_at) VALUES ('a@exemple.test', %s, '<p>x</p>', "
                "'b@exemple.test', 'mailer', NOW() - INTERVAL '1 minute')", (s,))
    return sujets


def _statuts() -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        rows = conn.execute("SELECT subject, status, attempts FROM scheduled_emails").fetchall()
    return {r["subject"]: (r["status"], r["attempts"]) for r in rows}


def _tour_du_scheduler() -> int:
    return verrou_de_boucle.sous_verrou("scheduler", scheduler._process_due_batch,
                                        si_saute=0)


def test_sans_verrou_le_second_processus_renvoie(file_d_envoi):
    # La preuve que l'épreuve d'après MORD : sans verrou, un second tour lancé pendant
    # que le premier envoie reprend les mêmes lignes (toujours `pending`).
    sujets = _programmer(3)
    second = {}

    def pendant():
        t = threading.Thread(target=lambda: second.update(n=scheduler._process_due_batch()))
        t.start()
        t.join(30)

    file_d_envoi["etat"]["pendant"] = pendant
    scheduler._process_due_batch()
    assert second["n"] >= 2
    assert any(file_d_envoi["envois"][s] > 1 for s in sujets)


def test_deux_boucles_concurrentes_un_seul_envoi(file_d_envoi):
    # Le premier tour tient le verrou ; au milieu de son premier envoi, un second tour
    # part dans un AUTRE thread, sur sa propre connexion — l'autre processus.
    sujets = _programmer(3)
    second = {}

    def pendant():
        t = threading.Thread(target=lambda: second.update(n=_tour_du_scheduler()))
        t.start()
        t.join(30)

    file_d_envoi["etat"]["pendant"] = pendant
    assert _tour_du_scheduler() == 3
    assert second == {"n": 0}
    assert file_d_envoi["envois"] == Counter({s: 1 for s in sujets})
    assert {s: st for s, (st, _) in _statuts().items()} == {s: "sent" for s in sujets}


def test_deux_boucles_en_course_libre_un_seul_envoi(file_d_envoi):
    # Course libre : deux threads partent ensemble et rejouent leur tour, comme deux
    # schedulers qui se chevauchent. Chaque email part une fois, quel que soit l'ordre.
    sujets = _programmer(20)
    depart = threading.Barrier(2)
    erreurs = []

    def boucle():
        try:
            depart.wait(10)
            for _ in range(5):
                _tour_du_scheduler()
        except Exception as e:  # noqa: BLE001 — rapporté par l'assertion ci-dessous
            erreurs.append(e)

    fils = [threading.Thread(target=boucle) for _ in range(2)]
    for f in fils:
        f.start()
    for f in fils:
        f.join(60)
    assert erreurs == []
    assert file_d_envoi["envois"] == Counter({s: 1 for s in sujets})


def test_verrou_pris_ailleurs_tour_saute_journal_rien_d_envoye(file_d_envoi, live,
                                                              pg_module_dsn, caplog):
    sujets = _programmer(2)
    with psycopg.connect(pg_module_dsn, autocommit=True) as autre:
        autre.execute("SELECT pg_advisory_lock(%s, %s)",
                      (verrou_de_boucle._FAMILLE, verrou_de_boucle.cle("scheduler")))
        with caplog.at_level(logging.DEBUG, logger="oto_mcp.db.verrou_de_boucle"):
            assert _tour_du_scheduler() == 0
            assert _tour_du_scheduler() == 0
    assert file_d_envoi["envois"] == Counter()
    # Rien n'a été réclamé : la ligne n'a pas pris de tentative.
    assert _statuts() == {s: ("pending", 0) for s in sujets}
    sautes = [r for r in caplog.records
              if "boucle scheduler : tour sauté, un autre processus la tient" in r.getMessage()]
    # Dit en INFO une fois, puis en DEBUG tant que ça dure.
    assert [r.levelno for r in sautes] == [logging.INFO, logging.DEBUG]
    # L'autre processus a rendu la main (connexion fermée) : le tour suivant joue.
    assert _tour_du_scheduler() == 2
    assert file_d_envoi["envois"] == Counter({s: 1 for s in sujets})


def _libre(dsn: str, nom: str) -> bool:
    """Un autre processus peut-il prendre la boucle ? (rendue aussitôt)."""
    with psycopg.connect(dsn, autocommit=True) as c:
        cle = (verrou_de_boucle._FAMILLE, verrou_de_boucle.cle(nom))
        pris = c.execute("SELECT pg_try_advisory_lock(%s, %s) AS t", cle).fetchone()[0]
        if pris:
            c.execute("SELECT pg_advisory_unlock(%s, %s)", cle)
        return pris


def test_le_verrou_est_relache_apres_le_tour_meme_sur_exception(live, pg_module_dsn):
    vu_pendant = {}

    def tour_qui_casse():
        vu_pendant["libre"] = _libre(pg_module_dsn, "essai")
        raise RuntimeError("le tour casse")

    with pytest.raises(RuntimeError, match="le tour casse"):
        verrou_de_boucle.sous_verrou("essai", tour_qui_casse, si_saute=None)
    assert vu_pendant == {"libre": False}      # tenu pendant le tour…
    assert _libre(pg_module_dsn, "essai")      # …rendu après, malgré l'exception

    assert verrou_de_boucle.sous_verrou("essai", lambda: "fait", si_saute=None) == "fait"
    assert _libre(pg_module_dsn, "essai")


def test_aucun_verrou_ne_reste_tenu_apres_le_tour(live, pg_module_dsn):
    def verrous() -> int:
        with psycopg.connect(pg_module_dsn, autocommit=True) as c:
            return c.execute(
                "SELECT COUNT(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND classid = %s::oid AND objsubid = 2", (verrou_de_boucle._FAMILLE,)).fetchone()[0]

    with pytest.raises(ValueError):
        verrou_de_boucle.sous_verrou("essai", lambda: (_ for _ in ()).throw(ValueError()),
                                     si_saute=None)
    verrou_de_boucle.sous_verrou("essai", lambda: None, si_saute=None)
    assert verrous() == 0
