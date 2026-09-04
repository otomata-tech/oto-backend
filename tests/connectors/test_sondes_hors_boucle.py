"""oto-backend#867, lot 2 — une sonde de connecteur lente ne fige plus le processus.

Les 34 sondes sont presque toutes synchrones et font du HTTP. Trois entrées les
appelaient nûment depuis un handler `async def` : la capacité `connectors.verify`,
le verify-avant-persist de l'écriture de clé, et la console d'instance. Le seam
des capacités ne sort vers le threadpool que les handlers `def` — par la porte
`async`, il ne protège rien, et c'est par là que ces trois-là passent.

Même méthode que `tests/connectors/test_identities_unipile_hors_boucle.py` : on
OBSERVE le thread, parce qu'aucune analyse statique ne dit où un appel synchrone
finit par s'exécuter. Et le contrôle MORD — la forme d'avant, jouée nûment dans
la boucle, doit être détectée par la même mesure, sinon un « vert » ne prouve rien.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from oto_mcp.connectors import verify as connector_verify


def _thread_de_la_boucle() -> threading.Thread:
    async def _run():
        return threading.current_thread()

    return asyncio.run(_run())


def test_une_sonde_synchrone_s_execute_hors_du_thread_de_la_boucle():
    vu = {}

    def _sonde(fields, config):
        vu["thread"] = threading.current_thread()

    async def _scenario():
        vu["boucle"] = threading.current_thread()
        await connector_verify.executer(_sonde, {}, {})

    asyncio.run(_scenario())
    assert vu["thread"] is not vu["boucle"], (
        "la sonde s'est exécutée DANS le thread de la boucle : un amont lent y "
        "fige tout le processus, MCP et REST compris.")


def test_la_mesure_MORD_sur_la_forme_d_avant():
    """Le même appel joué nûment — ce que faisait le code avant ce lot — doit être
    vu comme s'exécutant dans la boucle. Sans cette épreuve, le test précédent
    passerait au vert même si la mesure regardait à côté."""
    vu = {}

    def _sonde(fields, config):
        vu["thread"] = threading.current_thread()

    async def _scenario_nu():
        vu["boucle"] = threading.current_thread()
        _sonde({}, {})          # la forme d'avant : appel direct dans la boucle

    asyncio.run(_scenario_nu())
    assert vu["thread"] is vu["boucle"], (
        "la mesure ne distingue plus les deux formes : elle ne prouve rien.")


def test_la_boucle_reste_vivante_pendant_une_sonde_lente():
    """La preuve qui compte pour la production : pendant que la sonde attend, le
    processus doit continuer à servir. On compte les tours de boucle."""
    tours = {"n": 0}

    def _sonde_lente(fields, config):
        time.sleep(0.30)

    async def _battement():
        while True:
            tours["n"] += 1
            await asyncio.sleep(0.01)

    async def _scenario():
        batteur = asyncio.create_task(_battement())
        await connector_verify.executer(_sonde_lente, {}, {})
        batteur.cancel()

    asyncio.run(_scenario())
    assert tours["n"] > 5, (
        f"seulement {tours['n']} tours de boucle pendant 300 ms de sonde : la "
        "boucle était bloquée.")


def test_une_sonde_qui_ne_repond_pas_rend_une_erreur_nommee(monkeypatch):
    """Une borne de temps, et un message qui dit quoi en conclure : un credential
    n'est pas invalide parce que le service distant est lent."""
    monkeypatch.setattr(connector_verify, "_BORNE_S", 0.05)

    def _sonde_qui_dort(fields, config):
        time.sleep(0.5)

    with pytest.raises(TimeoutError, match="n'a pas répondu"):
        asyncio.run(connector_verify.executer(_sonde_qui_dort, {}, {}))


def test_la_borne_ne_transforme_pas_un_refus_en_delai(monkeypatch):
    """L'échec d'authentification reste l'échec d'authentification : c'est le
    RÉSULTAT de la sonde, et il doit traverser intact."""
    def _sonde_qui_refuse(fields, config):
        raise ValueError("clé invalide")

    with pytest.raises(ValueError, match="clé invalide"):
        asyncio.run(connector_verify.executer(_sonde_qui_refuse, {}, {}))


def test_une_sonde_async_reste_dans_la_boucle_sans_thread_inutile():
    """Une sonde déjà asynchrone n'a rien à faire dans un thread — la router là
    coûterait un thread par test de connexion, sans rien protéger."""
    vu = {}

    async def _sonde_async(fields, config):
        vu["thread"] = threading.current_thread()

    async def _scenario():
        vu["boucle"] = threading.current_thread()
        await connector_verify.executer(_sonde_async, {}, {})

    asyncio.run(_scenario())
    assert vu["thread"] is vu["boucle"]


def test_run_par_son_nom_de_connecteur_passe_par_le_meme_chemin():
    """`run()` est l'entrée du verify-avant-persist, sur le chemin d'écriture de
    TOUTE clé. Elle doit hériter de la protection, pas la contourner."""
    vu = {}

    def _sonde(fields, config):
        vu["thread"] = threading.current_thread()

    connector_verify.register("_bidon_867", _sonde)
    try:
        async def _scenario():
            vu["boucle"] = threading.current_thread()
            await connector_verify.run("_bidon_867", {}, {})

        asyncio.run(_scenario())
    finally:
        connector_verify._REGISTRY.pop("_bidon_867", None)
    assert vu["thread"] is not vu["boucle"]
