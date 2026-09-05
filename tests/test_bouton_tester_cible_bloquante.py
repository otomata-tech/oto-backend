"""Le bouton « tester » face à une cible asynchrone qui bloque — oto-backend#867.

Le dispatch du bouton route correctement : une cible **synchrone** part au
threadpool. Mais une cible **asynchrone** est awaitée telle quelle, et si elle
bloque, la boucle gèle — mesuré le 04/09 : une cible qui bloque 0,40 s laisse
passer **5 tours de boucle au lieu des ~40 attendus**. La borne de 45 s ne
protège pas : on ne préempte pas une coroutine qui ne rend jamais la main.

Il n'y a rien à corriger dans le dispatch, et rien n'y est corrigeable : on ne
rend pas non bloquante une coroutine qui bloque, le correctif est toujours chez
la cible. Ce qui manquait, c'est un TÉMOIN.

État mesuré au moment de la pose : 615 outils montés, 58 testables (sept
namespaces open-data), **un seul asynchrone** — la reconnaissance de domaine,
dont l'unique appel bloquant (un handshake TLS) est déjà passé au thread.
L'exemple que citait l'inventaire de l'issue, la lecture de page par navigateur,
n'est pas testable : il n'appartient à aucun de ces namespaces.

**Comment étendre la liste** — même patron que le cliquet de cardinalité : quand
un outil testable asynchrone apparaît, on ne l'ajoute pas pour faire taire le
test. On l'ouvre, on répond à UNE question — *tous ses appels bloquants sont-ils
passés au thread ?* — et on n'ajoute son nom qu'une fois la réponse oui, en
disant ici pourquoi.
"""
from __future__ import annotations

import asyncio
import inspect
import time

import pytest


# Outils testables ASYNCHRONES dont on a vérifié à la main qu'ils ne bloquent pas.
# Un nom ici est une affirmation, pas un silence.
TESTABLES_ASYNC_VERIFIES = {
    # `infosec_domain` : tout en client asynchrone (httpx.AsyncClient) ; son seul
    # appel bloquant est le handshake TLS, déjà enveloppé dans `asyncio.to_thread`.
    # Vérifié le 2026-09-04.
    "infosec_domain",
}


def _testables_async() -> set[str]:
    import logging

    from fastmcp import FastMCP

    from oto_mcp import tool_visibility as TV
    from oto_mcp import tools as T

    logging.getLogger("oto_mcp.tools.mount").setLevel(logging.ERROR)
    m = FastMCP("temoin-867")
    T.register_all(m)
    outils = asyncio.run(m.list_tools(run_middleware=False))
    return {t.name for t in outils
            if TV.is_testable(t.name)
            and inspect.iscoroutinefunction(getattr(t, "fn", None))}


def test_aucun_outil_testable_asynchrone_non_verifie():
    """LE TÉMOIN. Il ne dit pas qu'un nouvel entrant est fautif — il dit que
    personne ne l'a encore regardé, et que le bouton « tester » est le seul
    endroit où sa faute figerait tout le processus."""
    vus = _testables_async()
    nouveaux = vus - TESTABLES_ASYNC_VERIFIES
    assert not nouveaux, (
        f"outil(s) testable(s) asynchrone(s) jamais vérifié(s) : {sorted(nouveaux)}. "
        "Le bouton « tester » les await tels quels : s'ils bloquent, ils figent "
        "TOUT le processus, et la borne de 45 s ne préempte pas une coroutine. "
        "Ouvrir chacun, vérifier que ses appels bloquants passent au thread, puis "
        "ajouter son nom à TESTABLES_ASYNC_VERIFIES en disant pourquoi.")


def test_le_temoin_MORD_sur_un_entrant_inconnu():
    """Un témoin qui ne tombe jamais ne surveille rien. On lui donne un entrant
    que personne n'a vérifié."""
    vus = {"infosec_domain", "_faux_entrant_async"}
    nouveaux = vus - TESTABLES_ASYNC_VERIFIES
    assert nouveaux == {"_faux_entrant_async"}


def test_la_population_verifiee_ne_sert_pas_a_masquer_une_disparition():
    """L'autre sens : un nom listé qui n'existe plus est du bruit qui périme la
    liste. On le dit, sans faire échouer — retirer un outil est légitime."""
    vus = _testables_async()
    disparus = TESTABLES_ASYNC_VERIFIES - vus
    if disparus:
        pytest.skip(f"nom(s) à retirer de la liste (outil parti) : {sorted(disparus)}")


def test_une_cible_asynchrone_bloquante_fige_bien_la_boucle():
    """La mesure qui justifie ce fichier, rejouée : sans elle, la liste
    ci-dessus ressemblerait à une précaution gratuite.

    On reproduit la forme EXACTE du dispatch — une coroutine function est awaitée
    telle quelle — et on compte les tours de boucle."""
    tours = {"n": 0}

    async def _cible_qui_bloque():
        time.sleep(0.30)

    async def _battement():
        while True:
            tours["n"] += 1
            await asyncio.sleep(0.01)

    async def _scenario():
        b = asyncio.create_task(_battement())
        await asyncio.sleep(0.05)
        depart = tours["n"]
        # la forme du dispatch : `if iscoroutinefunction(fn): return await fn(...)`
        assert asyncio.iscoroutinefunction(_cible_qui_bloque)
        await asyncio.wait_for(_cible_qui_bloque(), timeout=45)
        b.cancel()
        return tours["n"] - depart

    pendant = asyncio.run(_scenario())
    assert pendant < 10, (
        f"{pendant} tours pendant 300 ms de blocage : la boucle ne gèle plus, "
        "donc ce fichier surveille un défaut qui n'existe plus — le relire.")
