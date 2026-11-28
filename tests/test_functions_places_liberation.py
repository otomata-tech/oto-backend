"""Une place du bac à sable suit l'EXÉCUTION, pas les appels d'outils qu'elle laisse (25/11/2026).

Des refus `sandbox_busy` en série, malgré des relances qui attendent leur `Retry-After`,
faisaient soupçonner une place jamais rendue : un appel d'outil abandonné à l'échéance
(son fil, que Python n'interrompt pas, continue) tiendrait la place de l'exécution.

Ces bancs jouent le VRAI `pont._executer_avec_pont` (un faux bac à sable, de vrais tubes,
de vraies tâches) et tranchent :

- la place est prise par la coroutine qui joue l'exécution, et rendue quand elle sort,
  processus du bac à sable tué ; un appel d'outil abandonné n'en tient pas — il vole
  hors place, jusqu'à la fin de son fil ;
- un second passage du MÊME compte est admis aussitôt, fil abandonné encore vivant ;
- une annulation (client parti) rend la place de même ;
- le pont se borne lui-même : un bac à sable qui ne lit pas son entrée ne tient pas la
  place au-delà de l'échéance de son budget ;
- au-dessus, la borne de dernier recours des recettes (`recipe_timeout`) laisse une place
  qui se rend dans la foulée.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

import pytest

from oto_mcp.capabilities import recipes as cap_recettes
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.functions import executor, isolement, places, pont


@pytest.fixture(autouse=True)
def _refus_immediat(monkeypatch):
    """Ces bancs éprouvent la libération et le `Retry-After` : sans attente (0), comme
    avant le 28/11/2026 ; l'attente a ses bancs (`test_functions_places_file.py`)."""
    monkeypatch.setenv(places.ENV_ATTENTE, "0")
    monkeypatch.delenv(places.ENV_RESERVE, raising=False)
    for f in (places.plafond, places.reserve, places.attente_max_s):
        f.cache_clear()
    yield
    for f in (places.plafond, places.reserve, places.attente_max_s):
        f.cache_clear()


def _un_par_compte(monkeypatch) -> None:
    monkeypatch.setenv(places.ENV_RESERVE, str(places.MAX_DEFAUT - 1))
    places.reserve.cache_clear()

# Le faux bac à sable : il lit sa tâche, lance ses demandes (`job.input.calls`), attend
# autant de réponses, puis finit.
_FAUX_BAC = r'''
import json, sys
tache = json.loads(sys.stdin.readline())
nonce, plan = tache["nonce"], tache["job"]["input"]
def ecrire(o):
    sys.stdout.write(nonce + json.dumps(o) + "\n")
    sys.stdout.flush()
for i, args in enumerate(plan.get("calls", []), 1):
    ecrire({"type": "call", "id": i, "tool": "acme_search", "args": args})
recues = [json.loads(sys.stdin.readline()) for _ in plan.get("calls", [])]
ecrire({"type": "done", "ok": True, "sortie": {"ok": True, "result": recues},
        "journal": ""})
'''

# Un bac à sable vivant qui ne lit JAMAIS son entrée : la tâche ne passe pas le tube.
_SOURD = "import time\ntime.sleep(30)\n"


def _poser(monkeypatch, tmp_path, source: str) -> None:
    script = tmp_path / "bac.py"
    script.write_text(source, encoding="utf-8")
    monkeypatch.setattr(executor, "repertoire", lambda: tmp_path)
    monkeypatch.setattr(executor, "roues", lambda racine, requirements: [])
    monkeypatch.setattr(executor, "chemin_pyodide", lambda racine: tmp_path)
    monkeypatch.setattr(executor, "lancement", lambda racine, s, duree: isolement.Lancement(
        argv=[sys.executable, "-I", str(script)], env=dict(os.environ), cwd=str(tmp_path)))


@pytest.fixture(autouse=True)
def places_neuves(monkeypatch):
    monkeypatch.delenv(places.ENV_MAX, raising=False)
    places.plafond.cache_clear()
    monkeypatch.setattr(pont, "GRACE_S", 0.5)
    assert places.en_cours() == 0, "un banc précédent a gardé une place"
    yield
    places.plafond.cache_clear()
    assert places.en_cours() == 0, "une place n'a pas été rendue"
    assert not places._tenues, "une place rendue garde sa tenue"


@pytest.fixture()
def faux_bac(tmp_path, monkeypatch):
    _poser(monkeypatch, tmp_path, _FAUX_BAC)


def _outil_bloque(libere: threading.Event, parti: threading.Event):
    """Un outil `def` qui bloque son fil (que Python n'interrompt pas) jusqu'à `libere`."""
    async def appeler(outil, arguments):
        import anyio

        def bloquer():
            parti.set()
            libere.wait(30)
        await anyio.to_thread.run_sync(bloquer)
        return {"ok": True, "result": "trop tard"}
    return appeler


async def _rien(outil, arguments):
    return {"ok": True, "result": None}


def _executer(appeler, calls, sub="u", **budget):
    return pont.executer_avec_pont(
        sources={"r.py": ""}, entrypoint="r:run", appeler=appeler, sub=sub,
        budget=pont.Budget(**budget), entree={"calls": calls})


def test_un_outil_abandonne_a_l_echeance_ne_garde_pas_la_place(faux_bac):
    """L'outil bloque son fil bien au-delà du budget : à l'échéance la place est rendue,
    le fil abandonné vit encore, et le même compte repasse tout de suite."""
    libere, parti = threading.Event(), threading.Event()

    async def scenario():
        try:
            debut = time.monotonic()
            sortie = await _executer(_outil_bloque(libere, parti), [{"n": 1}], delai_s=1.0)
            duree = time.monotonic() - debut
            assert sortie["stopped"] == "time_budget"
            assert parti.is_set() and not libere.is_set(), "le fil devait voler encore"
            assert places.en_cours() == 0, "le fil abandonné tient la place"
            # Le même compte, fil abandonné toujours vivant : admis.
            second = await _executer(_rien, [{"n": 2}], delai_s=5.0)
            assert second["ok"] and second["calls"] == 1
            return duree
        finally:
            libere.set()

    duree = asyncio.run(scenario())
    # L'échéance, la grâce du code puis celle de la fin, et l'arrêt du processus.
    assert duree < 1.0 + 2 * pont.GRACE_S + 2


def test_une_annulation_pendant_un_appel_rend_la_place(faux_bac):
    """Le client part (requête annulée) pendant qu'un outil bloque son fil : la place
    revient dans la grâce, sans attendre le fil."""
    libere, parti = threading.Event(), threading.Event()

    async def scenario():
        try:
            tache = asyncio.ensure_future(
                _executer(_outil_bloque(libere, parti), [{"n": 1}], delai_s=30.0))
            while not parti.is_set():
                await asyncio.sleep(0.01)
            assert places.en_cours() == 1
            debut = time.monotonic()
            tache.cancel()
            with pytest.raises(asyncio.CancelledError):
                await tache
            rendue_en = time.monotonic() - debut
            assert not libere.is_set() and places.en_cours() == 0
            second = await _executer(_rien, [], delai_s=5.0)
            assert second["ok"]
            return rendue_en
        finally:
            libere.set()

    assert asyncio.run(scenario()) < pont.GRACE_S + 2


def test_un_bac_qui_ne_lit_pas_son_entree_ne_tient_pas_la_place_au_dela_du_budget(
        tmp_path, monkeypatch):
    """La tâche dépasse le tube et le processus ne la lit jamais : l'envoi attendait sans
    échéance (sous `bwrap`, rien ne tue un processus qui dort). Le pont lève à
    l'échéance de son budget et rend la place."""
    _poser(monkeypatch, tmp_path, _SOURD)
    harnais = tmp_path / "harnais_lourd"
    harnais.write_text("#" * (2 * 1024 * 1024), encoding="utf-8")
    monkeypatch.setattr(pont, "_HARNAIS", harnais)

    async def scenario():
        debut = time.monotonic()
        with pytest.raises(executor.ExecutionEchouee, match="délai"):
            await asyncio.wait_for(_executer(_rien, [], delai_s=1.0), 20)
        return time.monotonic() - debut

    duree = asyncio.run(scenario())
    assert places.en_cours() == 0
    assert duree < 1.0 + 2 * pont.GRACE_S + 3


def test_la_borne_de_dernier_recours_des_recettes_rend_la_place_dans_la_foulee(
        tmp_path, monkeypatch):
    """Au-dessus du pont, la recette abandonne son exécution (`recipe_timeout`) sans
    l'attendre ; la place, tenue par l'exécution, se rend dès que sa fin a tué le
    processus — le même compte repasse."""
    from oto_mcp import outils_du_travail
    _poser(monkeypatch, tmp_path, _SOURD)
    harnais = tmp_path / "harnais_lourd"
    harnais.write_text("#" * (2 * 1024 * 1024), encoding="utf-8")
    monkeypatch.setattr(pont, "_HARNAIS", harnais)
    monkeypatch.setattr(outils_du_travail, "manquants", lambda cites: None)
    monkeypatch.setattr(cap_recettes, "_instance", lambda: object())
    # La borne de dernier recours échoit AVANT celle du pont (30 s) : l'exécution est
    # abandonnée en vol, l'envoi de sa tâche bloqué sur un tube plein.
    monkeypatch.setattr(cap_recettes, "_borne_s", lambda corps: 0.5)
    corps = {"kind": "code", "sources": {"r.py": "async def run(oto, p):\n    return 1\n"},
             "entrypoint": "r:run", "params": {}, "writes": [],
             "limits": {"max_units": 10, "max_seconds": 30, "max_writes": 0}}

    async def scenario():
        with pytest.raises(AuthzDenied) as e:
            await cap_recettes._executer(ResolvedCtx(sub="u"),
                                         cap_recettes.RecipeInput(op="test"), corps,
                                         ecrire=False)
        assert e.value.code == "recipe_timeout"
        debut = time.monotonic()
        while places.en_cours() and time.monotonic() - debut < 5:
            await asyncio.sleep(0.01)
        assert places.en_cours() == 0, "l'exécution abandonnée garde la place"
        return time.monotonic() - debut

    assert asyncio.run(scenario()) < pont.GRACE_S + 2


# ── Le `Retry-After` dit quand une place se libère au plus tard ───────────────

def _plafond(monkeypatch, n: int) -> None:
    monkeypatch.setenv(places.ENV_MAX, str(n))
    places.plafond.cache_clear()


def test_le_refus_par_compte_annonce_ce_qui_reste_a_son_execution(monkeypatch):
    _un_par_compte(monkeypatch)
    with places.place("a", 100):
        with pytest.raises(places.BacASableOccupe) as e:
            with places.place("a", 100):
                pytest.fail("un compte a tenu deux exécutions à la fois")
    assert 99 <= e.value.retry_after <= 100
    message = str(e.value)
    assert message.startswith("sandbox_busy: this person already has 1 execution(s) running")
    assert "per-person-per-org limit" in message and f"retry in {e.value.retry_after} s" in message


def test_le_refus_du_plafond_annonce_la_premiere_place_a_se_liberer(monkeypatch):
    _plafond(monkeypatch, 2)
    with places.place("a", 300), places.place("b", 40):
        with pytest.raises(places.BacASableOccupe) as e:
            with places.place("c", 100):
                pytest.fail("une troisième exécution est entrée sous un plafond de 2")
    assert 39 <= e.value.retry_after <= 40
    message = str(e.value)
    assert message.startswith("sandbox_busy: the sandbox is running its maximum of 2")
    assert "server-wide limit" in message


def test_une_place_sans_borne_garde_le_pas_fixe(monkeypatch):
    """Une place prise sans borne peut se libérer à tout instant : jamais plus que
    `ATTENTE_S`, même à côté d'une place bornée loin."""
    _plafond(monkeypatch, 2)
    with places.place("a"), places.place("b", 300):
        with pytest.raises(places.BacASableOccupe) as e:
            with places.place("c"):
                pass
    assert e.value.retry_after == places.ATTENTE_S


def test_une_place_qui_depasse_sa_borne_se_journalise_sans_le_compte(caplog, monkeypatch):
    import logging
    _un_par_compte(monkeypatch)
    with places.place("compte-secret", 0.01):
        time.sleep(0.05)
        with caplog.at_level(logging.WARNING, logger=places.__name__):
            with pytest.raises(places.BacASableOccupe) as e:
                with places.place("compte-secret"):
                    pass
    assert e.value.retry_after == places.ATTENTE_S
    assert any("après sa borne" in r.getMessage() for r in caplog.records)
    assert not any("compte-secret" in r.getMessage() for r in caplog.records)


def test_le_pont_annonce_sa_borne_au_refus_suivant(monkeypatch):
    """Une recette-code en cours (budget de 120 s) : le même compte est refusé avec un
    `Retry-After` qui couvre tout ce que l'exécution peut encore tenir sa place."""
    _un_par_compte(monkeypatch)
    tenue = asyncio.Event()

    async def tient(**_kw):
        tenue.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(pont, "_executer_avec_pont", tient)
    budget = pont.Budget(delai_s=120.0)

    async def scenario():
        tache = asyncio.ensure_future(pont.executer_avec_pont(
            sources={}, entrypoint="r:run", appeler=None, sub="a", budget=budget))
        await tenue.wait()
        try:
            with pytest.raises(places.BacASableOccupe) as e:
                await pont.executer_avec_pont(sources={}, entrypoint="r:run",
                                              appeler=None, sub="a", budget=budget)
            return e.value.retry_after
        finally:
            tache.cancel()
            with pytest.raises(asyncio.CancelledError):
                await tache

    borne = pont.duree_max_s(budget)
    assert borne == 120.0 + 2 * pont.GRACE_S + places.MARGE_ARRET_S
    assert borne - 2 <= asyncio.run(scenario()) <= borne
