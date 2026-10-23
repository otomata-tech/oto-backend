"""Les recettes EN CODE (#1202, lot 2) : le corps d'une recette est du Python, joué dans
le bac à sable d'`oto_function` par le pont (`functions/pont.py`).

Une recette-code se range comme une recette déclarative — mêmes tables, même cycle
proposée → publiée, même `oto_recipe` — et s'en distingue par `kind: "code"` :

    {"kind": "code",
     "sources": {"recette.py": "async def run(oto, params): ..."},
     "entrypoint": "recette:run",
     "params": {"q": {"required": true}},
     "writes": ["folk_record:create"],  # lot 3 : les écritures chez un tiers, déclarées
     "limits": {"max_units": 200, "max_calls": 100, "max_seconds": 120, "max_writes": 50}}

Le code reçoit `oto` et les `params` résolus ; il appelle des outils (`await
oto.call(outil, arguments)`) et rend un reçu — des COMPTES, pas des données : il est
borné à `MAX_RECU` caractères. Ce qu'il peut appeler est jugé par `pont.appeleur_de_recette` :
outils de connecteur déclarés en lecture, `data_rows`, `data_write` dans le seul tableau
désigné par `op=run` (jamais en épreuve), et — lot 3 — les écritures chez un tiers de la
liste fermée `ecritures.ECRITURES` que la recette DÉCLARE dans `writes` (jamais en
épreuve, `limits.max_writes` par exécution ; un outil qui peut envoyer exige
`allow_sending: true`).

**Qui publie** : une recette-code tourne avec les droits de qui l'EXÉCUTE, pas de qui
l'a écrite (auteur ≠ appelant, #1202). Tant que cette règle n'est pas tranchée, l'épreuve
et la publication d'une recette-code sont réservées à la PLATEFORME — la règle
d'`oto_function`. L'écrire et la proposer restent ouverts au membre.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Optional

from starlette.concurrency import run_in_threadpool

from .. import outils_du_travail
from ..functions import contract as contrat_fonctions
from ..functions import pont as pont_bac
from . import contrat, outils_cites, pont
from . import ecritures as ec

KIND = "code"
_CLES = {"kind", "sources", "entrypoint", "params", "limits", "writes", "allow_sending"}
#: Le reçu que rend le code : des comptes. Au-delà, il est remplacé par un refus — le
#: reçu n'est pas un canal de données.
MAX_RECU = 4000
#: Pas de plafond de plateforme sur `max_calls`, `max_units` ni `max_writes` : ce sont les
#: bornes de qui écrit la recette (`max_calls` absent = pas de borne). La durée garde le
#: sien, celui du bac à sable.
MAX_SECONDES = 600


def est_code(corps: Any) -> bool:
    return isinstance(corps, dict) and corps.get("kind") == KIND


def valider(corps: Any) -> dict:
    """Le corps normalisé (défauts posés), ou `contrat.RecetteInvalide`."""
    probs: list[str] = []
    c = copy.deepcopy(corps)
    inconnues = sorted(set(c) - _CLES)
    if inconnues:
        probs.append(f"unknown keys for a code recipe: {inconnues} (allowed: {sorted(_CLES)})")
    try:
        contrat_fonctions.valider(c.get("sources"), c.get("entrypoint") or "", [])
    except contrat_fonctions.VersionInvalide as e:
        probs.append(str(e))
    params = c.setdefault("params", {})
    if not isinstance(params, dict):
        probs.append("`params` is an object {name: {required?, default?}}")
    lim = c.get("limits")
    if not isinstance(lim, dict) or not isinstance(lim.get("max_units"), (int, float)) \
            or isinstance(lim.get("max_units"), bool) \
            or not 0 < lim["max_units"]:
        probs.append("`limits.max_units` (a hard cap on what the tools BILL, above 0) "
                     "is required: there is no default")
    else:
        lim.setdefault("max_seconds", 120)
        appels = lim.get("max_calls")
        if appels is not None and (not isinstance(appels, int) or isinstance(appels, bool)
                                   or appels < 1):
            probs.append("`limits.max_calls`: at least 1, or absent for no limit")
        if not isinstance(lim["max_seconds"], int) \
                or not 10 <= lim["max_seconds"] <= MAX_SECONDES:
            probs.append(f"`limits.max_seconds`: 10 to {MAX_SECONDES}")
        lim.setdefault("max_writes", 50 if c.get("writes") else 0)
        if not isinstance(lim["max_writes"], int) or isinstance(lim["max_writes"], bool) \
                or lim["max_writes"] < 0:
            probs.append("`limits.max_writes`: 0 or more")
    probs += _probs_ecritures(c)
    if probs:
        raise contrat.RecetteInvalide(probs)
    return c


def _probs_ecritures(c: dict) -> list[str]:
    """`writes` : une liste sans doublon de noms de `ecritures.declarables()` ;
    `allow_sending: true` si l'une peut envoyer."""
    declarees = c.setdefault("writes", [])
    if not isinstance(declarees, list) or not all(isinstance(d, str) for d in declarees):
        return ["`writes` is a list of `tool:op` names (e.g. \"folk_record:create\")"]
    probs = []
    inconnues = sorted(set(declarees) - ec.declarables())
    if inconnues:
        probs.append(f"`writes`: {inconnues} cannot be declared (allowed: "
                     f"{sorted(ec.declarables())})")
    if len(set(declarees)) != len(declarees):
        probs.append("`writes` names each write once")
    envoi = c.get("allow_sending", False)
    if not isinstance(envoi, bool):
        probs.append("`allow_sending` is true or false")
    elif ec.envoient(declarees) and envoi is not True:
        probs.append(f"{ec.envoient(declarees)} can trigger a send (a lead added to an "
                     "active campaign gets its sequence): `allow_sending: true` is required")
    return probs


def refus_hors_liste(manque: dict) -> dict:
    """Le reçu d'une recette-code refusée avant tout appel : les outils qu'elle cite et
    que l'agent n'a pas, et le geste qui les lui donne (`missing_tools`, `fix`)."""
    noms = manque["missing"]
    agent = f"agent {manque['trigger_id']}" if manque.get("trigger_id") else "this agent"
    return {"ok": False, "calls": 0, "units": 0, "refused": 0, "stopped": None,
            "writes": {}, "done": False, "code": outils_du_travail.CODE,
            "missing_tools": noms,
            "fix": outils_du_travail.correctif(manque.get("trigger_id"),
                                               manque.get("tools") or [], noms),
            "error": (f"{outils_du_travail.CODE}: this recipe calls {noms}, which "
                      f"{agent} is not allowed to use (job {manque['job_id']}). Nothing "
                      "was called. Stop the run with run_finish(outcome=\"blocked\") "
                      f"naming them: the owner adds them to {agent}'s tools in one "
                      "click (the agent's page shows them).")}


async def executer(corps: dict, params: dict, *, fastmcp, sub: Optional[str],
                   datastore: Any = None, ecrire: bool) -> dict:
    """Joue une recette-code VALIDÉE. `ecrire=False` = l'épreuve : aucune écriture,
    `data_write` et les écritures chez un tiers refusés. Rend le reçu `{ok, result |
    error, calls, units, refused, stopped, writes, done}` (+ `journal` en épreuve, où
    seule la plateforme lit) ; `writes` compte les écritures chez un tiers TENTÉES, par
    nom déclaré."""
    lim = corps["limits"]
    # Lancée par un agent hébergé : tout ce que le code CITE doit être dans la liste du
    # travail. Jugé ici, avant le premier appel, et refusé UNE fois en nommant chaque
    # outil manquant — sinon le code découvre un outil par exécution, après des écritures.
    manque = await run_in_threadpool(outils_du_travail.manquants,
                                     outils_cites.outils_cites(corps))
    if manque:
        return refus_hors_liste(manque)
    ecritures = pont.compteur_neuf()
    sortie = await pont_bac.executer_avec_pont(
        sources=corps["sources"], entrypoint=corps["entrypoint"], entree=params,
        appeler=pont.appeleur_de_recette(
            fastmcp, sub, datastore=datastore, ecrire=ecrire,
            ecritures=frozenset(corps.get("writes") or ()),
            max_ecritures=lim.get("max_writes", 0), compteur=ecritures),
        budget=pont_bac.Budget(appels=lim.get("max_calls"), unites=float(lim["max_units"]),
                               delai_s=float(lim["max_seconds"])))
    recu = {k: sortie.get(k) for k in ("ok", "calls", "units", "refused", "stopped")}
    # Les écritures chez un tiers, tentées (un délai dépassé a pu écrire) : le reçu les
    # compte par nom déclaré, et dit si leur plafond a arrêté le code.
    recu["writes"] = ecritures["writes"]
    recu["stopped"] = recu["stopped"] or ecritures["stopped"]
    if sortie.get("ok"):
        if len(json.dumps(sortie.get("result"))) > MAX_RECU:
            recu.update(ok=False, error=f"receipt_too_large: the recipe returned more than "
                                        f"{MAX_RECU} characters — return counts, not data")
        else:
            recu["result"] = sortie.get("result")
    else:
        recu["error"] = sortie.get("error")
    recu["done"] = bool(recu["ok"]) and recu["stopped"] is None
    if not ecrire:
        recu["journal"] = sortie.get("journal") or ""
    return recu
