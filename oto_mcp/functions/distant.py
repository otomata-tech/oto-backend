"""Le bac à sable DISTANT : le processus Deno tourne sur la ferme de recettes (28/11/2026).

Par défaut le bac à sable tourne ICI, sur la box de l'API (`executor`, `pont`). Avec
`OTO_FUNCTIONS_FERME` posée (`hôte:port`), seul le PROCESSUS part sur une box dédiée
(`ferme_recettes/agent.py`, réseau privé) : le serveur garde le pont, le budget et TOUTES
les gardes des appels d'outils (`recipes/pont.py`) — la box n'a aucun secret (ni base, ni
coffre), elle exécute du code de membre et rien d'autre. Une recette lourde ne pèse plus
sur l'API, et la capacité se gagne en ajoutant de la ferme, pas en élargissant l'API.

**Le protocole**, sur une connexion TCP par exécution :

1. le serveur écrit UNE ligne JSON — `secret` (comparé en temps constant par l'agent),
   `empreinte` (`executor.empreinte()` : Pyodide, verrou Deno, scripts et harnais —
   l'agent refuse une empreinte différente de la sienne), `script` (`aller` | `pont`),
   `requirements` (les dépendances de la liste blanche), `duree_max_s` ;
2. l'agent attend une place (file FIFO bornée), puis répond UNE ligne JSON :
   `{"ok": true, "pyodide": …, "wheels": […]}` — les chemins chez LUI, que le serveur met
   dans la tâche — ou `{"ok": false, "code": …, "message": …, "retry_after": …}` ;
3. ensuite, dans les deux sens, des trames `canal (1 octet) + longueur (4 octets) +
   données`. Vers l'agent : `i` (l'entrée du processus), `c` (entrée fermée). Vers le
   serveur : `o` (sortie), `e` (sortie d'erreur), `x` (fin : JSON `{"returncode": …}`).
   Fermer l'entrée est une TRAME, jamais une demi-fermeture : la fin de la connexion
   veut toujours dire « coupée », et l'agent tue le processus tout de suite.

Une connexion coupée tue le processus côté agent ; l'agent le tue aussi à sa propre durée
maximale. **Avant le départ** (ferme pleine, injoignable, empreinte différente) : le même
`BacASableOccupe` qu'en local — 429 `sandbox_busy` avec `Retry-After`, donc une recette
programmée est remise en file (phase 0), jamais tuée. **Après le départ** : une mort est
un échec, jamais rejouée.

Les places de `places.py` restent prises ici AVANT la ferme : elles portent la part
équitable par compte ; c'est l'agent qui fait foi pour la capacité de la box
(`OTO_FUNCTIONS_MAX_SIMULTANEES` se règle alors au nombre de places de la ferme).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import struct
from typing import Optional

from . import places

logger = logging.getLogger(__name__)

ENV_FERME = "OTO_FUNCTIONS_FERME"
ENV_SECRET = "OTO_FUNCTIONS_FERME_SECRET"
#: Ce qu'un refus conseille d'attendre quand la ferme ne répond pas.
ATTENTE_INJOIGNABLE_S = 30
#: Ce qu'un refus conseille d'attendre quand les empreintes diffèrent : un déploiement
#: en cours (la box suit, ou l'API suit) ; durable, la remise en file le borne.
ATTENTE_EMPREINTE_S = 60
#: Le temps laissé à l'ouverture de la connexion.
CONNEXION_S = 5.0
#: L'en-tête d'une trame : le canal, puis la longueur des données.
TRAME = struct.Struct("!cI")


def adresse() -> Optional[tuple[str, int]]:
    """`(hôte, port)` de la ferme, ou None : le bac à sable reste local (le défaut)."""
    brut = (os.environ.get("OTO_FUNCTIONS_FERME") or "").strip()
    if not brut:
        return None
    hote, _, port = brut.rpartition(":")
    if not hote or not port.isdigit():
        raise ValueError(f"{ENV_FERME}={brut!r} illisible : `hôte:port`.")
    return hote.strip("[]"), int(port)


def actif() -> bool:
    """La ferme est-elle déclarée ? (sinon : bac à sable local, comme avant)."""
    return adresse() is not None


def _secret() -> str:
    secret = os.environ.get("OTO_FUNCTIONS_FERME_SECRET") or ""
    if not secret:
        raise ValueError(f"{ENV_FERME} est posée sans {ENV_SECRET} : la ferme refuserait "
                         "tout appel.")
    return secret


def secrets_egaux(a: str, b: str) -> bool:
    """Comparaison en temps constant (partagée avec l'agent)."""
    return hmac.compare_digest(a.encode(), b.encode())


class _Ecrivain:
    """L'entrée du processus distant, avec la surface que le pont utilise."""

    def __init__(self, writer: asyncio.StreamWriter):
        self._w = writer

    def write(self, data: bytes) -> None:
        self._w.write(TRAME.pack(b"i", len(data)) + data)

    async def drain(self) -> None:
        await self._w.drain()

    def close(self) -> None:
        # L'entrée se ferme par une trame : une connexion fermée veut dire « coupée ».
        try:
            if not self._w.is_closing():
                self._w.write(TRAME.pack(b"c", 0))
        except (OSError, RuntimeError):
            pass


class ProcessusDistant:
    """Un processus du bac à sable sur la ferme, vu comme `asyncio.subprocess.Process`
    par le pont : `stdin` (write/drain/close), `stdout` et `stderr` (StreamReader),
    `returncode`, `kill()`, `wait()`."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 limite: int):
        self._reader, self._writer = reader, writer
        self.stdin = _Ecrivain(writer)
        self.stdout = asyncio.StreamReader(limit=limite)
        self.stderr = asyncio.StreamReader(limit=limite)
        self.returncode: Optional[int] = None
        self._fin = asyncio.Event()
        self._tache = asyncio.ensure_future(self._lire())

    async def _lire(self) -> None:
        try:
            while True:
                tete = await self._reader.readexactly(TRAME.size)
                canal, longueur = TRAME.unpack(tete)
                donnees = await self._reader.readexactly(longueur) if longueur else b""
                if canal == b"o":
                    self.stdout.feed_data(donnees)
                elif canal == b"e":
                    self.stderr.feed_data(donnees)
                elif canal == b"x":
                    try:
                        self.returncode = int(json.loads(donnees).get("returncode", -1))
                    except (ValueError, AttributeError):
                        self.returncode = -1
                    break
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            # Connexion perdue : le processus est mort côté agent (il le tue).
            if self.returncode is None:
                self.returncode = -1
        finally:
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._fin.set()
            self._fermer()

    def _fermer(self) -> None:
        try:
            self._writer.close()
        except (OSError, RuntimeError):
            pass

    def kill(self) -> None:
        """Couper la connexion : l'agent tue le processus."""
        if self.returncode is None:
            self.returncode = -9
        self._tache.cancel()
        self._fermer()

    async def wait(self) -> int:
        try:
            await self._tache
        except asyncio.CancelledError:
            pass
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        return self.returncode if self.returncode is not None else -1


class Ouverture:
    """Ce que la ferme rend à l'ouverture : le processus et les chemins chez elle."""

    def __init__(self, proc: ProcessusDistant, pyodide: str, wheels: list[str]):
        self.proc, self.pyodide, self.wheels = proc, pyodide, wheels


def _occupe(message: str, attente: int) -> places.BacASableOccupe:
    return places.BacASableOccupe(f"sandbox_busy: {message}", retry_after=max(1, attente))


async def ouvrir(*, script: str, requirements: list[str], duree_max_s: float,
                 empreinte: str, limite: int) -> Ouverture:
    """Ouvre une exécution sur la ferme. Lève `BacASableOccupe` avant tout départ (ferme
    pleine, injoignable, empreinte différente), `ValueError` si la demande est refusée
    comme invalide (une dépendance hors liste blanche, la même erreur qu'en local)."""
    hote, port = adresse()  # type: ignore[misc]
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(hote, port, limit=limite), CONNEXION_S)
    except (OSError, asyncio.TimeoutError) as e:
        logger.warning("ferme de recettes injoignable (%s:%s) : %s", hote, port, e)
        raise _occupe(f"the recipe farm is unreachable ({type(e).__name__}); retry in "
                      f"{ATTENTE_INJOIGNABLE_S} s.", ATTENTE_INJOIGNABLE_S) from None
    entete = {"secret": _secret(), "empreinte": empreinte, "script": script,
              "requirements": list(requirements), "duree_max_s": float(duree_max_s)}
    try:
        writer.write((json.dumps(entete) + "\n").encode())
        await writer.drain()
        # L'agent peut faire attendre une place : sa propre borne, plus une marge.
        brut = await asyncio.wait_for(reader.readline(), 120)
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
        writer.close()
        raise _occupe(f"the recipe farm did not answer ({type(e).__name__}); retry in "
                      f"{ATTENTE_INJOIGNABLE_S} s.", ATTENTE_INJOIGNABLE_S) from None
    try:
        reponse = json.loads(brut or b"{}")
    except ValueError:
        reponse = {}
    if not reponse.get("ok"):
        writer.close()
        code = reponse.get("code") or "no_answer"
        message = reponse.get("message") or code
        if code == "bad_request":
            raise ValueError(message)
        if code == "version_mismatch":
            logger.warning("ferme de recettes : empreinte différente (%s)", message)
            raise _occupe(f"the recipe farm runs another sandbox version ({message}); "
                          f"retry in {ATTENTE_EMPREINTE_S} s.", ATTENTE_EMPREINTE_S)
        if code == "unauthorized":
            logger.error("ferme de recettes : secret refusé")
        attente = int(reponse.get("retry_after") or ATTENTE_INJOIGNABLE_S)
        raise _occupe(f"the recipe farm refused ({code}: {message}); retry in "
                      f"{attente} s.", attente)
    proc = ProcessusDistant(reader, writer, limite)
    return Ouverture(proc, str(reponse.get("pyodide") or ""),
                     [str(w) for w in reponse.get("wheels") or []])


def jouer_aller(*, requirements: list[str], tache, duree_max_s: float, empreinte: str,
                limite_sortie: int, queue_erreurs: int) -> tuple[int, bytes, bytes]:
    """L'aller simple (`executor.executer`) sur la ferme, depuis un FIL (l'appelant joue
    dans `run_in_threadpool`) : sa propre boucle. `tache(pyodide, wheels)` rend la tâche
    à envoyer. Rend `(code, sortie, erreurs)` comme `executor._jouer` ; lève
    `BacASableOccupe` avant le départ, `TimeoutError` au-delà de la durée,
    `OverflowError` si la sortie dépasse `limite_sortie`."""
    return asyncio.run(_aller(requirements, tache, duree_max_s, empreinte, limite_sortie,
                              queue_erreurs))


async def _aller(requirements, tache, duree_max_s, empreinte, limite_sortie,
                 queue_erreurs):
    ouverture = await ouvrir(script="aller", requirements=requirements,
                             duree_max_s=duree_max_s, empreinte=empreinte,
                             limite=limite_sortie + 1)
    proc = ouverture.proc
    sortie, erreurs = bytearray(), bytearray()
    try:
        proc.stdin.write(json.dumps(tache(ouverture.pyodide, ouverture.wheels)).encode())
        await proc.stdin.drain()
        proc.stdin.close()

        async def _sortie():
            while bloc := await proc.stdout.read(64 * 1024):
                sortie.extend(bloc)
                if len(sortie) > limite_sortie:
                    raise OverflowError

        async def _erreurs():
            while bloc := await proc.stderr.read(64 * 1024):
                erreurs.extend(bloc)
                del erreurs[:-queue_erreurs]

        await asyncio.wait_for(asyncio.gather(_sortie(), _erreurs()), duree_max_s)
        code = await proc.wait()
    except asyncio.TimeoutError:
        raise TimeoutError from None
    finally:
        proc.kill()
        await proc.wait()
    return code, bytes(sortie), bytes(erreurs)
