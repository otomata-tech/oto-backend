"""The loop of the in-app agent: model turn → tools → model turn …, until it answers,
pauses on the person (an approval card) or on the page (a client tool), or hits the step
cap. Each leg is an async generator of protocol events (the front's `CopilotEvent`); the
conversation's state is written back to the base after every step, so a leg that dies
(a restart, a closed tab) leaves a conversation that resumes.
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import AsyncIterator, Optional

import anyio
from starlette.concurrency import run_in_threadpool

from ... import geste
from ...db import copilot as store
from . import annulation, carte, classement, cle as cles, depense, modele, pieces
from .outils import Boite, executer
from .texte import FiltreSuite, couper

logger = logging.getLogger(__name__)

MAX_ETAPES = 30
#: How long one step may hold the conversation's lock before another leg may take it.
VERROU_S = 900

ECHEC_ARRETE = "Interrupted: the person sent a new message instead."
#: To the model, for each tool call its own output cut short: nothing ran, write less.
COUPE = ("Your output reached its length limit before this call was complete, so it did "
         "not run. Make the change in smaller calls: never resend a long text whole — "
         "for an app, `oto_apps` op=patch with only the lines that change (exact find → "
         "replace); for a doc or a guide, op=patch on one section.")
#: To the person, when the model was cut again — or mid-answer.
COUPE_ECHEC = ("The assistant's reply was too long to finish. Ask for a smaller change, or "
               "split it into steps.")
REFUS = "The person declined this action. Do not retry it; ask what they want instead."


def ev(type_: str, **champs) -> dict:
    return {"type": type_, **{k: v for k, v in champs.items() if v is not None}}


async def sauver(fil: dict) -> None:
    await run_in_threadpool(store.enregistrer, fil["id"], {
        k: fil[k] for k in (*store.CHAMPS_JSON, "project_id", "run_id", "model_calls", "mode")})


def regler_interrompus(fil: dict, raison: str) -> None:
    """A leg that ended mid-way (Stop, a dropped stream, a card left open) leaves
    `tool_use` blocks without results: the API refuses that history. Each one is
    answered as interrupted before anything else is sent — appended, never edited."""
    dernier = fil["messages"][-1] if fil["messages"] else None
    if dernier and dernier.get("role") == "assistant" and isinstance(dernier.get("content"), list):
        repondus = {r.get("tool_use_id") for r in fil["results"]}
        resultats = list(fil["results"])
        for bloc in dernier["content"]:
            if bloc.get("type") == "tool_use" and bloc.get("id") not in repondus:
                resultats.append({"type": "tool_result", "tool_use_id": bloc["id"],
                                  "content": raison, "is_error": True})
        if resultats:
            fil["messages"].append({"role": "user", "content": resultats})
    fil["queue"], fil["results"], fil["pending"] = [], [], None


def _resultat(bloc_id: str, contenu: str, erreur: bool) -> dict:
    return {"type": "tool_result", "tool_use_id": bloc_id, "content": contenu,
            "is_error": bool(erreur)}


def noms_client(fil: dict) -> set:
    return {t.get("name") for t in fil.get("client_tools") or []}


async def _lancer_outil(fil: dict, b: Boite, bloc: dict) -> AsyncIterator[dict]:
    """Runs one server tool call: `tool_call`, then `tool_result`; appends the model's
    `tool_result` to the conversation."""
    name, args = bloc["name"], bloc.get("input") or {}
    target, targs = classement.cible(name, args)
    sorte = classement.classer(target, targs, b.annotations(target))
    yield ev("tool_call", id=bloc["id"], name=name, label=carte.libelle(name, args),
             write=sorte != classement.LECTURE)
    gid = geste.nouvel_identifiant()
    prep = None
    #: An Undo the card promised (an undoable write) but that could not be prepared:
    #: the call still runs — the person approved IT —, and the step says it has none.
    sans_annulation = False
    if sorte != classement.LECTURE:
        try:
            prep = await run_in_threadpool(annulation.preparer, fil["sub"], target, targs, gid)
        except Exception:
            logger.warning("copilot : préparation de l'annulation impossible — l'appel "
                           "part sans annulation, et l'étape le dit", exc_info=True)
            sans_annulation = annulation.annulable(target)
    try:
        issue = await executer(b, fil["sub"], name, args, org=fil["org_id"],
                               projet=fil["project_id"], run_id=fil["run_id"], geste_id=gid)
    except Exception:
        logger.exception("copilot : outil %s en échec inattendu", target)
        issue = None
    if issue is None:
        texte, ok = f"Tool error: `{target}` failed unexpectedly.", False
    else:
        texte, ok = issue.texte, issue.ok
    revert_id = None
    if ok and prep is not None:
        try:
            offre = await run_in_threadpool(annulation.offre, prep, issue.donnees)
            if offre:
                revert_id = f"rv_{uuid.uuid4().hex}"
                await run_in_threadpool(store.offrir_annulation, revert_id, fil["id"],
                                        fil["sub"], carte.libelle(name, args), offre[0],
                                        offre[1], annulation.DUREE_S)
        except Exception:
            logger.warning("copilot : annulation non offerte pour %s", target, exc_info=True)
            revert_id = None
    change = None
    if ok and sorte != classement.LECTURE:
        table = targs.get("datastore") if target.startswith("data_") else None
        change = {k: v for k, v in {
            "table": str(table) if table is not None else None,
            "schema": bool(re.search(r"schema|column|create|datastore", target)),
            "scope": carte.portee(target)}.items() if v is not None}
    note = bloc.get("_edits")
    contenu = couper(texte)
    if note:
        contenu = (f"[The person edited the call on the card before applying it: "
                   f"{', '.join(note)}.]\n{contenu}")
    # Recorded BEFORE the event goes out: a leg cancelled at this `yield` still keeps
    # the result of a call that ran (the caller writes it back).
    if fil["queue"] and fil["queue"][0].get("id") == bloc["id"]:
        fil["queue"].pop(0)
    fil["results"].append(_resultat(bloc["id"], contenu, not ok))
    resume = carte.resume(texte)
    if ok and sans_annulation:
        resume = f"{resume} · no undo for this change"
    yield ev("tool_result", id=bloc["id"], ok=ok, summary=resume,
             changed=change, revert_id=revert_id)


async def drainer(fil: dict, b: Boite, etat: dict) -> AsyncIterator[dict]:
    """Works through the queued tool calls of the last assistant turn. Sets
    `etat["pause"]` when one needs the person (a card) or the page (a client tool)."""
    etat["pause"] = False
    clients = noms_client(fil)
    offerts = {o["name"] for o in b.offerts}
    while fil["queue"]:
        bloc = fil["queue"][0]
        name, args = bloc["name"], bloc.get("input") or {}
        if name in clients:
            fil["pending"] = {"kind": "client", "block": bloc}
            etat["pause"] = True
            yield ev("client_tool_call", id=bloc["id"], name=name, args=args)
            return
        target, targs = classement.cible(name, args)
        refus = None
        if name not in offerts:
            refus = f"{name} is not one of your tools; reach it through oto_call."
        elif classement.interdit(target):
            refus = (f"{target} is not available to the in-app agent: the person does this "
                     "from their own screens.")
        elif target not in b.visibles:
            refus = f"Unknown tool `{target}`: it is not in your toolbox here."
        if refus:
            fil["queue"].pop(0)
            fil["results"].append(_resultat(bloc["id"], refus, True))
            continue
        sorte = classement.classer(target, targs, b.annotations(target))
        dispense = sorte == classement.ECRITURE and (
            fil.get("mode") == "act"
            or classement.cle_de_sorte(name, args) in (fil.get("remembered") or []))
        if sorte != classement.LECTURE and not dispense and not bloc.get("approved"):
            fil["pending"] = {"kind": "approval", "block": bloc, "sorte": sorte}
            etat["pause"] = True
            titre = carte.phrase(name, args)
            if not annulation.annulable(target):
                titre = f"{titre} {annulation.SANS_ANNULATION}"
            yield ev("approval_required", id=bloc["id"], title=titre,
                     steps=[{"title": carte.libelle(name, args),
                             "detail": carte.detail(name, args)}],
                     destructive=classement.rouge(target, targs),
                     editable=carte.champs_modifiables(name, args),
                     rememberable=sorte == classement.ECRITURE)
            return
        async for e in _lancer_outil(fil, b, bloc):
            yield e
        await sauver(fil)


def _outils_du_modele(fil: dict, b: Boite) -> list[dict]:
    serveur = {o["name"] for o in b.offerts}
    clients = [t for t in fil.get("client_tools") or [] if t.get("name") not in serveur]
    return [*b.offerts, *clients]


async def _un_appel(fil: dict, b: Boite, cle: cles.Cle, sortie: dict) -> AsyncIterator[dict]:
    """One model call, reserved before and settled after; yields text and progress, and
    leaves the assistant message in `sortie["message"]`."""
    if await run_in_threadpool(depense.atteint, fil["sub"]):
        raise cles.SansCle(depense.CODE, depense.message_de_refus(), 429)
    rang = int(fil.get("model_calls") or 0) + 1
    uid = cles.uid_du_tour(fil["id"], rang)
    await run_in_threadpool(cles.reserver, cle, uid, fil["sub"], fil["org_id"],
                            modele.MODELE, modele.MAX_TOKENS)
    fil["model_calls"] = rang
    await sauver(fil)
    tampon: list[dict] = []
    filtre = FiltreSuite(lambda t: tampon.append(ev("text_delta", text=t)))
    progres, regle, rien_recu = "", False, True
    releve: dict = {}
    # The attached files resolved on a COPY (`pieces.resoudre`): the current turn's as
    # content, presigned once per leg (`_pieces`, never stored), earlier ones as a line.
    envoyes = await run_in_threadpool(pieces.resoudre, fil["messages"], fil["sub"],
                                      fil["org_id"], fil.setdefault("_pieces", {}))
    corps = modele.corps_de_requete(fil["system"], _outils_du_modele(fil, b), envoyes)
    try:
        async for quoi, val in modele.flux(cle.secret, cle.workspace, corps, releve=releve):
            rien_recu = False
            if quoi == "block":
                progres = ""
            elif quoi == "thinking" and val:
                progres += val
                if progres.strip():
                    yield ev("progress", text=progres.strip())
            elif quoi == "text":
                filtre.pousser(val)
                while tampon:
                    yield tampon.pop(0)
            elif quoi == "end":
                sortie["message"] = val
        sortie["suggestions"] = filtre.finir()
        while tampon:
            yield tampon.pop(0)
        await run_in_threadpool(cles.regler, cle, uid, fil["sub"], fil["org_id"],
                                modele.MODELE, (sortie.get("message") or {}).get("usage") or {},
                                fil["id"])
        regle = True
    except modele.ErreurModele as e:
        if e.statut is not None and rien_recu:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(cles.liberer, cle, uid)
            regle = True
        raise
    finally:
        # The daily ceiling counts what the provider reported, the call finished or cut.
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(_noter_depense, fil, releve.get("usage") or {})
            if not regle:
                await run_in_threadpool(cles.clore, cle, uid)


def _noter_depense(fil: dict, usage: dict) -> None:
    """Adds one call to the person's spend of the day. Logged, never raised: the call
    has happened, and its answer must not be lost to a failed counter."""
    try:
        depense.noter(fil["sub"], modele.MODELE, usage)
    except Exception:
        logger.exception("copilot : dépense de l'appel non comptée (conversation %s, "
                         "jetons %s)", fil["id"], usage)


async def conduire(fil: dict, b: Boite, cle: cles.Cle) -> AsyncIterator[dict]:
    """One leg: drains what is queued, then model turns until an answer or a pause."""
    etat: dict = {}
    async for e in drainer(fil, b, etat):
        yield e
    if etat.get("pause"):
        await sauver(fil)
        return
    if fil["results"]:
        fil["messages"].append({"role": "user", "content": fil["results"]})
        fil["results"] = []
    coupes = 0
    for _ in range(MAX_ETAPES):
        await run_in_threadpool(store.prolonger, fil["id"], fil["_verrou"], VERROU_S)
        sortie: dict = {}
        try:
            async for e in _un_appel(fil, b, cle, sortie):
                yield e
        except cles.SansCle as e:
            await sauver(fil)
            yield ev("error", message=e.message)
            yield ev("done")
            return
        except modele.ErreurModele as e:
            await sauver(fil)
            yield ev("error", message=e.message)
            yield ev("done")
            return
        message = sortie.get("message") or {"content": [], "stop_reason": None}
        fil["messages"].append({"role": "assistant", "content": message["content"]})
        if message.get("stop_reason") == "refusal":
            await sauver(fil)
            yield ev("error", message="The model declined this request.")
            yield ev("done")
            return
        appels = [blk for blk in message["content"] if blk.get("type") == "tool_use"]
        if message.get("stop_reason") == "max_tokens":
            # Coupé net (typiquement : un long texte réécrit en entier dans un appel). Fini
            # « normalement », le fil s'arrêtait sans un mot ; l'appel coupé ne s'exécute
            # jamais (son entrée est tronquée) — le modèle l'apprend et réessaie UNE fois.
            coupes += 1
            logger.warning("copilot : sortie coupée à max_tokens (conversation %s, outils %s, "
                           "%s fois)", fil["id"], [a.get("name") for a in appels], coupes)
            if appels and coupes == 1:
                fil["messages"].append({"role": "user", "content": [
                    _resultat(a["id"], COUPE, True) for a in appels]})
                await sauver(fil)
                continue
            if appels:
                regler_interrompus(fil, COUPE)
            await sauver(fil)
            yield ev("error", message=COUPE_ECHEC)
            yield ev("done")
            return
        if message.get("stop_reason") != "tool_use" or not appels:
            await sauver(fil)
            if sortie.get("suggestions"):
                yield ev("suggestions", items=sortie["suggestions"])
            if fil.get("_speak"):
                # The answer read aloud, once, before `done` — or nothing (`voix.py`).
                from . import voix
                audio = await voix.dire(fil, message["content"])
                if audio:
                    yield audio
            yield ev("done")
            return
        fil["queue"] = [{"id": u["id"], "name": u["name"], "input": u.get("input") or {}}
                        for u in appels]
        fil["results"] = []
        await sauver(fil)
        async for e in drainer(fil, b, etat):
            yield e
        if etat.get("pause"):
            await sauver(fil)
            return
        fil["messages"].append({"role": "user", "content": fil["results"]})
        fil["results"] = []
        await sauver(fil)
    yield ev("error", message=f"Stopped after {MAX_ETAPES} steps.")
    yield ev("done")


def repondre_carte(fil: dict, approval_id: str, approuve: bool,
                   edits: Optional[dict], retenir: bool) -> Optional[str]:
    """Applies the person's answer to the waiting card. Returns an error message when
    the answer is not for what is waiting (another card, a page action), else None."""
    attente = fil.get("pending") or {}
    bloc = attente.get("block") or {}
    if attente.get("kind") != "approval" or bloc.get("id") != approval_id \
            or not fil["queue"] or fil["queue"][0].get("id") != approval_id:
        return "That answer is for something no longer waiting."
    fil["pending"] = None
    if not approuve:
        fil["queue"].pop(0)
        fil["results"].append(_resultat(approval_id, REFUS, True))
        return None
    if edits:
        avant = bloc.get("input") or {}
        apres = carte.appliquer_modifications(bloc["name"], avant, edits)
        if apres != avant:
            offerts = {f["key"] for f in carte.champs_modifiables(bloc["name"], avant) or []}
            bloc["_edits"] = sorted(k for k in edits if k in offerts)
            bloc["input"] = apres
    if retenir and attente.get("sorte") == classement.ECRITURE:
        sorte = classement.cle_de_sorte(bloc["name"], bloc.get("input"))
        fil["remembered"] = sorted({*(fil.get("remembered") or []), sorte})
    bloc["approved"] = True
    fil["queue"][0] = bloc
    return None


def repondre_page(fil: dict, call_id: str, ok: bool, result, erreur: Optional[str],
                  outils_client: Optional[list]) -> Optional[str]:
    """Applies the page's answer to a client tool call; refreshes the page's tools."""
    import json
    attente = fil.get("pending") or {}
    bloc = attente.get("block") or {}
    if attente.get("kind") != "client" or bloc.get("id") != call_id \
            or not fil["queue"] or fil["queue"][0].get("id") != call_id:
        return "That answer is for something no longer waiting."
    fil["pending"] = None
    suite = ""
    if outils_client is not None:
        fil["client_tools"] = outils_client
        suite = f" Page actions now: {', '.join(t['name'] for t in outils_client)}."
    fil["queue"].pop(0)
    corps = result if ok else {"error": erreur or "failed"}
    if ok and corps is None:
        corps = {"ok": True}
    fil["results"].append(_resultat(
        call_id, json.dumps(corps, ensure_ascii=False, default=str)[:20_000] + suite, not ok))
    return None
