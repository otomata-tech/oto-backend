"""Le pont sert en parallèle les appels que le code lance en parallèle (27/10/2026).

Une recette-code lançait cinq recherches par `asyncio.gather` ; le journal les montrait
l'une APRÈS l'autre (~11 s chacune) : le serveur lisait une demande, attendait l'outil,
répondait, puis seulement lisait la suivante. La recette dépassait sa durée.

Ces bancs tournent SANS Deno : le bac à sable est remplacé par un petit processus Python
qui parle le même protocole (une ligne JSON préfixée du nonce par message, réponses par
`id`) — ce qu'on éprouve est la boucle du SERVEUR, ses vrais tubes et ses vraies tâches.
Le même cas sur le vrai bac à sable vit dans `test_functions_pont_live.py`.
"""
import asyncio
import logging
import os
import sys
import threading
import time

import pytest

from oto_mcp.functions import executor, isolement, pont

# Le faux bac à sable : il lance TOUTES ses demandes d'un coup (`job.input.calls`), puis
# lit autant de réponses, dans l'ordre où elles arrivent, et les rend. `abandonne` : il
# finit sans attendre ses réponses (un appel lancé et jamais attendu par le code).
# `lire` : il n'en attend que tant, puis finit — en erreur si `ok` est faux (un appel
# d'un `gather` a levé, les autres volent encore). `meurt` : il sort aussitôt ses demandes
# écrites, sans réponse ni fin (un bac à sable tué en vol).
_FAUX_BAC = r'''
import json, sys
tache = json.loads(sys.stdin.readline())
nonce, plan = tache["nonce"], tache["job"]["input"]
def ecrire(o):
    sys.stdout.write(nonce + json.dumps(o) + "\n")
    sys.stdout.flush()
for i, args in enumerate(plan["calls"], 1):
    ecrire({"type": "call", "id": i, "tool": "acme_search", "args": args})
if plan.get("meurt"):
    import time
    time.sleep(0.3)  # le serveur a lu les demandes et lancé leurs appels
    sys.exit(0)
recues = []
if not plan.get("abandonne"):
    for _ in range(plan.get("lire", len(plan["calls"]))):
        recues.append(json.loads(sys.stdin.readline()))
if plan.get("ok", True):
    ecrire({"type": "done", "ok": True, "sortie": {"ok": True, "result": recues},
            "journal": ""})
else:
    ecrire({"type": "done", "ok": False, "erreur": "un appel du gather a levé"})
'''


@pytest.fixture()
def faux_bac(tmp_path, monkeypatch):
    script = tmp_path / "faux_bac.py"
    script.write_text(_FAUX_BAC, encoding="utf-8")
    monkeypatch.setattr(executor, "repertoire", lambda: tmp_path)
    monkeypatch.setattr(executor, "roues", lambda racine, requirements: [])
    monkeypatch.setattr(executor, "chemin_pyodide", lambda racine: tmp_path)
    monkeypatch.setattr(executor, "lancement", lambda racine, s, duree: isolement.Lancement(
        argv=[sys.executable, "-I", str(script)], env=dict(os.environ), cwd=str(tmp_path)))


def _jouer(demandes: list, appeler, abandonne=False, plan: dict | None = None,
           **budget) -> dict:
    return asyncio.run(pont.executer_avec_pont(
        sources={"r.py": ""}, entrypoint="r:run", appeler=appeler, sub="u",
        budget=pont.Budget(**budget),
        entree={"calls": demandes, "abandonne": abandonne, **(plan or {})}))


def _outil_lent(vus: list):
    """`appeler` qui dort `args['s']` secondes et note (début, fin) de chaque appel."""
    async def appeler(outil, arguments):
        debut = time.monotonic()
        await asyncio.sleep(arguments["s"])
        vus.append((arguments["n"], debut, time.monotonic()))
        return {"ok": True, "result": arguments["n"], "quantity": 1}
    return appeler


def test_deux_appels_lents_se_chevauchent(faux_bac):
    vus = []
    sortie = _jouer([{"n": "lent", "s": 0.6}, {"n": "vif", "s": 0.2}], _outil_lent(vus))
    assert sortie["ok"] and sortie["calls"] == 2 and sortie["units"] == 2
    debuts = [d for _, d, _ in vus]
    fins = [f for _, _, f in vus]
    # Le second a commencé AVANT que le premier finisse : servis ensemble, pas en file.
    assert max(debuts) < min(fins)
    assert max(fins) - min(debuts) < 0.6 + 0.2
    # Chaque réponse repart dès qu'elle est prête, avec son `id` : la vive d'abord.
    assert [(r["id"], r["result"]) for r in sortie["result"]] == [(2, "vif"), (1, "lent")]


def test_au_plus_en_vol_appels_a_la_fois(faux_bac):
    en_cours, pic = 0, 0

    async def appeler(outil, arguments):
        nonlocal en_cours, pic
        en_cours += 1
        pic = max(pic, en_cours)
        await asyncio.sleep(0.05)
        en_cours -= 1
        return {"ok": True, "result": arguments["n"]}

    sortie = _jouer([{"n": i} for i in range(8)], appeler, en_vol=3)
    assert pic == 3
    assert sortie["calls"] == 8 and sorted(r["result"] for r in sortie["result"]) == list(range(8))


def test_max_calls_reste_exact_quand_tout_arrive_ensemble(faux_bac):
    vus = []
    sortie = _jouer([{"n": i, "s": 0.05} for i in range(6)], _outil_lent(vus), appels=4)
    assert len(vus) == 4
    assert sortie["calls"] == 4 and sortie["refused"] == 2
    assert sortie["stopped"] == "max_calls"
    codes = sorted(r.get("code", "ok") for r in sortie["result"])
    assert codes == ["budget_exhausted"] * 2 + ["ok"] * 4


def test_un_appel_que_le_code_n_attend_pas_va_a_son_terme(faux_bac):
    """Le code finit sans attendre sa réponse : l'appel admis est joué jusqu'au bout et
    compté, comme quand le serveur servait en série — et répondre à un processus parti
    ne fait pas échouer l'exécution."""
    vus = []
    sortie = _jouer([{"n": "orphelin", "s": 0.2}], _outil_lent(vus), abandonne=True)
    assert sortie["ok"] and [n for n, _, _ in vus] == ["orphelin"]
    assert sortie["calls"] == 1 and sortie["units"] == 1


def test_un_gather_plus_large_que_en_vol_ne_passe_pas_le_plafond_d_unites(faux_bac):
    """Douze appels lancés ensemble, trois places, quatre unités : ceux qui attendent en
    file sont REJUGÉS à l'obtention de leur place. Au plus `en_vol` appels partent sur un
    plafond que les précédents n'avaient pas encore débité — jamais les douze."""
    vus = []
    sortie = _jouer([{"n": i, "s": 0.05} for i in range(12)], _outil_lent(vus),
                    en_vol=3, unites=4)
    assert len(vus) <= 4 + 3 - 1
    assert sortie["calls"] == len(vus) and sortie["units"] == len(vus)
    assert sortie["refused"] == 12 - len(vus)
    assert sortie["stopped"] == "spend_cap"


def test_une_duree_echue_pendant_la_file_ne_laisse_pas_partir_l_appel(faux_bac):
    """Une place, cinq appels de 0,2 s, 0,3 s de budget : le deuxième est coupé par la
    durée, les suivants obtiennent leur place après l'échéance — refusés sans partir, et
    leur place dans `calls` rendue."""
    partis = []

    async def appeler(outil, arguments):
        partis.append(arguments["n"])
        await asyncio.sleep(0.2)
        return {"ok": True, "result": arguments["n"]}

    sortie = _jouer([{"n": i} for i in range(5)], appeler, en_vol=1, delai_s=0.3)
    assert partis == [0, 1]
    assert sortie["calls"] == 2 and sortie["refused"] == 3
    assert sortie["stopped"] == "time_budget"


def test_une_fin_en_erreur_laisse_les_appels_partis_aller_a_leur_terme(faux_bac):
    """Le code lève après sa première réponse (un appel de son `gather` a échoué) : les
    appels déjà partis — des écritures peut-être — vont à leur terme ; celui qui
    attendait encore sa place ne part pas ; puis l'exécution lève."""
    partis, finis = [], []

    async def appeler(outil, arguments):
        partis.append(arguments["n"])
        await asyncio.sleep(arguments["s"])
        finis.append(arguments["n"])
        return {"ok": True, "result": arguments["n"]}

    demandes = [{"n": "vif", "s": 0.05}, {"n": "ecriture", "s": 0.4},
                {"n": "suivant", "s": 0.4}, {"n": "en_file", "s": 0.4}]
    with pytest.raises(executor.ExecutionEchouee, match="gather"):
        _jouer(demandes, appeler, en_vol=2, plan={"lire": 1, "ok": False})
    assert "en_file" not in partis
    assert sorted(finis) == sorted(partis)


def test_chaque_appel_garde_son_contexte_sous_concurrence(faux_bac):
    """Chaque appel est joué dans SA tâche, contexte copié : ce qu'un appel pose dans une
    ContextVar (c'est ainsi qu'`executer_cible` pose compte, org et run de sa cible) ne
    se voit pas d'un appel concurrent, même quand tous la changent en même temps."""
    import contextvars
    cv = contextvars.ContextVar("axe_du_test", default=None)
    vus = []

    async def appeler(outil, arguments):
        cv.set(arguments["n"])
        await asyncio.sleep(arguments["s"])
        vus.append((arguments["n"], cv.get()))
        return {"ok": True, "result": cv.get()}

    sortie = _jouer([{"n": f"compte-{i}", "s": 0.05 * (5 - i)} for i in range(5)],
                    appeler)
    assert len(vus) == 5 and all(n == lu for n, lu in vus)
    assert sorted(r["result"] for r in sortie["result"]) == [f"compte-{i}" for i in range(5)]
    assert cv.get() is None


# ── Un outil synchrone dans un fil : la borne rend la main (28/10/2026) ──────────
#
# Un outil `def` joue dans un fil (`anyio.to_thread.run_sync`) que Python n'interrompt
# pas : la borne ne doit attendre ni lui ni sa tâche annulée au-delà de son délai. Le fil d'ici dort sur un `Event`
# (30 s au plus) plutôt que `time.sleep(30)` : même blocage, mais le banc le libère après
# sa mesure — sinon `asyncio.run`, qui attend ses tâches en fermant, tiendrait 30 s.


def _outil_dans_un_fil(libere: threading.Event):
    async def appeler(outil, arguments):
        import anyio
        await anyio.to_thread.run_sync(libere.wait, 30)
        return {"ok": True, "result": "trop tard"}
    return appeler


def _mesurer(demandes: list, appeler, libere: threading.Event, plan: dict | None = None,
             **budget):
    """`(sortie | exception, durée jusqu'au reçu)`, le fil libéré APRÈS la mesure."""
    async def scenario():
        debut = time.monotonic()
        try:
            sortie = await pont.executer_avec_pont(
                sources={"r.py": ""}, entrypoint="r:run", appeler=appeler, sub="u",
                budget=pont.Budget(**budget), entree={"calls": demandes, **(plan or {})})
        except executor.ExecutionEchouee as e:
            sortie = e
        finally:
            duree = time.monotonic() - debut
            libere.set()
        return sortie, duree
    return asyncio.run(scenario())


def test_un_outil_bloque_dans_un_fil_rend_le_refus_de_duree_a_l_echeance(
        faux_bac, monkeypatch):
    """Le code attend la réponse d'un outil qui bloque son fil 30 s : à l'échéance du
    budget, il reçoit `budget_exhausted` SANS attendre le fil, et rend son reçu ; la fin
    ne laisse au fil abandonné que `GRACE_S`."""
    monkeypatch.setattr(pont, "GRACE_S", 1.0)
    libere = threading.Event()
    sortie, duree = _mesurer([{"n": 1}], _outil_dans_un_fil(libere), libere, delai_s=1.0)
    assert duree < 1.0 + pont.GRACE_S + 2
    assert sortie["ok"] and sortie["stopped"] == "time_budget"
    assert [r["code"] for r in sortie["result"]] == ["budget_exhausted"]


def test_la_fin_n_attend_pas_un_fil_au_dela_de_la_grace(faux_bac, monkeypatch, caplog):
    """Le bac à sable meurt pendant qu'un outil bloque son fil, loin de l'échéance : la
    fin annule l'appel, l'attend au plus `GRACE_S` et lève — au lieu de tenir jusqu'à la
    fin du fil. Ce qui vole encore après la grâce se journalise sans ses arguments ;
    qu'il en reste dépend de la vitesse à laquelle la tâche cède (`anyio` 4 la rend
    aussitôt), d'où l'absence d'exigence sur le nombre de lignes."""
    monkeypatch.setattr(pont, "GRACE_S", 1.0)
    libere = threading.Event()
    with caplog.at_level(logging.WARNING, logger=pont.__name__):
        sortie, duree = _mesurer([{"secret": "valeur-d-argument"}],
                                 _outil_dans_un_fil(libere), libere,
                                 plan={"meurt": True}, delai_s=30.0)
    assert isinstance(sortie, executor.ExecutionEchouee)
    # La grâce, plus la sortie d'erreur attendue 2 s au plus (`_fin_erreurs`).
    assert duree < pont.GRACE_S + 2 + 1
    assert not any("valeur-d-argument" in r.getMessage() for r in caplog.records)
