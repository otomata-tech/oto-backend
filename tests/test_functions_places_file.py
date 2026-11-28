"""La file des places du bac à sable (`functions/places.py`, 28/11/2026).

Une demande ATTEND une place, bornée (`OTO_FUNCTIONS_ATTENTE_MAX_S`), au lieu d'un refus
immédiat ; la place libérée va au compte qui en tient le MOINS, puis au plus ancien ; une
demande qui renonce (délai, annulation) ne laisse ni place ni entrée de file. Deux sortes
d'appelants partagent la file : un fil (`place`, l'aller simple) et la boucle
(`place_async`, le pont des recettes-code), qui n'est jamais bloquée.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from oto_mcp.functions import places


@pytest.fixture(autouse=True)
def places_neuves(monkeypatch):
    for env in (places.ENV_MAX, places.ENV_RESERVE, places.ENV_ATTENTE):
        monkeypatch.delenv(env, raising=False)
    _vider()
    assert places.en_cours() == 0 and places.en_attente() == 0
    yield
    _vider()
    assert places.en_cours() == 0, "une place n'a pas été rendue"
    assert places.en_attente() == 0, "une demande est restée dans la file"


def _vider() -> None:
    for f in (places.plafond, places.reserve, places.attente_max_s):
        f.cache_clear()


def _regler(monkeypatch, plafond: int, reserve: int = 1, attente: float = 5.0) -> None:
    monkeypatch.setenv(places.ENV_MAX, str(plafond))
    monkeypatch.setenv(places.ENV_RESERVE, str(reserve))
    monkeypatch.setenv(places.ENV_ATTENTE, str(attente))
    _vider()


def _attendre(condition, delai: float = 5.0) -> None:
    fin = time.monotonic() + delai
    while not condition():
        if time.monotonic() > fin:
            pytest.fail("la condition attendue n'est jamais venue")
        time.sleep(0.005)


def test_les_defauts():
    assert places.attente_max_s() == places.ATTENTE_MAX_DEFAUT == 25.0
    assert places.reserve() == places.RESERVE_DEFAUT == 1
    assert places.par_compte() == places.plafond() - 1


def test_la_reserve_laisse_au_moins_une_place_par_compte(monkeypatch):
    _regler(monkeypatch, plafond=1, reserve=1)
    assert places.par_compte() == 1


# ── Un fil attend son tour ────────────────────────────────────────────────────

def test_un_fil_attend_puis_entre_quand_la_place_se_libere(monkeypatch):
    _regler(monkeypatch, plafond=1)
    entre = threading.Event()

    def b():
        with places.place("b"):
            entre.set()

    with places.place("a"):
        fil = threading.Thread(target=b)
        fil.start()
        _attendre(lambda: places.en_attente() == 1)
        assert not entre.is_set(), "b est entré pendant que a tenait l'unique place"
    fil.join(5)
    assert entre.is_set(), "b n'est pas entré une fois la place rendue"


def test_au_dela_de_l_attente_le_refus_annonce_la_borne(monkeypatch):
    _regler(monkeypatch, plafond=1, attente=0.2)
    with places.place("a", 50):
        debut = time.monotonic()
        with pytest.raises(places.BacASableOccupe) as e:
            with places.place("b"):
                pytest.fail("b est entré sous un plafond tenu")
        attendu = time.monotonic() - debut
        assert 0.15 <= attendu < 2, f"l'attente n'a pas tenu sa borne ({attendu:.2f} s)"
        assert places.en_attente() == 0, "le refus a laissé b dans la file"
    assert 48 <= e.value.retry_after <= 50
    assert "server-wide limit" in str(e.value)


def test_un_nouvel_arrivant_ne_passe_pas_devant_qui_attend(monkeypatch):
    _regler(monkeypatch, plafond=1)
    ordre: list[str] = []
    libere = threading.Event()

    def demandeur(sub):
        with places.place(sub):
            ordre.append(sub)
            libere.wait(5)

    with places.place("a"):
        fb = threading.Thread(target=demandeur, args=("b",))
        fb.start()
        _attendre(lambda: places.en_attente() == 1)
        fc = threading.Thread(target=demandeur, args=("c",))
        fc.start()
        _attendre(lambda: places.en_attente() == 2)
    _attendre(lambda: ordre == ["b"])
    libere.set()
    fb.join(5)
    fc.join(5)
    assert ordre == ["b", "c"]


# ── La part équitable ─────────────────────────────────────────────────────────

def test_la_place_liberee_va_au_compte_qui_en_tient_le_moins(monkeypatch):
    """a tient les deux places et redemande AVANT b ; une place se libère : elle va à b
    (qui n'en tient aucune), pas à a (qui en tient encore une), malgré son rang."""
    _regler(monkeypatch, plafond=2, reserve=0)
    servis: list[str] = []
    fin = threading.Event()

    def demandeur(sub):
        with places.place(sub):
            servis.append(sub)
            fin.wait(5)

    premiere = places.place("a")
    premiere.__enter__()
    with places.place("a"):
        fa = threading.Thread(target=demandeur, args=("a",))
        fa.start()
        _attendre(lambda: places.en_attente() == 1)
        fb = threading.Thread(target=demandeur, args=("b",))
        fb.start()
        _attendre(lambda: places.en_attente() == 2)
        premiere.__exit__(None, None, None)
        _attendre(lambda: servis == ["b"])
        assert places.en_attente() == 1, "a aurait dû rester en file"
    # La seconde place de a rendue : sa demande entre à son tour.
    _attendre(lambda: servis == ["b", "a"])
    fin.set()
    fa.join(5)
    fb.join(5)


def test_un_compte_a_sa_limite_passe_son_tour(monkeypatch):
    """Plafond 3, réserve 1 : a tient ses deux places ; une troisième place est libre
    mais a ne peut pas la prendre — elle reste à b, qui arrive après."""
    _regler(monkeypatch, plafond=3, reserve=1, attente=0.2)
    with places.place("a"), places.place("a"):
        with pytest.raises(places.BacASableOccupe, match="per-person-per-org limit"):
            with places.place("a"):
                pytest.fail("a a pris la place réservée aux autres")
        with places.place("b"):
            assert places.en_cours() == 3


# ── La boucle attend sans être bloquée ────────────────────────────────────────

def test_le_pont_attend_dans_la_boucle_sans_la_bloquer(monkeypatch):
    _regler(monkeypatch, plafond=1)
    tenue = places.place("a")
    tenue.__enter__()

    async def scenario():
        tic = 0

        async def horloge():
            nonlocal tic
            while True:
                await asyncio.sleep(0.01)
                tic += 1

        h = asyncio.ensure_future(horloge())

        async def b():
            async with places.place_async("b"):
                return "entré"

        tache = asyncio.ensure_future(b())
        while places.en_attente() == 0:
            await asyncio.sleep(0.005)
        avant = tic
        await asyncio.sleep(0.1)
        assert tic > avant + 3, "la boucle s'est figée pendant l'attente d'une place"
        # La place est rendue depuis un autre fil, comme le fait l'aller simple.
        await asyncio.to_thread(tenue.__exit__, None, None, None)
        resultat = await asyncio.wait_for(tache, 5)
        h.cancel()
        return resultat

    assert asyncio.run(scenario()) == "entré"


def test_le_pont_refuse_apres_son_attente(monkeypatch):
    _regler(monkeypatch, plafond=1, attente=0.2)

    async def scenario():
        with places.place("a", 30):
            with pytest.raises(places.BacASableOccupe) as e:
                async with places.place_async("b"):
                    pytest.fail("b est entré sous un plafond tenu")
            return e.value.retry_after

    assert 28 <= asyncio.run(scenario()) <= 30


def test_une_annulation_pendant_l_attente_ne_laisse_rien(monkeypatch):
    _regler(monkeypatch, plafond=1)

    async def scenario():
        with places.place("a"):
            async def b():
                async with places.place_async("b"):
                    pytest.fail("b est entré")

            tache = asyncio.ensure_future(b())
            while places.en_attente() == 0:
                await asyncio.sleep(0.005)
            tache.cancel()
            with pytest.raises(asyncio.CancelledError):
                await tache
            assert places.en_attente() == 0, "la demande annulée est restée en file"

    asyncio.run(scenario())


def test_une_place_attribuee_a_une_tache_annulee_est_rendue(monkeypatch):
    """La place arrive à b au moment même où b est annulé : elle n'est pas perdue, elle
    est rendue (et irait au suivant)."""
    _regler(monkeypatch, plafond=1)

    async def scenario():
        tenue = places.place("a")
        tenue.__enter__()

        async def b():
            async with places.place_async("b"):
                await asyncio.sleep(10)

        tache = asyncio.ensure_future(b())
        while places.en_attente() == 0:
            await asyncio.sleep(0.005)
        tenue.__exit__(None, None, None)   # attribue la place à b…
        tache.cancel()                      # …qui est annulé avant d'avoir repris
        with pytest.raises(asyncio.CancelledError):
            await tache
        assert places.en_cours() == 0, "la place attribuée à la tâche annulée a fui"

    asyncio.run(scenario())


def test_fils_et_boucle_partagent_la_meme_file(monkeypatch):
    """Un fil qui attend et une tâche qui attend : servis dans leur ordre d'arrivée."""
    _regler(monkeypatch, plafond=1)
    ordre: list[str] = []

    async def scenario():
        tenue = places.place("a")
        tenue.__enter__()

        def fil_b():
            with places.place("b"):
                ordre.append("b")

        fb = threading.Thread(target=fil_b)
        fb.start()
        while places.en_attente() == 0:
            await asyncio.sleep(0.005)

        async def c():
            async with places.place_async("c"):
                ordre.append("c")

        tc = asyncio.ensure_future(c())
        while places.en_attente() < 2:
            await asyncio.sleep(0.005)
        await asyncio.to_thread(tenue.__exit__, None, None, None)
        await asyncio.wait_for(tc, 5)
        await asyncio.to_thread(fb.join, 5)

    asyncio.run(scenario())
    assert ordre == ["b", "c"]


def test_la_borne_de_dernier_recours_des_recettes_couvre_l_attente(monkeypatch):
    """La file passe avant le lancement : la borne d'une recette-code y ajoute l'attente
    maximale, sans quoi une recette courte qui attendait finissait en `recipe_timeout`."""
    from oto_mcp.capabilities import recipes as cap_recettes
    from oto_mcp.functions import pont
    corps = {"limits": {"max_seconds": 10}}
    monkeypatch.setenv(places.ENV_ATTENTE, "25")
    places.attente_max_s.cache_clear()
    avec = cap_recettes._borne_s(corps)
    monkeypatch.setenv(places.ENV_ATTENTE, "0")
    places.attente_max_s.cache_clear()
    assert avec - cap_recettes._borne_s(corps) == 25
    assert cap_recettes._borne_s(corps) == 10 + pont.GRACE_S + cap_recettes._MARGE_S


# ── Le compte = une personne DANS une org, `(sub, org_id)` (décision du 28/11/2026) ─────


def test_une_personne_a_sa_limite_dans_l_org_a_prend_une_place_dans_l_org_b(monkeypatch):
    """Une personne membre de deux orgs n'est pas bornée d'une org à l'autre."""
    _regler(monkeypatch, plafond=4, reserve=1, attente=0)
    with places.place("u", org_id=1), places.place("u", org_id=1), places.place("u", org_id=1):
        # Sa part dans l'org 1 est pleine (3 = 4 − 1) …
        with pytest.raises(places.BacASableOccupe, match="this person"):
            with places.place("u", org_id=1):
                pass
        # … mais l'org 2 est un autre compte : la dernière place lui revient.
        with places.place("u", org_id=2):
            assert places.en_cours() == 4


def test_deux_personnes_d_une_meme_org_ont_chacune_leur_part(monkeypatch):
    _regler(monkeypatch, plafond=3, reserve=1, attente=0)
    with places.place("a", org_id=7), places.place("a", org_id=7):
        with places.place("b", org_id=7):
            assert places.en_cours() == 3


def test_sans_org_est_un_compte_a_part(monkeypatch):
    _regler(monkeypatch, plafond=2, reserve=1, attente=0)
    with places.place("u", org_id=5):
        with places.place("u"):  # org None : (u, None), distinct de (u, 5)
            assert places.en_cours() == 2
    assert places.compte("u", "5") == ("u", 5)


def test_la_part_equitable_compte_par_personne_et_par_org(monkeypatch):
    """La place libérée va au compte (sub, org) qui en tient le moins : la même personne
    dans une AUTRE org passe devant elle-même dans l'org où elle en tient déjà."""
    _regler(monkeypatch, plafond=2, reserve=0, attente=5)
    servis: list[str] = []
    liberer = [threading.Event(), threading.Event()]

    def occupant(i):
        with places.place("u", org_id=1):
            liberer[i].wait(5)

    occupants = [threading.Thread(target=occupant, args=(i,)) for i in (0, 1)]
    for t in occupants:
        t.start()
    _attendre(lambda: places.en_cours() == 2)

    def demandeur(nom, org):
        with places.place("u", org_id=org):
            servis.append(nom)

    # Arrivée d'abord : la même personne dans l'org 1 (qui y tient déjà 2 places)…
    d1 = threading.Thread(target=demandeur, args=("org1", 1))
    d1.start()
    _attendre(lambda: places.en_attente() == 1)
    # … puis dans l'org 2 (0 place tenue) : elle passe devant.
    d2 = threading.Thread(target=demandeur, args=("org2", 2))
    d2.start()
    _attendre(lambda: places.en_attente() == 2)
    liberer[0].set()                      # UNE place se libère : elle va à l'org 2
    _attendre(lambda: len(servis) >= 1)   # (l'org 2 rend aussitôt la sienne à l'org 1)
    liberer[1].set()
    for t in (*occupants, d1, d2):
        t.join(5)
    assert servis == ["org2", "org1"]
