"""Le temps gagné de la page Agents se calcule HORS de la requête (`_temps_gagne_borne`).

Ce que ces bancs tiennent :
- une org légère est servie comme avant (le calcul finit pendant l'attente courte) ;
- un calcul lent ne tient pas la requête : la page sort sans le temps gagné, puis le
  résultat sert depuis le cache ;
- dix ouvertures simultanées ne lancent qu'UN calcul ;
- au plus `CALCULS_SIMULTANES` calculs pour tout le processus ;
- un échec n'est pas relancé à chaque ouverture ;
- chaque requête du calcul porte une borne `statement_timeout`.
"""
from __future__ import annotations

import threading
import time

import pytest

from oto_mcp.capabilities import _temps_gagne_borne as T


@pytest.fixture
def rapide(monkeypatch):
    monkeypatch.setattr(T, "ATTENTE_S", 0.5)


def _attendre(cond, s=2.0):
    fin = time.monotonic() + s
    while time.monotonic() < fin:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_calcul_rapide_servi_dans_la_requete(rapide):
    assert T.formes(1, 28, [5], lambda: ("formes", "sans")) == ("formes", "sans")


def test_calcul_lent_ne_tient_pas_la_requete_puis_sert_du_cache(rapide):
    lache = threading.Event()
    appels = []

    def lent():
        appels.append(1)
        lache.wait(5)
        return "fini"

    debut = time.monotonic()
    assert T.formes(1, 28, [5], lent) is None
    assert time.monotonic() - debut < 1.5
    lache.set()
    assert _attendre(lambda: T.formes(1, 28, [5], lent) == "fini")
    assert len(appels) == 1


def test_un_seul_calcul_par_cle(monkeypatch):
    monkeypatch.setattr(T, "ATTENTE_S", 0.05)
    lache = threading.Event()
    appels = []

    def lent():
        appels.append(1)
        lache.wait(5)
        return "fini"

    for _ in range(10):
        T.formes(1, 28, [5], lent)
    lache.set()
    assert _attendre(lambda: T.formes(1, 28, [5], lent) == "fini")
    assert len(appels) == 1


def test_calculs_bornes_pour_tout_le_processus(monkeypatch):
    monkeypatch.setattr(T, "ATTENTE_S", 0.05)
    lache = threading.Event()
    en_vol = []

    def lent():
        en_vol.append(1)
        lache.wait(5)
        return "fini"

    for org in range(1, 8):
        T.formes(org, 28, [5], lent)
    time.sleep(0.2)
    assert len(en_vol) == T.CALCULS_SIMULTANES
    lache.set()
    # Les autres faisaient la QUEUE : ils passent dès qu'un fil se libère.
    assert _attendre(lambda: len(en_vol) == 7)
    assert _attendre(lambda: all(T.formes(o, 28, [5], lent) == "fini" for o in range(1, 8)))


def test_resultat_perime_servi_sans_attendre(monkeypatch):
    monkeypatch.setattr(T, "ATTENTE_S", 0.5)
    assert T.formes(1, 28, [5], lambda: "ancien") == "ancien"
    monkeypatch.setattr(T, "TTL_S", 0.0)
    monkeypatch.setattr(T, "ATTENTE_S", 5.0)
    lache = threading.Event()
    debut = time.monotonic()
    assert T.formes(1, 28, [5], lambda: lache.wait(5) and "neuf") == "ancien"
    assert time.monotonic() - debut < 1.0
    lache.set()


def test_un_calcul_d_avant_vider_ne_range_rien(monkeypatch):
    monkeypatch.setattr(T, "ATTENTE_S", 0.05)
    lache = threading.Event()
    T.formes(1, 28, [5], lambda: lache.wait(5) and "vieux")
    T.vider()
    lache.set()
    time.sleep(0.2)
    assert T._cache == {}


def test_un_echec_n_est_pas_relance_a_chaque_ouverture(rapide):
    appels = []

    def casse():
        appels.append(1)
        raise RuntimeError("borne atteinte")

    assert T.formes(1, 28, [5], casse) is None
    assert _attendre(lambda: not T._en_vol)
    assert T.formes(1, 28, [5], casse) is None
    assert len(appels) == 1


def test_le_calcul_porte_une_borne_sql(rapide):
    from oto_mcp.db import _conn
    vu = []
    T.formes(1, 28, [5], lambda: vu.append(_conn._duree_bornee_ms.get()) or "ok")
    assert vu == [T.DUREE_MAX_MS]


def test_la_file_est_bornee(monkeypatch):
    """Au-delà de `EN_VOL_MAX` clés en vol ou en queue, aucune n'est ajoutée : la page
    sort sans le temps gagné, et l'ouverture suivante retente."""
    monkeypatch.setattr(T, "ATTENTE_S", 0.0)
    monkeypatch.setattr(T, "EN_VOL_MAX", 3)
    lache = threading.Event()
    lances = []

    def lent():
        lances.append(1)
        lache.wait(5)
        return "fini"

    for org in range(1, 7):
        assert T.formes(org, 28, [5], lent) is None
    assert len(T._en_vol) == 3
    lache.set()
    assert _attendre(lambda: not T._en_vol)
    assert len(lances) == 3
    assert _attendre(lambda: T.formes(6, 28, [5], lent) == "fini")


def test_les_calculs_tournent_dans_des_fils_demons(rapide):
    """Un fil non démon retiendrait l'arrêt du processus jusqu'à la fin de chaque
    calcul en queue."""
    vu = []
    T.formes(1, 28, [5], lambda: vu.append(threading.current_thread().daemon) or "ok")
    assert vu == [True]


def test_la_memoire_des_echecs_est_bornee(monkeypatch):
    monkeypatch.setattr(T, "ATTENTE_S", 0.0)
    monkeypatch.setattr(T, "_TAILLE_MAX", 2)

    def casse():
        raise RuntimeError("borne atteinte")

    for org in range(1, 6):
        T.formes(org, 28, [5], casse)
        assert _attendre(lambda: not T._en_vol)
    assert len(T._echecs) == 2
