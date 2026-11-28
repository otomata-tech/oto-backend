"""L'agent de la ferme de recettes : il lance le bac à sable pour le serveur (28/11/2026).

Il tourne sur une box dédiée, sur le réseau privé, et n'a AUCUN secret de production :
ni base, ni coffre, ni clé de connecteur. Il reçoit d'un serveur oto (protocole :
`oto_mcp/functions/distant.py`) une demande d'exécution, attend une place, lance le MÊME
processus que le serveur lancerait chez lui (`executor.lancement` : Deno en
`--cached-only`, environnement vidé, répertoire en lecture seule, isolement déclaré par
`OTO_FUNCTIONS_ISOLATION`), et fait passer son entrée et ses sorties sur la connexion.
Les appels d'outils du code ne sont PAS servis ici : ils remontent au serveur, qui les
juge (budget, outils permis, écritures) — l'agent ne fait que transporter des lignes.

**Ce qu'il garantit** :
- un appelant qui ne présente pas le secret partagé (`FERME_RECETTES_SECRET`, comparé
  en temps constant) n'obtient rien ;
- une empreinte de bac à sable différente de la sienne (`executor.empreinte()`) est
  refusée : le serveur et la box doivent lancer le même bac à sable ;
- au plus `FERME_RECETTES_PLACES` processus à la fois ; au-delà, la demande attend son
  tour (FIFO) au plus `FERME_RECETTES_ATTENTE_S`, puis un refus `busy` avec
  `retry_after` = quand la première place doit se libérer ;
- chaque processus est tué quand la connexion se coupe, et au plus tard à sa durée
  demandée bornée par `FERME_RECETTES_DUREE_MAX_S` (sa « location » de place).

Lancement : `python -m ferme_recettes.agent` (unité systemd :
`ferme_recettes/oto-ferme-recettes.service`). Il n'importe que des modules de la
bibliothèque standard et `oto_mcp/functions/` (eux-mêmes sans dépendance).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from oto_mcp.functions import distant, executor, pont

logger = logging.getLogger("ferme_recettes")

#: Les scripts que l'agent sait lancer, désignés par le serveur.
SCRIPTS = {"aller": executor._SCRIPT, "pont": pont._SCRIPT}
#: Ce que l'arrêt d'un processus laisse au-delà de sa durée.
MARGE_S = 5.0
#: La plus longue ligne d'en-tête acceptée (le secret, l'empreinte, quelques noms).
MAX_ENTETE = 16 * 1024
_BLOC = 64 * 1024


@dataclass
class Reglages:
    hote: str
    port: int
    secret: str
    places: int = 2
    attente_s: float = 20.0
    duree_max_s: float = 900.0

    @classmethod
    def depuis_env(cls) -> "Reglages":
        secret = os.environ.get("FERME_RECETTES_SECRET") or ""
        if len(secret) < 32:
            raise SystemExit("FERME_RECETTES_SECRET absent ou trop court (32 caractères "
                             "au moins) : l'agent refuse de démarrer.")
        hote = os.environ.get("FERME_RECETTES_HOTE") or ""
        if not hote:
            raise SystemExit("FERME_RECETTES_HOTE absent : l'agent écoute sur l'adresse "
                             "PRIVÉE de la box, déclarée, jamais sur toutes.")
        return cls(hote=hote, port=int(os.environ.get("FERME_RECETTES_PORT") or 8790),
                   secret=secret,
                   places=int(os.environ.get("FERME_RECETTES_PLACES") or 2),
                   attente_s=float(os.environ.get("FERME_RECETTES_ATTENTE_S") or 20),
                   duree_max_s=float(os.environ.get("FERME_RECETTES_DUREE_MAX_S") or 900))


@dataclass
class Places:
    """Les places de la box : FIFO, attente bornée, échéance connue par place."""
    total: int
    echeances: dict = field(default_factory=dict)   # jeton -> instant de fin au plus tard
    attente: deque = field(default_factory=deque)   # futurs des demandes en attente
    _jetons: int = 0

    def _donner(self, echeance: float) -> int:
        self._jetons += 1
        self.echeances[self._jetons] = echeance
        return self._jetons

    def retry_after(self) -> int:
        if not self.echeances:
            return 5
        return max(1, math.ceil(min(self.echeances.values()) - time.monotonic()))

    async def prendre(self, duree_s: float, attente_s: float) -> Optional[int]:
        """Un jeton de place, ou None après `attente_s` (rien n'est pris)."""
        if len(self.echeances) < self.total and not self.attente:
            return self._donner(time.monotonic() + duree_s)
        futur = asyncio.get_running_loop().create_future()
        self.attente.append(futur)
        try:
            await asyncio.wait_for(asyncio.shield(futur), attente_s)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            # Parti en attendant : une place qui lui aurait été donnée est rendue.
            if futur.done() and not futur.cancelled():
                self.rendre(futur.result())
            futur.cancel()
            raise
        finally:
            if futur in self.attente:
                self.attente.remove(futur)
        if futur.done() and not futur.cancelled():
            # La place a été réservée pour nous par `rendre` : on y pose notre échéance.
            jeton = futur.result()
            self.echeances[jeton] = time.monotonic() + duree_s
            return jeton
        futur.cancel()
        return None

    def rendre(self, jeton: int) -> None:
        self.echeances.pop(jeton, None)
        while self.attente and len(self.echeances) < self.total:
            futur = self.attente.popleft()
            if not futur.done():
                futur.set_result(self._donner(time.monotonic() + 1))
                return


class Agent:
    def __init__(self, reglages: Reglages):
        self.r = reglages
        self.places = Places(reglages.places)
        self.empreinte = executor.empreinte()
        self.en_cours = 0

    async def _repondre(self, writer: asyncio.StreamWriter, objet: dict) -> None:
        writer.write((json.dumps(objet) + "\n").encode())
        await writer.drain()

    async def servir(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        pair = writer.get_extra_info("peername")
        try:
            try:
                brut = await asyncio.wait_for(reader.readline(), 10)
                entete = json.loads(brut[:MAX_ENTETE] or b"{}")
            except (asyncio.TimeoutError, ValueError, ConnectionError):
                return
            if not distant.secrets_egaux(str(entete.get("secret") or ""), self.r.secret):
                logger.warning("refus : secret invalide (de %s)", pair)
                await self._repondre(writer, {"ok": False, "code": "unauthorized",
                                              "message": "unauthorized"})
                return
            if entete.get("empreinte") != self.empreinte:
                await self._repondre(writer, {
                    "ok": False, "code": "version_mismatch",
                    "message": f"farm {self.empreinte}, server {entete.get('empreinte')}"})
                return
            script = SCRIPTS.get(str(entete.get("script")))
            if script is None:
                await self._repondre(writer, {"ok": False, "code": "bad_request",
                                              "message": "unknown script"})
                return
            try:
                duree = float(entete.get("duree_max_s") or 0)
            except (TypeError, ValueError):
                duree = 0
            duree = min(max(duree, 1.0), self.r.duree_max_s)
            try:
                racine = executor.repertoire()
                roues = executor.roues(racine, list(entete.get("requirements") or []))
            except ValueError as e:
                await self._repondre(writer, {"ok": False, "code": "bad_request",
                                              "message": str(e)})
                return
            except executor.BacASableAbsent as e:
                logger.error("bac à sable absent : %s", e)
                await self._repondre(writer, {"ok": False, "code": "unavailable",
                                              "message": "sandbox unavailable on the farm",
                                              "retry_after": 60})
                return
            jeton = await self.places.prendre(duree + MARGE_S, self.r.attente_s)
            if jeton is None:
                await self._repondre(writer, {
                    "ok": False, "code": "busy", "retry_after": self.places.retry_after(),
                    "message": f"all {self.places.total} places of the farm are taken"})
                return
            try:
                await self._executer(reader, writer, racine, script, roues, duree)
            finally:
                self.places.rendre(jeton)
        except Exception:  # noqa: BLE001 — une connexion qui casse n'arrête pas l'agent
            logger.exception("exécution interrompue (de %s)", pair)
        finally:
            try:
                writer.close()
            except (OSError, RuntimeError):
                pass

    async def _executer(self, reader, writer, racine, script, roues, duree) -> None:
        lance = executor.lancement(racine, script, duree)
        proc = await asyncio.create_subprocess_exec(
            *lance.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=lance.env, cwd=lance.cwd)
        self.en_cours += 1
        debut = time.monotonic()
        await self._repondre(writer, {"ok": True,
                                      "pyodide": str(executor.chemin_pyodide(racine)),
                                      "wheels": roues})
        ecriture = asyncio.Lock()

        async def trame(canal: bytes, donnees: bytes) -> None:
            async with ecriture:
                writer.write(distant.TRAME.pack(canal, len(donnees)) + donnees)
                await writer.drain()

        async def entree() -> None:
            # Trames `i` -> l'entrée du processus, `c` -> entrée fermée. La fin de la
            # connexion (sans trame) = le serveur est parti : le processus est tué.
            while True:
                try:
                    canal, longueur = distant.TRAME.unpack(
                        await reader.readexactly(distant.TRAME.size))
                    donnees = await reader.readexactly(longueur) if longueur else b""
                except (asyncio.IncompleteReadError, ConnectionError, OSError):
                    raise ConnectionResetError("connexion coupée par le serveur") from None
                if canal == b"i":
                    proc.stdin.write(donnees)
                    await proc.stdin.drain()
                elif canal == b"c":
                    proc.stdin.close()

        async def sortie(flux, canal: bytes) -> None:
            while bloc := await flux.read(_BLOC):
                await trame(canal, bloc)

        taches = [asyncio.ensure_future(entree()),
                  asyncio.ensure_future(sortie(proc.stdout, b"o")),
                  asyncio.ensure_future(sortie(proc.stderr, b"e"))]
        raison = "fin"
        sorties = asyncio.gather(taches[1], taches[2])
        try:
            # Les sorties vont à leur fin, OU l'entrée casse (connexion coupée) : le
            # premier des deux décide.
            fait, _ = await asyncio.wait({sorties, taches[0]}, timeout=duree + MARGE_S,
                                         return_when=asyncio.FIRST_COMPLETED)
            if not fait:
                raise asyncio.TimeoutError
            if taches[0] in fait and taches[0].exception() is not None:
                # Le serveur ferme dès qu'il a lu le résultat : si les sorties finissent
                # dans la seconde, c'est une fin normale, pas une coupure.
                fini, _ = await asyncio.wait({sorties}, timeout=1.0)
                if not fini:
                    raise ConnectionResetError
            await asyncio.wait_for(asyncio.shield(sorties), duree + MARGE_S)
            await asyncio.wait_for(proc.wait(), MARGE_S)
        except asyncio.TimeoutError:
            raison = "duree"
            logger.warning("processus tué : durée %.0f s dépassée", duree)
        except (ConnectionError, OSError):
            raison = "deconnexion"
        finally:
            if proc.returncode is None:
                proc.kill()
                await asyncio.to_thread(lance.arreter)
            for t in taches:
                t.cancel()
            await asyncio.gather(*taches, return_exceptions=True)
            sorties.cancel()
            await asyncio.gather(sorties, return_exceptions=True)
            await proc.wait()
            self.en_cours -= 1
            try:
                await trame(b"x", json.dumps({"returncode": proc.returncode,
                                              "raison": raison}).encode())
            except (ConnectionError, OSError, RuntimeError):
                pass
            logger.info("exécution : %s, code %s, %.1f s", raison, proc.returncode,
                        time.monotonic() - debut)


async def principal(reglages: Optional[Reglages] = None) -> None:
    reglages = reglages or Reglages.depuis_env()
    agent = Agent(reglages)
    executor.repertoire()   # un bac à sable incomplet refuse de démarrer ICI
    serveur = await asyncio.start_server(agent.servir, reglages.hote, reglages.port,
                                         limit=executor.MAX_REPONSE)
    logger.info("ferme de recettes : %s:%s, %d places, empreinte %s", reglages.hote,
                reglages.port, reglages.places, agent.empreinte)
    arret = asyncio.Event()
    boucle = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        boucle.add_signal_handler(sig, arret.set)
    async with serveur:
        await arret.wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(principal())
