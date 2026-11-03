"""Le pont : du code du bac à sable qui appelle des outils (#1202).

`executor.executer` joue une fonction en ALLER SIMPLE : la tâche entre, une réponse sort,
le code n'a rien pu demander. Ici le code peut, pendant qu'il tourne, demander un appel
d'outil (`await oto.call(outil, arguments)`) : le bac à sable écrit la demande sur une
ligne de sa sortie, le serveur la juge et l'exécute, puis répond sur l'entrée du
processus (`sandbox_pont.ts`, `harness_pont.pyodide`).

**Le bac à sable ne change pas** : ni réseau, ni secret, ni écriture, mêmes permissions
Deno et même isolement que l'aller simple (`executor.lancement`). Tout ce que le code
peut faire passe donc par le pont, et le pont ne fait qu'une chose : passer chaque
demande à `appeler`, la fonction que l'appelant lui donne et qui porte TOUTES les
gardes (quels outils, sous
quelle identité, dans quelle org — `recipes/pont.py` pour la lecture). Ce module ne
décide d'aucun droit ; il borne seulement l'exécution (`Budget`).

**Le budget** borne une exécution : nombre d'appels, unités FACTURÉES (ce que l'outil
déclare, `quantity`), durée. Atteint, le pont refuse les appels suivants (`ToolError`
côté code, code `budget_exhausted`) et le reçu dit lequel (`stopped` : `max_calls`,
`spend_cap`, `time_budget`). La durée laisse ensuite `GRACE_S` au code pour rendre son
reçu ; un code qui ne rend rien dans ce délai est tué, et l'exécution lève. Pas de reprise au
milieu d'une fonction : une relance repart du début, et une recette s'écrit donc avec des
écritures idempotentes (#1202).

**Des appels en parallèle sont servis en parallèle** : chaque demande est jugée par le
budget dès sa lecture (dans l'ordre d'arrivée, sans `await` entre le compte et la
décision : `max_calls` reste exact), puis jouée dans sa propre tâche, au plus
`Budget.en_vol` à la fois ; la boucle continue de lire pendant ce temps et chaque réponse
repart avec son `id` dès qu'elle est prête (`sandbox_pont.ts` les range par `id`). Servis
un par un jusqu'au 27/10/2026 : une recette qui lançait cinq recherches par
`asyncio.gather` les voyait passer l'une après l'autre (~11 s chacune) et dépassait sa
durée. Une tâche par appel, et pas un entrelacement dans la boucle : `executer_cible`
pose et retire des ContextVar par jeton, ce qui exige que chaque appel ait son contexte.
⚠️ Le budget se juge DEUX fois : à la lecture de la demande (son rang, `max_calls`), puis
quand l'appel obtient sa place (unités et durée, sans `await` jusqu'au départ) — une
demande qui a attendu en file derrière d'autres ne part pas sur un plafond que ces
autres ont épuisé, et sa place dans `calls` est rendue. La quantité n'étant connue
qu'après l'appel, le plafond d'unités peut être dépassé de ce que facturent les appels
déjà PARTIS : au plus `en_vol` appels, un seul quand le code appelle en série.

**Une fin en erreur n'abandonne pas ce qui est parti** : le code qui lève (un appel d'un
`asyncio.gather` a échoué) laisse des appels frères en vol, une écriture peut-être. Ceux
qui ont leur place vont à leur terme (bornés par la durée) ; seuls ceux encore en file
sont annulés, sans partir ni compter ; puis l'exécution lève.

**Rien n'attend un appel au-delà de sa borne** : une tâche annulée peut tenir jusqu'à la
fin de ce qu'elle attend (un fil protégé de l'annulation, un `finally` qui attend), et
Python n'interrompt pas un fil. `asyncio.wait_for` puis le `gather` de la fin attendaient
la tâche annulée sans délai ; l'appel échu est désormais ABANDONNÉ (annulé, pas attendu) et le code
reçoit tout de suite son refus de durée ; la fin attend au plus `GRACE_S` ce qui vole
encore, puis le journalise (outil, durée — jamais ses arguments) et l'abandonne. Le fil
finit seul ; sa ligne de `tool_calls` s'écrit quand il finit.

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

from . import executor, places

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
#: Les appels d'UNE exécution servis en même temps. Au-delà, une demande lue attend
#: qu'un appel finisse (son budget est déjà jugé, sa place dans l'ordre aussi).
EN_VOL = 5

#: `appeler(outil, arguments)` → `{ok, result?, code?, message?, quantity?}`. Ne lève
#: pas : un refus ou un échec est une réponse `ok: false` avec son `code`.
Appeler = Callable[[str, dict], Awaitable[dict]]

#: Les appels ABANDONNÉS de toutes les exécutions, tenus jusqu'à leur fin : la boucle ne
#: garde qu'une référence faible sur une tâche, et une tâche abandonnée dont le fil tourne
#: encore ne doit pas être ramassée en vol (elle écrit sa ligne de `tool_calls` en finissant).
_ORPHELINS: set[asyncio.Task] = set()


@dataclass(frozen=True)
class Budget:
    """Les bornes d'UNE exécution. Chiffres provisoires, comme ceux de l'aller simple."""
    appels: Optional[int] = 100   # None = pas de borne en nombre d'appels
    unites: float = 1000.0
    delai_s: float = 120.0
    en_vol: int = EN_VOL          # appels servis en même temps (au moins 1)


async def executer_avec_pont(*, sources: dict, entrypoint: str, appeler: Appeler,
                             sub: Optional[str], budget: Budget = Budget(),
                             requirements: tuple = (),
                             entree: Optional[dict] = None) -> dict:
    """Joue `entrypoint(oto, entree)` dans le bac à sable, en servant ses appels
    d'outils par `appeler`. Rend `{ok, result | error, calls, units, refused, stopped,
    journal, duree_ms}`. Lève `executor.ExecutionEchouee` si le bac à sable n'a rien
    rendu d'exploitable (délai, plantage, protocole), `executor.BacASableAbsent` si
    l'instance n'en a pas, `executor.BacASableOccupe` si `sub` ou le processus n'a plus
    de place (`places.py`) — la place est tenue jusqu'à la mort du processus, annulation
    comprise."""
    with places.place(sub):
        return await _executer_avec_pont(sources=sources, entrypoint=entrypoint,
                                         appeler=appeler, budget=budget,
                                         requirements=requirements, entree=entree)


async def _executer_avec_pont(*, sources: dict, entrypoint: str, appeler: Appeler,
                              budget: Budget, requirements: tuple,
                              entree: Optional[dict]) -> dict:
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
    lance = executor.lancement(racine, _SCRIPT, budget.delai_s + GRACE_S)
    proc = await asyncio.create_subprocess_exec(
        *lance.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=lance.env, cwd=lance.cwd,
        limit=executor.MAX_REPONSE)
    # Vidée en parallèle, BORNÉE : un processus qui remplit sa sortie d'erreur bloquerait
    # sinon, et la lire en entier la garderait toute en mémoire.
    erreurs = asyncio.ensure_future(_queue_erreurs(proc.stderr))
    # Les appels en vol, leurs places, et UN écrivain à la fois sur l'entrée du processus.
    en_vol: set[asyncio.Task] = set()
    partis: set[asyncio.Task] = set()   # ceux qui ont leur place : l'appel est parti
    # Les appels ABANDONNÉS (échus ou annulés en vol), avec (outil, début) : la fin les
    # attend au plus `GRACE_S`, puis journalise ceux qui tiennent encore.
    abandons: dict[asyncio.Task, tuple[str, float]] = {}
    places = asyncio.Semaphore(max(1, budget.en_vol))
    ecriture = asyncio.Lock()
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
            except ValueError:
                # `readline` au-delà de `limit` : une ligne plus longue que le plafond.
                raise executor.ReponseTropLongue(
                    f"ligne de plus de {executor.MAX_REPONSE} octets : processus "
                    "arrêté") from None
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
                # Jugée ICI, dans l'ordre de lecture : le compte et la décision ne sont
                # séparés par aucun `await`, donc deux demandes ne passent pas sur la
                # même place du budget. Seul l'appel admis part dans sa tâche.
                refus = _admettre(message, budget, recu, fin)
                if refus is not None:
                    await _repondre(proc, ecriture, message.get("id"), refus)
                    continue
                tache_appel = asyncio.create_task(_servir(
                    proc, ecriture, places, message, appeler, recu, fin, budget, partis,
                    abandons))
                en_vol.add(tache_appel)
                tache_appel.add_done_callback(en_vol.discard)
                tache_appel.add_done_callback(partis.discard)
                continue
            if message.get("type") == "done":
                if not message.get("ok"):
                    # Ce qui attend encore sa place ne part pas ; ce qui est parti (une
                    # écriture peut-être) va à son terme avant que l'exécution lève.
                    for tache_appel in en_vol - partis:
                        tache_appel.cancel()
                # Un appel lancé et jamais attendu par le code va à son terme, comme
                # quand le serveur servait en série : le reçu compte ce qu'il a facturé.
                # Un appel ABANDONNÉ n'est pas dans `en_vol` : son refus est rendu, rien
                # ne l'attend ici (la fin lui laisse `GRACE_S`).
                if en_vol:
                    await asyncio.wait(set(en_vol),
                                       timeout=max(0.1, fin_dure - time.monotonic()))
                if not message.get("ok"):
                    raise executor.ExecutionEchouee(message.get("erreur") or "échec sans message")
                sortie = dict(message.get("sortie") or {})
                return {**sortie, **recu, "journal": message.get("journal") or "",
                        "duree_ms": int((time.monotonic() - debut) * 1000)}
    finally:
        # Ce qui vole encore (délai, plantage, fin anticipée) est annulé AVANT de
        # fermer l'entrée : une réponse n'est jamais écrite dans un tube fermé.
        # L'attente est BORNÉE (`GRACE_S`) : un appel dont l'outil tourne dans un fil ne
        # se laisse pas annuler, et l'attendre sans délai tenait la recette autant que
        # l'outil. Un appel annulé ici abandonne son outil dans `abandons` en traitant son
        # annulation : l'ensemble attendu se relit à chaque tour.
        for tache_appel in list(en_vol):
            tache_appel.cancel()
        echeance = time.monotonic() + GRACE_S
        while (en_vol or abandons) and (reste := echeance - time.monotonic()) > 0:
            await asyncio.wait(en_vol | set(abandons), timeout=reste,
                               return_when=asyncio.FIRST_COMPLETED)
        _abandonner_ce_qui_vole(en_vol, abandons)
        if proc.returncode is None:
            proc.kill()
            await asyncio.to_thread(lance.arreter)
        await proc.wait()
        # Fermés sur la boucle, pas laissés au ramasse-miettes (qui les fermerait après
        # elle) : l'entrée, puis la sortie d'erreur attendue jusqu'à sa fin — un processus
        # isolé peut la tenir un instant après la fin de celui qu'on a lancé.
        proc.stdin.close()
        await _fin_erreurs(erreurs)


def _abandonner(tache: asyncio.Task, outil: str, debut: float, abandons: dict) -> None:
    """Annule l'appel d'outil SANS l'attendre et le range parmi les abandons de
    l'exécution (journalisés s'ils tiennent à la fin) et du processus (`_ORPHELINS`)."""
    tache.cancel()
    if tache.done():
        return
    abandons[tache] = (outil, debut)
    tache.add_done_callback(lambda t: abandons.pop(t, None))
    _ORPHELINS.add(tache)
    tache.add_done_callback(_ORPHELINS.discard)


def _abandonner_ce_qui_vole(en_vol: set, abandons: dict) -> None:
    """Ce qui vole encore après `GRACE_S` est laissé à lui-même : journalisé (outil,
    durée — jamais les arguments, qui portent des données), jamais attendu."""
    maintenant = time.monotonic()
    for tache, (outil, debut) in list(abandons.items()):
        if not tache.done():
            logger.warning("pont : l'appel `%s` vole encore après %.0f s — abandonné, son "
                           "fil finira seul", outil, maintenant - debut)
    for tache in en_vol:
        if not tache.done():
            logger.warning("pont : la réponse de `%s` n'est pas partie après %.0f s de "
                           "grâce — abandonnée", tache.get_name(), GRACE_S)
            _ORPHELINS.add(tache)
            tache.add_done_callback(_ORPHELINS.discard)


async def _envoyer(proc, objet: dict) -> None:
    proc.stdin.write((json.dumps(objet) + "\n").encode())
    await proc.stdin.drain()


async def _queue_erreurs(flux) -> bytes:
    """La FIN de la sortie d'erreur (`executor.QUEUE_ERREURS` octets), lue jusqu'au bout."""
    queue = bytearray()
    while bloc := await flux.read(64 * 1024):
        queue.extend(bloc)
        del queue[:-executor.QUEUE_ERREURS]
    return bytes(queue)


async def _fin_erreurs(erreurs) -> str:
    try:
        return (await asyncio.wait_for(erreurs, 2)).decode(errors="replace")
    except asyncio.TimeoutError:
        return ""


async def _repondre(proc, ecriture: asyncio.Lock, id_appel, reponse: dict) -> None:
    """La réponse d'UN appel, une ligne entière à la fois (`ecriture`). Un processus
    parti (fin, plantage, délai) ne la lit plus : la boucle le constate sur sa sortie."""
    try:
        async with ecriture:
            await _envoyer(proc, {"id": id_appel, **reponse})
    except (BrokenPipeError, ConnectionResetError):
        pass


def _admettre(message: dict, budget: Budget, recu: dict, fin: float) -> Optional[dict]:
    """Le jugement d'UNE demande par le budget, SANS `await` : le refus à rendre, ou
    `None` — l'appel est admis et déjà compté dans `calls`."""
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
    return None


def _rejuger(budget: Budget, recu: dict, fin: float) -> Optional[dict]:
    """Le budget rejugé quand l'appel admis obtient sa place, SANS `await` : les unités
    et la durée ont pu s'épuiser pendant qu'il attendait. Refusé, il rend sa place dans
    `calls` (il n'est pas parti) et compte parmi les refus."""
    borne = ("spend_cap" if recu["units"] >= budget.unites else
             "time_budget" if time.monotonic() >= fin else None)
    if borne is None:
        return None
    recu["calls"] -= 1
    recu["refused"] += 1
    recu["stopped"] = recu["stopped"] or borne
    return {"ok": False, "code": "budget_exhausted",
            "message": f"This run's budget is spent ({borne}). Nothing was called."}


async def _servir(proc, ecriture: asyncio.Lock, places: asyncio.Semaphore, message: dict,
                  appeler: Appeler, recu: dict, fin: float, budget: Budget,
                  partis: set, abandons: dict) -> None:
    """UN appel ADMIS, dans sa tâche : il attend sa place, est rejugé, joue, répond.
    Annulé avant d'être parti (fin du code en erreur), il rend sa place dans `calls`."""
    parti = False
    try:
        async with places:
            reponse = _rejuger(budget, recu, fin)
            if reponse is None:
                parti = True
                partis.add(asyncio.current_task())
                reponse = await _jouer(message["tool"], dict(message["args"]), appeler,
                                       recu, fin, abandons)
    except asyncio.CancelledError:
        if not parti:
            recu["calls"] -= 1
        raise
    except Exception as e:  # noqa: BLE001 — une tâche qui lève laisserait le code sans réponse
        logger.exception("pont : l'appel `%s` a levé hors d'`appeler`", message.get("tool"))
        reponse = {"ok": False, "code": "bridge_error", "message": type(e).__name__}
    await _repondre(proc, ecriture, message.get("id"), reponse)


async def _jouer(outil: str, arguments: dict, appeler: Appeler, recu: dict,
                 fin: float, abandons: dict) -> dict:
    """L'appel lui-même, passé à `appeler`, borné par ce qui reste de la durée.

    Dans SA tâche, attendue par `asyncio.wait` et non `wait_for` : à l'échéance,
    `wait_for` annule PUIS attend la tâche, sans délai si elle tarde à céder. Ici l'appel
    échu est abandonné (`_abandonner`) et le refus
    de durée part aussitôt ; annulé de l'extérieur (la fin de l'exécution), de même."""
    debut = time.monotonic()
    appel = asyncio.ensure_future(appeler(outil, dict(arguments)))
    try:
        fait, _ = await asyncio.wait({appel}, timeout=max(0.1, fin - time.monotonic()))
    except asyncio.CancelledError:
        _abandonner(appel, outil, debut, abandons)
        raise
    if not fait:
        _abandonner(appel, outil, debut, abandons)
        recu["stopped"] = recu["stopped"] or "time_budget"
        return {"ok": False, "code": "budget_exhausted",
                "message": "This run's time budget ran out during the call."}
    try:
        reponse = appel.result()
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
