"""Le pont : du code du bac à sable qui appelle des outils (#1202).

`executor.executer` joue une fonction en ALLER SIMPLE : la tâche entre, une réponse sort,
le code n'a rien pu demander. Ici le code peut, pendant qu'il tourne, demander un appel
d'outil (`await oto.call(outil, arguments)`) : le bac à sable écrit la demande sur une
ligne de sa sortie, le serveur la juge et l'exécute, puis répond sur l'entrée du
processus (`sandbox_pont.ts`, `harness_pont.pyodide`).

**Le bac à sable ne change pas** : ni réseau, ni secret, ni écriture, mêmes permissions
Deno que l'aller simple (`executor.commande`). Tout ce que le code peut faire passe donc
par le pont, et le pont ne fait qu'une chose : passer chaque demande à `appeler`, la
fonction que l'appelant lui donne et qui porte TOUTES les gardes (quels outils, sous
quelle identité, dans quelle org — `recipes/pont.py` pour la lecture). Ce module ne
décide d'aucun droit ; il borne seulement l'exécution (`Budget`).

**Le budget** borne une exécution : nombre d'appels, unités FACTURÉES (ce que l'outil
déclare, `quantity`), durée. Atteint, le pont refuse les appels suivants (`ToolError`
côté code, code `budget_exhausted`) et le reçu dit lequel (`stopped` : `max_calls`,
`spend_cap`, `time_budget`). La durée laisse ensuite `GRACE_S` au code pour rendre son
reçu ; un code qui ne rend rien dans ce délai est tué, et l'exécution lève. Pas de reprise au
milieu d'une fonction : une relance repart du début, et une recette s'écrit donc avec des
écritures idempotentes (#1202).

**Limite connue** : le serveur sert les appels un par un (il lit une ligne, attend
`appeler`, répond). Des appels lancés en parallèle par le code (`asyncio.gather`) sont
servis dans l'ordre ; s'ils sont très nombreux pendant un appel lent, la sortie du
processus peut se remplir et le code attendre. À reprendre quand une recette en aura besoin.

**Pas de SQL ici** et rien de bloquant : un sous-processus asyncio, lu ligne à ligne sur
la boucle. `appeler` est une coroutine (`meta.executer_cible` l'est), qui joue elle-même
hors de la boucle ce qui doit l'être.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from . import executor

logger = logging.getLogger(__name__)

_ICI = Path(__file__).resolve().parent
_SCRIPT = _ICI / "sandbox_pont.ts"
_HARNAIS = _ICI / "harness_pont.pyodide"

#: Ce qu'un résultat d'outil peut peser en entrant dans le bac à sable. Jugé APRÈS
#: l'appel : l'outil a été appelé (et facturé), le code reçoit `result_too_large`.
MAX_RESULTAT = 5 * 1024 * 1024
#: Le temps laissé au code, une fois son budget de durée épuisé, pour rendre son reçu :
#: les appels y sont refusés (`time_budget`), seule la fin est attendue. Au-delà, le
#: processus est tué et l'exécution lève — c'est la seule borne qui finit en exception.
GRACE_S = 5.0

#: `appeler(outil, arguments)` → `{ok, result?, code?, message?, quantity?}`. Ne lève
#: pas : un refus ou un échec est une réponse `ok: false` avec son `code`.
Appeler = Callable[[str, dict], Awaitable[dict]]


@dataclass(frozen=True)
class Budget:
    """Les bornes d'UNE exécution. Chiffres provisoires, comme ceux de l'aller simple."""
    appels: Optional[int] = 100   # None = pas de borne en nombre d'appels
    unites: float = 1000.0
    delai_s: float = 120.0


async def executer_avec_pont(*, sources: dict, entrypoint: str, appeler: Appeler,
                             budget: Budget = Budget(), requirements: tuple = (),
                             entree: Optional[dict] = None) -> dict:
    """Joue `entrypoint(oto, entree)` dans le bac à sable, en servant ses appels
    d'outils par `appeler`. Rend `{ok, result | error, calls, units, refused, stopped,
    journal, duree_ms}`. Lève `executor.ExecutionEchouee` si le bac à sable n'a rien
    rendu d'exploitable (délai, plantage, protocole), `executor.BacASableAbsent` si
    l'instance n'en a pas."""
    racine = executor.repertoire()
    roues = executor.roues(racine, list(requirements))
    nonce = secrets.token_hex(16)
    tache = {
        "pyodide": str(executor.chemin_pyodide(racine)), "wheels": roues, "nonce": nonce,
        "harness": _HARNAIS.read_text(encoding="utf-8"),
        "job": {"sources": sources, "entrypoint": entrypoint,
                "input": entree if entree is not None else {}},
    }
    recu = {"calls": 0, "units": 0.0, "refused": 0, "stopped": None}
    debut = time.monotonic()
    # Deux échéances : celle du budget (au-delà, plus d'appel), et celle où l'on cesse
    # d'attendre la fin du code (`GRACE_S` plus tard) pour qu'il puisse rendre son reçu.
    fin = debut + budget.delai_s
    fin_dure = fin + GRACE_S
    proc = await asyncio.create_subprocess_exec(
        *executor.commande(racine, _SCRIPT), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=executor.environnement(racine), cwd=str(racine), limit=executor.MAX_REPONSE)
    # Vidée en parallèle : un processus qui remplit sa sortie d'erreur bloquerait sinon.
    erreurs = asyncio.ensure_future(proc.stderr.read())
    try:
        await _envoyer(proc, tache)
        while True:
            reste = fin_dure - time.monotonic()
            if reste <= 0:
                raise executor.ExecutionEchouee(f"délai dépassé ({budget.delai_s:g} s)")
            try:
                brut = await asyncio.wait_for(proc.stdout.readline(), reste)
            except asyncio.TimeoutError:
                raise executor.ExecutionEchouee(
                    f"délai dépassé ({budget.delai_s:g} s)") from None
            if not brut:
                raise executor.ExecutionEchouee(
                    f"le bac à sable n'a rendu aucune réponse : "
                    f"{(await _fin_erreurs(erreurs))[-1500:]}")
            ligne = brut.decode("utf-8", errors="replace").rstrip("\n")
            # Une ligne sans le nonce vient du code, pas du bac à sable : ignorée.
            if not ligne.startswith(nonce):
                continue
            message = json.loads(ligne[len(nonce):])
            if message.get("type") == "call":
                reponse = await _servir(message, appeler, budget, recu, fin)
                await _envoyer(proc, {"id": message.get("id"), **reponse})
                continue
            if message.get("type") == "done":
                if not message.get("ok"):
                    raise executor.ExecutionEchouee(message.get("erreur") or "échec sans message")
                sortie = dict(message.get("sortie") or {})
                return {**sortie, **recu, "journal": message.get("journal") or "",
                        "duree_ms": int((time.monotonic() - debut) * 1000)}
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        erreurs.cancel()


async def _envoyer(proc, objet: dict) -> None:
    proc.stdin.write((json.dumps(objet) + "\n").encode())
    await proc.stdin.drain()


async def _fin_erreurs(erreurs) -> str:
    try:
        return (await asyncio.wait_for(erreurs, 2)).decode(errors="replace")
    except asyncio.TimeoutError:
        return ""


async def _servir(message: dict, appeler: Appeler, budget: Budget, recu: dict,
                  fin: float) -> dict:
    """UNE demande du code : bornée par le budget, puis passée à `appeler`."""
    outil, arguments = message.get("tool"), message.get("args")
    if not isinstance(outil, str) or not isinstance(arguments, dict):
        recu["refused"] += 1
        return {"ok": False, "code": "invalid_call",
                "message": "`tool` is a name and `arguments` a dict."}
    borne = ("max_calls" if budget.appels is not None and recu["calls"] >= budget.appels else
             "spend_cap" if recu["units"] >= budget.unites else
             "time_budget" if time.monotonic() >= fin else None)
    if borne:
        recu["refused"] += 1
        recu["stopped"] = recu["stopped"] or borne
        return {"ok": False, "code": "budget_exhausted",
                "message": f"This run's budget is spent ({borne}). Nothing was called."}
    recu["calls"] += 1
    try:
        reponse = await asyncio.wait_for(appeler(outil, dict(arguments)),
                                         max(0.1, fin - time.monotonic()))
    except asyncio.TimeoutError:
        recu["stopped"] = recu["stopped"] or "time_budget"
        return {"ok": False, "code": "budget_exhausted",
                "message": "This run's time budget ran out during the call."}
    except Exception as e:
        # `appeler` ne doit pas lever ; s'il le fait, c'est un défaut du serveur, pas du
        # code : journalisé, et le code reçoit un refus nommé plutôt qu'un pont coupé.
        logger.exception("pont : `%s` a levé au lieu de rendre une réponse", outil)
        return {"ok": False, "code": "bridge_error", "message": type(e).__name__}
    quantite = reponse.get("quantity")
    if isinstance(quantite, (int, float)) and not isinstance(quantite, bool):
        recu["units"] += float(quantite)
    sortie = {k: reponse[k] for k in ("ok", "result", "code", "message") if k in reponse}
    if len(json.dumps(sortie.get("result"))) > MAX_RESULTAT:
        return {"ok": False, "code": "result_too_large",
                "message": f"The tool's result is over {MAX_RESULTAT} bytes: ask for a "
                           "smaller page."}
    return sortie
