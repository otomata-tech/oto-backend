"""La ferme de recettes : l'agent (`ferme_recettes/agent.py`) et le mode distant du
serveur (`functions/distant.py`), bout à bout, SANS Deno — un faux `deno` parle le
protocole des harnais (tâche en première ligne, lignes préfixées du nonce)."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import textwrap
import time

import pytest

from ferme_recettes import agent as agent_mod
from oto_mcp.functions import distant, executor, places, pont

SECRET = "s" * 40

# Le faux `deno` : le dernier argument est le script lancé (`sandbox.ts` = aller simple,
# `sandbox_pont.ts` = pont). Ce que fait le « code » se lit dans `job.input.fake`.
FAUX_DENO = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, sys, time
    pont = sys.argv[-1].endswith("sandbox_pont.ts")
    tache = json.loads(sys.stdin.readline())
    nonce, job = tache["nonce"], tache["job"]
    fake = (job.get("input") or {{}}).get("fake", "echo")
    def dire(obj):
        sys.stdout.write(nonce + json.dumps(obj) + "\\n"); sys.stdout.flush()
    if fake == "sleep":
        time.sleep(30)
    if not pont:
        sys.stdin.read()
        dire({{"ok": True, "sortie": {{"ok": True, "result": {{"wheels": tache["wheels"]}}}}}})
        sys.exit(0)
    resultat = None
    if fake == "call":
        dire({{"type": "call", "id": 1, "tool": "outil_test", "args": {{"a": 1}}}})
        resultat = json.loads(sys.stdin.readline())
    dire({{"type": "done", "ok": True, "sortie": {{"ok": True, "result": resultat}},
           "journal": "fini"}})
    """)


@pytest.fixture
def bac(tmp_path, monkeypatch):
    racine = tmp_path / "bac"
    (racine / "wheels").mkdir(parents=True)
    pyo = racine / executor._PYODIDE_REL
    pyo.mkdir(parents=True)
    (pyo / "pyodide.mjs").write_text("")
    deno = racine / "deno"
    deno.write_text(FAUX_DENO)
    deno.chmod(deno.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("OTO_FUNCTIONS_SANDBOX_DIR", str(racine))
    monkeypatch.setenv("OTO_FUNCTIONS_ISOLATION", "direct")
    monkeypatch.setenv("OTO_FUNCTIONS_ATTENTE_MAX_S", "0")
    places.plafond.cache_clear()
    return racine


async def _demarrer(monkeypatch, *, places_n=2, attente_s=0.3, duree_max_s=60.0,
                    secret=SECRET):
    reglages = agent_mod.Reglages(hote="127.0.0.1", port=0, secret=secret,
                                  places=places_n, attente_s=attente_s,
                                  duree_max_s=duree_max_s)
    agent = agent_mod.Agent(reglages)
    serveur = await asyncio.start_server(agent.servir, "127.0.0.1", 0,
                                         limit=executor.MAX_REPONSE)
    port = serveur.sockets[0].getsockname()[1]
    monkeypatch.setenv("OTO_FUNCTIONS_FERME", f"127.0.0.1:{port}")
    monkeypatch.setenv("OTO_FUNCTIONS_FERME_SECRET", SECRET)
    return agent, serveur


async def _appeler(outil, arguments):
    return {"ok": True, "result": {"vu": outil, "arguments": arguments}}


def test_mode_local_par_defaut(monkeypatch):
    monkeypatch.delenv("OTO_FUNCTIONS_FERME", raising=False)
    assert distant.actif() is False


def test_pont_bout_a_bout_par_la_ferme(bac, monkeypatch):
    async def scenario():
        agent, serveur = await _demarrer(monkeypatch)
        async with serveur:
            rendu = await pont.executer_avec_pont(
                sources={"r.py": "x"}, entrypoint="r:run", appeler=_appeler, sub="u1",
                entree={"fake": "call"})
        return rendu, agent
    rendu, agent = asyncio.run(scenario())
    assert rendu["ok"] is True
    # L'appel d'outil est remonté au SERVEUR (son `appeler`), pas servi par la ferme.
    assert rendu["result"]["result"] == {"vu": "outil_test", "arguments": {"a": 1}}
    assert rendu["calls"] == 1
    assert agent.en_cours == 0


def test_aller_simple_par_la_ferme(bac, monkeypatch):
    async def demarrer():
        return await _demarrer(monkeypatch)

    boucle = asyncio.new_event_loop()
    agent, serveur = boucle.run_until_complete(demarrer())
    import threading
    fil = threading.Thread(target=boucle.run_forever, daemon=True)
    fil.start()
    try:
        rendu = executor.executer(sources={"f.py": "x"}, entrypoint="f:run",
                                  requirements=[], mode="run", sub="u1", entree={})
    finally:
        boucle.call_soon_threadsafe(boucle.stop)
        fil.join(5)
    assert rendu["ok"] is True
    # Les chemins des roues sont ceux de la BOX (aucune demandée ici).
    assert rendu["result"] == {"wheels": []}


def test_secret_refuse(bac, monkeypatch):
    async def scenario():
        _, serveur = await _demarrer(monkeypatch)
        monkeypatch.setenv("OTO_FUNCTIONS_FERME_SECRET", "x" * 40)
        async with serveur:
            with pytest.raises(places.BacASableOccupe):
                await distant.ouvrir(script="pont", requirements=[], duree_max_s=5,
                                     empreinte=executor.empreinte(), limite=1024)
    asyncio.run(scenario())


def test_empreinte_differente_refusee_et_rejouable(bac, monkeypatch):
    async def scenario():
        _, serveur = await _demarrer(monkeypatch)
        async with serveur:
            with pytest.raises(places.BacASableOccupe) as e:
                await distant.ouvrir(script="pont", requirements=[], duree_max_s=5,
                                     empreinte="autre", limite=1024)
        return e.value
    refus = asyncio.run(scenario())
    assert refus.retry_after == distant.ATTENTE_EMPREINTE_S
    assert "another sandbox version" in str(refus)


def test_dependance_hors_liste_blanche_refusee(bac, monkeypatch):
    async def scenario():
        _, serveur = await _demarrer(monkeypatch)
        async with serveur:
            with pytest.raises(ValueError):
                await pont.executer_avec_pont(sources={"r.py": "x"}, entrypoint="r:run",
                                              appeler=_appeler, sub="u1",
                                              requirements=("inconnue",))
    asyncio.run(scenario())


def test_ferme_pleine_rend_busy_avec_retry_after(bac, monkeypatch):
    async def scenario():
        agent, serveur = await _demarrer(monkeypatch, places_n=1, attente_s=0.2)
        async with serveur:
            premiere = await distant.ouvrir(script="pont", requirements=[],
                                            duree_max_s=30,
                                            empreinte=executor.empreinte(), limite=1024)
            premiere.proc.stdin.write(json.dumps(
                {"nonce": "n", "job": {"input": {"fake": "sleep"}}, "wheels": []}).encode()
                + b"\n")
            await premiere.proc.stdin.drain()
            debut = time.monotonic()
            with pytest.raises(places.BacASableOccupe) as e:
                await distant.ouvrir(script="pont", requirements=[], duree_max_s=5,
                                     empreinte=executor.empreinte(), limite=1024)
            attendu = time.monotonic() - debut
            premiere.proc.kill()
            await premiere.proc.wait()
            for _ in range(50):
                if agent.en_cours == 0:
                    break
                await asyncio.sleep(0.1)
            return e.value, attendu, agent
    refus, attendu, agent = asyncio.run(scenario())
    assert refus.retry_after >= 1
    assert attendu >= 0.15            # la demande a attendu son tour avant le refus
    assert agent.en_cours == 0        # la coupure a tué le processus et rendu la place


def test_place_liberee_va_au_premier_en_attente():
    async def scenario():
        p = agent_mod.Places(1)
        premier = await p.prendre(10, 1)
        ordre = []

        async def demande(nom):
            jeton = await p.prendre(10, 2)
            ordre.append(nom)
            p.rendre(jeton)

        a = asyncio.ensure_future(demande("a"))
        await asyncio.sleep(0.05)
        b = asyncio.ensure_future(demande("b"))
        await asyncio.sleep(0.05)
        p.rendre(premier)
        await asyncio.gather(a, b)
        return ordre
    assert asyncio.run(scenario()) == ["a", "b"]


def test_depart_en_attente_ne_garde_aucune_place():
    async def scenario():
        p = agent_mod.Places(1)
        premier = await p.prendre(10, 1)
        attente = asyncio.ensure_future(p.prendre(10, 5))
        await asyncio.sleep(0.05)
        attente.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attente
        p.rendre(premier)
        return p
    p = asyncio.run(scenario())
    assert p.echeances == {} and not p.attente


def test_duree_depassee_tue_le_processus(bac, monkeypatch):
    async def scenario():
        agent, serveur = await _demarrer(monkeypatch, duree_max_s=1.0)
        monkeypatch.setattr(agent_mod, "MARGE_S", 0.5)
        async with serveur:
            ouverture = await distant.ouvrir(script="pont", requirements=[],
                                             duree_max_s=30,
                                             empreinte=executor.empreinte(), limite=1024)
            ouverture.proc.stdin.write(json.dumps(
                {"nonce": "n", "job": {"input": {"fake": "sleep"}}, "wheels": []}).encode()
                + b"\n")
            await ouverture.proc.stdin.drain()
            debut = time.monotonic()
            code = await asyncio.wait_for(ouverture.proc.wait(), 10)
            return code, time.monotonic() - debut, agent
    code, duree, agent = asyncio.run(scenario())
    assert code != 0
    assert duree < 5                 # tué à SA durée (1 s + marge), pas aux 30 s du code
    assert agent.en_cours == 0


def test_ferme_injoignable_rend_busy(monkeypatch):
    monkeypatch.setenv("OTO_FUNCTIONS_FERME", "127.0.0.1:1")
    monkeypatch.setenv("OTO_FUNCTIONS_FERME_SECRET", SECRET)

    async def scenario():
        with pytest.raises(places.BacASableOccupe) as e:
            await distant.ouvrir(script="pont", requirements=[], duree_max_s=5,
                                 empreinte="x", limite=1024)
        return e.value
    assert asyncio.run(scenario()).retry_after == distant.ATTENTE_INJOIGNABLE_S


def test_l_agent_refuse_de_demarrer_sans_secret_ni_adresse(monkeypatch):
    monkeypatch.delenv("FERME_RECETTES_SECRET", raising=False)
    with pytest.raises(SystemExit):
        agent_mod.Reglages.depuis_env()
    monkeypatch.setenv("FERME_RECETTES_SECRET", SECRET)
    monkeypatch.delenv("FERME_RECETTES_HOTE", raising=False)
    with pytest.raises(SystemExit):
        agent_mod.Reglages.depuis_env()
