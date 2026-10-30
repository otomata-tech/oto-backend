"""Ce que le pont du bac à sable (`functions/pont.py`) a le droit d'appeler : les outils
DÉCLARÉS EN LECTURE, sous les mêmes gardes qu'`oto_call` (#1202, lot 1).

Le pont ne décide d'aucun droit ; cette fonction les porte tous, et ce sont ceux des
recettes déclaratives, au même endroit : `moteur._outil` (outil de connecteur, ni outil
de plateforme ni outil à modèle, déclaré en lecture — refus nommé sinon) puis
`moteur._appeler` → `meta.executer_cible` (activation du connecteur, axes d'appel, org du
run, journal sous le nom de l'outil, facturation, rédaction). L'identité est celle de
l'appelant (`sub`) : le code qui tourne n'en a aucune à lui.

Aucun outil de connecteur qui écrit pour une recette déclarative ni pour le pont en
lecture : il est refusé ici, avant tout appel. Une recette-code (`appeleur_de_recette`)
écrit dans le tableau que son exécution désigne et chez un tiers par TOUT outil de
connecteur, classé par le verbe de son op (`ecritures.classer_appel`) : les écritures et
les envois qu'elle DÉCLARE (`writes`), jamais en épreuve, dans l'org du run, bornés par
`max_writes` (et `max_sends`) ; jamais une op qui détruit. Elle tourne avec les droits
de qui l'EXÉCUTE (#1202) : le pont n'ouvre rien que l'appelant ne puisse appeler.
"""
from __future__ import annotations

from typing import Any, Optional

from .. import outils_du_travail, redaction
from ..mcp_errors import McpError
from ..tools import lecture as lecture_declaree
from . import ecritures as ec
from . import moteur


def _reponse(issue) -> dict:
    """L'issue d'`executer_cible`, dans la forme que le pont rend au code."""
    if not issue.ok:
        return {"ok": False, "code": issue.code or "tool_failed",
                "message": issue.message or "", "quantity": issue.quantity}
    if issue.retenu:
        return {"ok": False, "code": "redaction_withheld",
                "message": "The org's redaction policy withheld this tool's output.",
                "quantity": issue.quantity}
    return {"ok": True, "result": redaction.extract_payload(issue.result),
            "quantity": issue.quantity}


async def _executer(tool, sub: Optional[str], outil: str, arguments: dict) -> dict:
    """UN appel ordinaire de l'outil (`moteur._appeler` → `meta.executer_cible`) : ses
    gardes, son journal (`tool_calls`), sa rédaction. Un refus de garde est une réponse."""
    try:
        issue = await moteur._appeler(tool, sub, outil, dict(arguments))
    except McpError as e:
        # Arguments refusés par le schéma de l'outil, garde d'activation ou de
        # suspension : rien n'a été appelé. Un outil hors de la liste du travail hébergé
        # garde son code nommé : le code de la recette doit pouvoir le distinguer.
        data = e.error.data if isinstance(e.error.data, dict) else {}
        hors_liste = data.get("code") == outils_du_travail.CODE
        code = outils_du_travail.CODE if hors_liste else "call_refused"
        return {"ok": False, "code": code, "message": str(e.error.message)[:500]}
    return _reponse(issue)


def appeleur_en_lecture(fastmcp, sub: Optional[str]):
    """L'`appeler` du pont pour `sub` : un outil en lecture, ou une réponse `ok: false`
    nommée. Ne lève pas — un refus est une réponse."""
    async def appeler(outil: str, arguments: dict) -> dict:
        try:
            tool = await moteur._outil(fastmcp, outil)
        except moteur.RecetteRefusee as e:
            return {"ok": False, "code": e.code, "message": str(e)}
        return await _executer(tool, sub, outil, arguments)
    return appeler


#: Ce qu'une page de `data_rows` peut rendre au code.
MAX_LIGNES_LUES = 500
_ARGS_LIRE = frozenset({"datastore", "filter", "fields", "limit", "cursor"})
_ARGS_ECRIRE = frozenset({"rows", "key", "datastore"})


#: Les jetons de contexte qu'une écriture chez un tiers ne prend pas : elle se fait dans
#: l'org du run, jamais ailleurs. `_account` / `_instance` (quel compte du connecteur)
#: restent permis.
_AXES_HORS_RUN = frozenset({"_org", "_group", "_project"})


def compteur_neuf() -> dict:
    """L'état des écritures chez un tiers d'UNE exécution, lu par le reçu. `envois` :
    celles d'entre elles qui envoient."""
    return {"writes": {}, "total": 0, "envois": 0, "stopped": None}


def appeleur_de_recette(fastmcp, sub: Optional[str], *, datastore: Any, ecrire: bool,
                        ecritures: frozenset = frozenset(), max_ecritures: int = 0,
                        max_envois: Optional[int] = None,
                        compteur: Optional[dict] = None):
    """L'`appeler` d'une recette-code (lot 2) : celui de la lecture, plus les tableaux.

    - `data_rows(datastore, filter?, fields?, limit?, cursor?)` : lire un tableau que
      l'appelant peut lire — une page à la fois (`MAX_LIGNES_LUES`).
    - `data_write(rows, key)` : écrire dans le SEUL tableau que `op=run` a désigné
      (`datastore`), avec les droits de l'appelant ; jamais en épreuve (`ecrire=False`).
      `key` est exigée : une relance repart du début, l'écriture doit être idempotente.

    Ces deux noms sont des appels ORDINAIRES des outils `data_rows` / `data_write`
    (`meta.executer_cible`, comme tout outil du pont) : leurs gardes (droits sur le
    tableau, org suspendue, axes, org du run), leur ligne dans `tool_calls`, leur
    rédaction. Le pont ne fait que RÉTRÉCIR ce que le code peut leur passer (arguments,
    tableau cible) ; il n'ouvre jamais le store lui-même.

    Un outil déclaré `LECTURE` suit `appeleur_en_lecture`. Tout autre outil de
    connecteur est classé (`ecritures.classer_appel`) : une op qui lit passe ; une op qui
    écrit ou envoie doit être dans `ecritures` (ce que la recette déclare), hors
    épreuve, sans jeton qui l'emmène hors de l'org du run, et dans la limite de
    `max_ecritures` (et de `max_envois` pour un envoi, s'il est posé) — comptée À LA
    TENTATIVE (un délai dépassé a pu écrire), PAR FICHE : un appel en lot (`items` /
    `ids` / `entries`, au plus `MAX_FICHES_PAR_APPEL`) compte autant d'écritures que de
    fiches, et n'est joué que s'il tient en entier dans ce qui reste ; une op qui détruit
    est refusée. `compteur` reçoit les comptes."""
    lecture = appeleur_en_lecture(fastmcp, sub)
    etat = compteur if compteur is not None else compteur_neuf()

    async def appeler(outil: str, arguments: dict) -> dict:
        if outil == "data_rows":
            refus = _refus_lire(arguments)
            if refus:
                return refus
            return await _outil_de_tableau(fastmcp, sub, outil, arguments)
        if outil == "data_write":
            if not ecrire:
                return {"ok": False, "code": "write_in_test",
                        "message": "A test writes nothing: `data_write` only runs in "
                                   "`op=run`."}
            if datastore is None:
                return {"ok": False, "code": "no_target_table",
                        "message": "`data_write` writes into the table `op=run` names: "
                                   "pass `datastore` to the run."}
            refus = _refus_ecrire(arguments, datastore)
            if refus:
                return refus
            return await _outil_de_tableau(fastmcp, sub, outil,
                                           {**arguments, "datastore": datastore})
        return await _chez_un_tiers(outil, arguments)

    async def _chez_un_tiers(outil: str, arguments: dict) -> dict:
        from ..tools import meta
        if outil not in ec.ECRITURES and ec.hors_connecteur(outil):
            # Outil de plateforme, à modèle ou hors connecteur : la lecture le refuse en
            # le nommant, comme avant.
            return await lecture(outil, arguments)
        tool = await meta.resoudre_outil(fastmcp, outil)
        if tool is None:
            if outil not in ec.ECRITURES:
                return await lecture(outil, arguments)
            return {"ok": False, "code": "unknown_tool",
                    "message": f"`{outil}` is not served by this instance."}
        if outil not in ec.ECRITURES and lecture_declaree.en_lecture(tool):
            return await lecture(outil, arguments)
        sens, nom = ec.classer_appel(outil, arguments, multiplexe=_multiplexe(tool))
        if sens == "refus":
            return {"ok": False, "code": "write_op_not_allowed", "message": nom}
        if sens == "lire":
            return await _executer(tool, sub, outil, arguments)
        fiches = _fiches(arguments)
        envoi = sens == "envoyer"
        refus = _refus_ecrire_chez_un_tiers(nom, arguments, fiches, envoi=envoi)
        if refus:
            return refus
        # Réservées AVANT le premier `await` : le pont sert des appels en parallèle, et
        # deux écritures jugées sur le même reste passeraient toutes les deux.
        etat["total"] += fiches
        etat["writes"][nom] = etat["writes"].get(nom, 0) + fiches
        if envoi:
            etat["envois"] += fiches
        return await _executer(tool, sub, outil, arguments)

    def _refus_ecrire_chez_un_tiers(nom: str, arguments: dict, fiches: Optional[int], *,
                                    envoi: bool = False) -> Optional[dict]:
        if nom not in ecritures:
            return {"ok": False, "code": "write_not_declared",
                    "message": f"`{nom}` writes at the provider and this recipe does not "
                               f"declare it (`writes`: {sorted(ecritures)}). Nothing was "
                               "called."}
        if not ecrire:
            return {"ok": False, "code": "write_in_test",
                    "message": f"A test writes nothing: `{nom}` only runs in `op=run`."}
        hors = sorted(_AXES_HORS_RUN & set(arguments))
        if hors:
            return {"ok": False, "code": "write_outside_run_org",
                    "message": f"A write lands in the run's org: leave {hors} out."}
        if fiches is None or not 1 <= fiches <= MAX_FICHES_PAR_APPEL:
            return {"ok": False, "code": "invalid_call",
                    "message": f"A batch (`items` / `ids` / `entries`) is a list of 1 to "
                               f"{MAX_FICHES_PAR_APPEL} records. Nothing was called."}
        if envoi and max_envois is not None and fiches > max_envois - etat["envois"]:
            etat["stopped"] = etat["stopped"] or "max_sends"
            return {"ok": False, "code": "budget_exhausted",
                    "message": f"This run's send budget (max_sends={max_envois}) has "
                               f"{max(max_envois - etat['envois'], 0)} send(s) left; this "
                               f"call sends {fiches}. Nothing was called."}
        reste = max_ecritures - etat["total"]
        if fiches > reste:
            # Un lot qui ne tient pas n'est pas joué en partie : le code peut le
            # redécouper dans ce qui reste, et le reçu dit que le plafond a mordu.
            etat["stopped"] = etat["stopped"] or "max_writes"
            return {"ok": False, "code": "budget_exhausted",
                    "message": f"This run's write budget (max_writes={max_ecritures}) "
                               f"has {max(reste, 0)} write(s) left; this call writes "
                               f"{fiches}. Nothing was called."}
        return None
    return appeler


#: Ce qu'un appel EN LOT peut porter de fiches : la borne des outils CRM de la liste
#: (`items` / `ids` / `entries`, 50 au plus).
MAX_FICHES_PAR_APPEL = 50


def _multiplexe(tool) -> bool:
    """L'outil prend-il une `op` ? Lu dans son schéma d'entrée."""
    schema = getattr(tool, "parameters", None) or {}
    return "op" in (schema.get("properties") or {})


def _fiches(arguments: dict) -> Optional[int]:
    """Combien de fiches touche un appel qui écrit : la longueur de son lot (`items`,
    sinon `ids`, sinon `entries` — les numéros ajoutés à une file d'Allo), 1 sans lot.
    `None` = un lot qui n'est pas une liste."""
    # `entries` = file Allo seulement (`add_queue_numbers`).
    for cle in ("items", "ids", "entries"):
        lot = arguments.get(cle)
        if lot is not None:
            return len(lot) if isinstance(lot, list) else None
    return 1


async def _outil_de_tableau(fastmcp, sub: Optional[str], outil: str,
                            arguments: dict) -> dict:
    from ..tools import meta
    tool = await meta.resoudre_outil(fastmcp, outil)
    if tool is None:
        # Un défaut de l'instance, pas du code : le pont le journalise (`bridge_error`).
        raise LookupError(f"`{outil}` is not served by this instance")
    return await _executer(tool, sub, outil, arguments)


def _refus_lire(arguments: dict) -> Optional[dict]:
    inconnus = sorted(set(arguments) - _ARGS_LIRE)
    if inconnus or "datastore" not in arguments:
        return {"ok": False, "code": "invalid_call",
                "message": "`data_rows` takes datastore, filter?, fields?, limit?, cursor?"
                           + (f" (unknown: {inconnus})" if inconnus else "")}
    limite = arguments.get("limit", 100)
    if not isinstance(limite, int) or isinstance(limite, bool) \
            or not 1 <= limite <= MAX_LIGNES_LUES:
        return {"ok": False, "code": "invalid_call",
                "message": f"`limit`: 1 to {MAX_LIGNES_LUES}"}
    return None


def _refus_ecrire(arguments: dict, cible: Any) -> Optional[dict]:
    inconnus = sorted(set(arguments) - _ARGS_ECRIRE)
    lignes, cle = arguments.get("rows"), arguments.get("key")
    if inconnus or not isinstance(lignes, list) or not lignes \
            or not all(isinstance(r, dict) for r in lignes) \
            or not isinstance(cle, str) or not cle:
        return {"ok": False, "code": "invalid_call",
                "message": "`data_write` takes rows (a non-empty list of objects) and key "
                           "(the column that identifies a row: a re-run must not duplicate)"
                           + (f" (unknown: {inconnus})" if inconnus else "")}
    # Le tableau est celui de l'exécution, posé par le pont : le code peut le taire ou
    # le répéter tel qu'`op=run` l'a reçu, jamais en nommer un autre.
    if "datastore" in arguments and str(arguments["datastore"]) != str(cible):
        return {"ok": False, "code": "write_outside_target",
                "message": "`data_write` only writes into the table `op=run` names: "
                           "leave `datastore` out."}
    return None
