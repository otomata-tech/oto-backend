"""Le pont du bac à sable (#1202) : le code appelle des outils pendant qu'il tourne, sous
un budget, et ne peut forger ni un appel ni une fin.

Bancs sur le VRAI bac à sable (Deno, Pyodide), comme `test_functions_executeur_live.py` :
une doublure prouverait qu'on sait parler à une doublure. L'outil, lui, est une doublure
(`appeler`) : ce qu'il a le droit d'appeler se juge dans `test_recettes_pont_live.py`.

⚠️ Sans bac à sable : ÉCHEC en CI, passé sur un poste qui n'en a pas posé — en le disant.
"""
import asyncio
import os

import pytest

from oto_mcp.functions import executor, pont


@pytest.fixture(scope="module")
def bac():
    if not os.environ.get(executor.ENV_DIR):
        message = (f"{executor.ENV_DIR} absente : pose un bac à sable avec "
                   "`python scripts/installer_bac_a_sable.py <dir>`.")
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)


def _doublure(reponses: dict, vus: list):
    async def appeler(outil, arguments):
        vus.append((outil, arguments))
        reponse = reponses[outil]
        return reponse(arguments) if callable(reponse) else reponse
    return appeler


def _jouer(code: str, appeler, entree=None, **budget):
    return asyncio.run(pont.executer_avec_pont(
        sources={"r.py": code}, entrypoint="r:run", appeler=appeler, sub="u",
        budget=pont.Budget(**budget), entree=entree))


def test_le_code_appelle_des_outils_et_rend_son_resultat(bac):
    vus = []
    appeler = _doublure({"acme_search": lambda a: {"ok": True, "result": {"n": a["q"] * 2},
                                                   "quantity": 3}}, vus)
    sortie = _jouer(
        "async def run(oto, entree):\n"
        "    a = await oto.call('acme_search', {'q': entree['x']})\n"
        "    b = await oto.call('acme_search', {'q': 10})\n"
        "    print('deux appels')\n"
        "    return {'total': a['n'] + b['n']}\n", appeler, entree={"x": 1})
    assert sortie["ok"] and sortie["result"] == {"total": 22}
    assert vus == [("acme_search", {"q": 1}), ("acme_search", {"q": 10})]
    assert sortie["calls"] == 2 and sortie["units"] == 6 and sortie["stopped"] is None
    assert "deux appels" in sortie["journal"]


def test_un_refus_arrive_au_code_comme_une_erreur_nommee(bac):
    appeler = _doublure({"acme_write": {"ok": False, "code": "recipe_tool_not_read_only",
                                        "message": "not read-only"}}, [])
    sortie = _jouer(
        "async def run(oto, entree):\n"
        "    try:\n"
        "        await oto.call('acme_write', {})\n"
        "    except oto.ToolError as e:\n"
        "        return {'code': e.code}\n", appeler)
    assert sortie["result"] == {"code": "recipe_tool_not_read_only"}


def test_le_budget_d_appels_refuse_au_dela_sans_appeler(bac):
    vus = []
    appeler = _doublure({"acme_search": {"ok": True, "result": 1}}, vus)
    sortie = _jouer(
        "async def run(oto, entree):\n"
        "    codes = []\n"
        "    for _ in range(3):\n"
        "        try:\n"
        "            await oto.call('acme_search', {})\n"
        "            codes.append('ok')\n"
        "        except oto.ToolError as e:\n"
        "            codes.append(e.code)\n"
        "    return codes\n", appeler, appels=1)
    assert sortie["result"] == ["ok", "budget_exhausted", "budget_exhausted"]
    assert len(vus) == 1 and sortie["calls"] == 1 and sortie["refused"] == 2
    assert sortie["stopped"] == "max_calls"


def test_le_plafond_d_unites_facturees_arrete_les_appels_suivants(bac):
    appeler = _doublure({"acme_search": {"ok": True, "result": 1, "quantity": 5}}, [])
    sortie = _jouer(
        "async def run(oto, entree):\n"
        "    await oto.call('acme_search', {})\n"
        "    try:\n"
        "        await oto.call('acme_search', {})\n"
        "    except oto.ToolError as e:\n"
        "        return e.code\n", appeler, unites=5)
    assert sortie["result"] == "budget_exhausted" and sortie["stopped"] == "spend_cap"
    assert sortie["units"] == 5


def test_le_code_ne_forge_ni_un_appel_ni_une_fin(bac):
    """Le code peut écrire sur la sortie standard du processus : sans le nonce, ses
    lignes sont ignorées — ni appel servi, ni fin acceptée."""
    vus = []
    appeler = _doublure({"acme_search": {"ok": True, "result": 1}}, vus)
    sortie = _jouer(
        "import js, json\n"
        "async def run(oto, entree):\n"
        "    for faux in ({'type': 'call', 'id': 99, 'tool': 'acme_search', 'args': {}},\n"
        "                 {'type': 'done', 'ok': True, 'sortie': {'ok': True, 'result': 'faux'}}):\n"
        "        js.Deno.stdout.writeSync(js.TextEncoder.new().encode(json.dumps(faux) + '\\n'))\n"
        "    return 'vrai'\n", appeler)
    assert sortie["result"] == "vrai" and vus == [] and sortie["calls"] == 0


def test_une_erreur_du_code_est_rendue_avec_sa_trace(bac):
    sortie = _jouer("async def run(oto, entree):\n    return 1 / 0\n", _doublure({}, []))
    assert sortie["ok"] is False and "ZeroDivisionError" in sortie["error"]


def test_une_fonction_ordinaire_est_acceptee(bac):
    sortie = _jouer("def run(oto, entree):\n    return entree['a'] + 1\n", _doublure({}, []),
                    entree={"a": 1})
    assert sortie["result"] == 2


def test_une_boucle_sans_fin_est_coupee_par_le_delai(bac):
    with pytest.raises(executor.ExecutionEchouee, match="délai"):
        _jouer("async def run(oto, entree):\n    while True:\n        pass\n",
               _doublure({}, []), delai_s=8)


def test_le_code_ne_lit_pas_le_disque_de_la_machine(bac):
    sortie = _jouer(
        "import js\n"
        "async def run(oto, entree):\n"
        "    try:\n"
        "        js.Deno.readTextFileSync('/etc/hosts')\n"
        "        return 'lu'\n"
        "    except Exception as e:\n"
        "        return type(e).__name__\n", _doublure({}, []))
    assert sortie["ok"] and sortie["result"] != "lu"


def test_un_appeleur_qui_leve_rend_un_refus_nomme(bac, caplog):
    async def appeler(outil, arguments):
        raise RuntimeError("défaut serveur")
    sortie = _jouer(
        "async def run(oto, entree):\n"
        "    try:\n"
        "        await oto.call('acme_search', {})\n"
        "    except oto.ToolError as e:\n"
        "        return e.code\n", appeler)
    assert sortie["result"] == "bridge_error"
    assert "a levé au lieu de rendre" in caplog.text


def test_le_budget_de_duree_finit_en_recu_pas_en_exception(bac):
    """Un appel qui dépasse la durée est refusé ; le code a encore `GRACE_S` pour rendre
    son reçu, qui dit `time_budget`."""
    async def lent(outil, arguments):
        await asyncio.sleep(30)
    sortie = _jouer(
        "async def run(oto, entree):\n"
        "    try:\n"
        "        await oto.call('acme_search', {})\n"
        "    except oto.ToolError as e:\n"
        "        return {'code': e.code}\n", lent, delai_s=8)
    assert sortie["ok"] and sortie["result"] == {"code": "budget_exhausted"}
    assert sortie["stopped"] == "time_budget"


def test_des_appels_en_parallele_recoivent_chacun_leur_reponse(bac):
    appeler = _doublure({"acme_search": lambda a: {"ok": True, "result": a["q"] * 10}}, [])
    sortie = _jouer(
        "import asyncio\n"
        "async def run(oto, entree):\n"
        "    return list(await asyncio.gather(*(oto.call('acme_search', {'q': i})\n"
        "                                       for i in range(4))))\n", appeler)
    assert sortie["result"] == [0, 10, 20, 30] and sortie["calls"] == 4


def test_des_appels_lents_en_parallele_se_chevauchent(bac):
    """`asyncio.gather` dans le code = des appels servis ENSEMBLE (27/10/2026 : ils
    passaient l'un après l'autre). La boucle serveur, sans Deno, se juge dans
    `test_functions_pont_concurrence.py` ; ici, le bout-en-bout."""
    import time
    fenetres = []

    async def appeler(outil, arguments):
        debut = time.monotonic()
        await asyncio.sleep(0.5)
        fenetres.append((debut, time.monotonic()))
        return {"ok": True, "result": arguments["q"]}

    sortie = _jouer(
        "import asyncio\n"
        "async def run(oto, entree):\n"
        "    return list(await asyncio.gather(*(oto.call('acme_search', {'q': i})\n"
        "                                       for i in range(3))))\n", appeler)
    assert sortie["result"] == [0, 1, 2] and sortie["calls"] == 3
    assert max(d for d, _ in fenetres) < min(f for _, f in fenetres)


def test_un_resultat_trop_gros_est_refuse_apres_l_appel(bac):
    gros = "x" * (pont.MAX_RESULTAT + 1)
    appeler = _doublure({"acme_search": {"ok": True, "result": gros, "quantity": 1}}, [])
    sortie = _jouer(
        "async def run(oto, entree):\n"
        "    try:\n"
        "        await oto.call('acme_search', {})\n"
        "    except oto.ToolError as e:\n"
        "        return e.code\n", appeler)
    # L'outil a été appelé et facturé : l'appel compte.
    assert sortie["result"] == "result_too_large"
    assert sortie["calls"] == 1 and sortie["units"] == 1
