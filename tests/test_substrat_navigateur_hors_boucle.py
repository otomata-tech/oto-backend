"""oto-backend#867, lot 2 — le substrat navigateur ne fige plus le processus.

Ouvrir et libérer une session Browserbase sont des appels HTTP **synchrones**
(urllib, 30 s de délai d'attente) au milieu de fonctions `async def`. Appelés
nûment, ils bloquent tout le processus le temps que Browserbase réponde.

Le défaut vit dans le SEGMENT, pas chez ses appelants : quatre familles d'outils
passent par lui (le navigateur générique, deux connecteurs d'API privée, la GED),
plus le troisième cran de la lecture web. Le corriger une fois les couvre toutes
— et c'est pour ça qu'on l'éprouve ici plutôt que chez chaque appelant.

Même méthode que les autres bancs de cette classe : on OBSERVE le thread, et la
mesure doit MORDRE sur la forme d'avant.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from oto_mcp import browserbase


class _Stop(Exception):
    """Interrompt le scénario juste après le segment qu'on mesure."""


def test_l_ouverture_de_session_s_execute_hors_de_la_boucle(monkeypatch):
    vu = {}

    def _faux_start(*a, **kw):
        vu["thread"] = threading.current_thread()
        raise _Stop()

    monkeypatch.setattr(browserbase, "start_ephemeral_session", _faux_start)

    async def _scenario():
        vu["boucle"] = threading.current_thread()
        with pytest.raises(_Stop):
            await browserbase.fetch_page_ephemeral("https://exemple.invalid")

    asyncio.run(_scenario())
    assert vu["thread"] is not vu["boucle"], (
        "l'ouverture de session s'est faite DANS la boucle : un Browserbase lent "
        "y fige tout le processus.")


def test_l_ouverture_avec_profil_persistant_aussi(monkeypatch):
    """L'autre porte : la session liée à un Context, celle des connecteurs."""
    vu = {}

    def _faux_start(*a, **kw):
        vu["thread"] = threading.current_thread()
        raise _Stop()

    monkeypatch.setattr(browserbase, "start_session", _faux_start)

    async def _scenario():
        vu["boucle"] = threading.current_thread()
        with pytest.raises(_Stop):
            await browserbase.run_page_eval("ctx-1", "https://exemple.invalid", "() => 1")

    asyncio.run(_scenario())
    assert vu["thread"] is not vu["boucle"]


def test_la_mesure_MORD_sur_la_forme_d_avant():
    """Le même appel joué nûment doit être vu comme s'exécutant dans la boucle,
    sinon les deux épreuves ci-dessus passeraient au vert sans rien prouver."""
    vu = {}

    def _appel_sync():
        vu["thread"] = threading.current_thread()

    async def _scenario_nu():
        vu["boucle"] = threading.current_thread()
        _appel_sync()

    asyncio.run(_scenario_nu())
    assert vu["thread"] is vu["boucle"]


def test_la_boucle_reste_vivante_pendant_une_ouverture_lente(monkeypatch):
    """La preuve qui vaut pour la production : pendant que Browserbase prend son
    temps, le processus continue de servir."""
    tours = {"n": 0}

    def _start_lent(*a, **kw):
        time.sleep(0.30)
        raise _Stop()

    monkeypatch.setattr(browserbase, "start_ephemeral_session", _start_lent)

    async def _battement():
        while True:
            tours["n"] += 1
            await asyncio.sleep(0.01)

    async def _scenario():
        batteur = asyncio.create_task(_battement())
        with pytest.raises(_Stop):
            await browserbase.fetch_page_ephemeral("https://exemple.invalid")
        batteur.cancel()

    asyncio.run(_scenario())
    assert tours["n"] > 5, (
        f"seulement {tours['n']} tours pendant 300 ms d'ouverture : boucle bloquée.")


def test_la_liberation_de_session_est_aussi_hors_boucle(monkeypatch):
    """Elle vit dans un `finally` : c'est le chemin le plus facile à oublier, et
    il s'exécute justement quand les choses vont mal."""
    vu = {}

    monkeypatch.setattr(browserbase, "start_ephemeral_session",
                        lambda *a, **kw: {"id": "s-1", "connectUrl": "ws://x"})

    def _faux_release(sid):
        vu["thread"] = threading.current_thread()

    monkeypatch.setattr(browserbase, "release_session", _faux_release)

    async def _scenario():
        vu["boucle"] = threading.current_thread()
        # Le navigateur n'est pas installé ici : l'import échoue, le `finally`
        # s'exécute — c'est exactement ce qu'on veut mesurer.
        with pytest.raises(Exception):
            await browserbase.fetch_page_ephemeral("https://exemple.invalid")

    asyncio.run(_scenario())
    assert vu.get("thread") is not None, "la libération n'a pas eu lieu"
    assert vu["thread"] is not vu["boucle"]


def _appels_sync_dans_une_async(source: str, methode: str) -> list[int]:
    """Lignes où `methode` est appelée dans une fonction `async def` sans passer
    au thread.

    C'est la définition EXACTE de la classe : dans une fonction `def`, le seam des
    capacités range déjà l'appel hors de la boucle. Ne pas faire cette distinction
    fait crier le contrôle sur du code sain — et un garde-fou qui crie à tort
    finit ignoré.
    """
    import ast

    arbre = ast.parse(source)
    differes: set[int] = set()
    for n in ast.walk(arbre):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "to_thread"):
            for sous in ast.walk(n):
                if isinstance(sous, ast.Call):
                    differes.add(id(sous))

    lignes = []
    for fn in ast.walk(arbre):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for n in ast.walk(fn):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == methode and id(n) not in differes):
                lignes.append(n.lineno)
    return sorted(lignes)


def test_la_sonde_de_compte_unipile_n_est_plus_appelee_nument():
    """L'autre chemin de cette famille : la sonde de vivacité du wizard d'auth
    hébergée. Son voisin immédiat était protégé, pas elle — même fichier, même
    client, une seule des deux lignes traitée.

    ⚠️ Contrôle STATIQUE, et c'est une faiblesse assumée : la fonction qui le
    porte exige une base et un état de wizard qu'on ne monte pas ici, donc on ne
    peut pas observer le thread comme dans les épreuves du dessus. Il lit l'arbre
    plutôt que le texte — il tombera si quelqu'un remet la forme d'avant, pas sur
    un reformatage.
    """
    import pathlib as _p

    src = (_p.Path(__file__).resolve().parents[1] / "oto_mcp" / "unipile_connect.py"
           ).read_text()
    nus = _appels_sync_dans_une_async(src, "account_alive")
    assert not nus, (
        f"`account_alive` appelé nûment ligne(s) {nus} : appel HTTP synchrone dans "
        "une fonction `async def`, il fige le processus le temps qu'Unipile "
        "réponde. L'envelopper dans `asyncio.to_thread`.")


def test_ce_controle_MORD_sur_la_forme_d_avant():
    """Un détecteur qui ne voit rien rend du vert."""
    assert _appels_sync_dans_une_async(
        "async def f(c):\n    return c.account_alive('a')\n", "account_alive") == [2]


def test_ce_controle_ne_crie_pas_sur_une_fonction_synchrone():
    """Dans une fonction `def`, le seam range déjà l'appel hors de la boucle :
    le signaler serait un faux positif. Vérifié en vrai — un appel légitime de
    cette forme existe dans le fichier surveillé, et le contrôle le laissait
    passer pour une régression avant cette précision."""
    assert _appels_sync_dans_une_async(
        "def f(c):\n    return c.account_alive('a')\n", "account_alive") == []


def test_ce_controle_voit_l_enveloppe():
    assert _appels_sync_dans_une_async(
        "import asyncio\nasync def f(c):\n"
        "    return await asyncio.to_thread(lambda: c.account_alive('a'))\n",
        "account_alive") == []
