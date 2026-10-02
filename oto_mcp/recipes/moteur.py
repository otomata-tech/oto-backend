"""Le moteur d'une recette : appeler l'outil page par page, filtrer, fabriquer les
lignes, écrire. Sans modèle.

**Chaque page est un appel ordinaire de l'outil** : `tools/meta.executer_cible`, le
corps d'`oto_call` — mêmes gardes (activation du connecteur, axes, org du run), même
journal sous le NOM DE L'OUTIL (donc même facturation, par sa `quantity`), même
rédaction. Les lignes sont fabriquées depuis le résultat RÉDIGÉ : un champ que l'org
cache aux agents n'atterrit pas dans un tableau que des agents lisent.

**Ce qui arrête une exécution, et ce qu'elle rend.** Le plafond de dépense
(`limits.max_units`, dans les unités de l'outil), le nombre de pages, le budget
d'horloge (au-delà : reçu partiel et `resume`), la dernière page, ou un refus — de
l'outil (clé, crédits) ou du tableau. Le reçu porte des COMPTES et des CODES, jamais
une valeur lue.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any, Optional

from starlette.concurrency import run_in_threadpool

from .. import redaction
from ..mcp_errors import McpError
from ..tool_visibility import namespace_of
from . import contrat
from . import correspondance as co
from . import ecriture

#: Budget d'horloge d'un appel, vérifié AVANT chaque page : passé ce délai le reçu est
#: rendu partiel avec `resume`, plutôt que coupé sans reçu. Même raisonnement que
#: `datastore/par_reference.BUDGET_S`.
BUDGET_S = 30.0


class RecetteRefusee(Exception):
    """Refus AVANT tout appel au connecteur. `code` nommé, message pour l'agent."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _jeton(etat: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(etat).encode()).decode()


def lire_reprise(reprise: Optional[str]) -> dict:
    if not reprise:
        return {"p": None, "c": None, "u": 0, "n": 0}
    try:
        etat = json.loads(base64.urlsafe_b64decode(reprise.encode()).decode())
        return {"p": etat.get("p"), "c": etat.get("c"), "u": int(etat.get("u", 0)),
                "n": int(etat.get("n", 0))}
    except (ValueError, TypeError, AttributeError):
        raise RecetteRefusee("invalid_resume", "`resume` is not a token returned by a "
                                               "previous run of this recipe.")


def _recu() -> dict:
    return {"pages": 0, "items_seen": 0, "units": 0, "rows_built": 0, "written": 0,
            "updated": 0, "existing_left_untouched": 0, "skipped_where": 0,
            "skipped_no_key": 0, "duplicates_in_call": 0, "failed": {},
            "created_columns": [], "done": False, "stopped": None, "resume": None}


async def _appeler(fastmcp, sub: Optional[str], outil: str, args: dict):
    from ..tools import meta
    if namespace_of(outil) in contrat.NAMESPACES_INTERDITS:
        raise RecetteRefusee("tool_not_allowed",
                             f"`{outil}` is a platform tool: a recipe only calls connector tools.")
    tool = await meta.resoudre_outil(fastmcp, outil)
    if tool is None:
        raise RecetteRefusee("unknown_tool", f"Unknown tool `{outil}`.")
    return await meta.executer_cible(tool, sub, outil, outil, args)


def _arguments(corps: dict, params: dict, etat: dict, restant: int) -> dict:
    args = co.rendre(corps.get("arguments") or {}, {"params": params})
    pag = corps["source"]["pagination"]
    if pag["type"] == "page":
        args[pag["param"]] = etat["p"] if etat["p"] is not None else pag.get("start", 0)
    elif pag["type"] == "cursor" and etat["c"]:
        args[pag["param"]] = etat["c"]
    if pag.get("size"):
        taille = pag["size"]
        if corps["units"] == "items":
            taille = max(1, min(taille, restant))
        args[pag["size_param"]] = taille
    return args


def _suite(corps: dict, payload: Any, elements: list, args: dict, etat: dict) -> bool:
    """Avance l'état vers la page suivante ; False = c'était la dernière."""
    pag = corps["source"]["pagination"]
    if pag["type"] == "none":
        return False
    if pag["type"] == "cursor":
        etat["c"] = co.lire(payload, pag["next"])
        return bool(etat["c"])
    if not elements or (pag.get("last") and co.lire(payload, pag["last"]) is True):
        return False
    if pag.get("size_param") and len(elements) < int(args.get(pag["size_param"]) or 0):
        return False
    etat["p"] = args[pag["param"]] + 1
    return True


def _arreter(recu: dict, code: str, **details) -> dict:
    recu["stopped"] = code
    recu.update(details)
    return recu


async def executer(corps: dict, params: dict, *, fastmcp, sub: Optional[str],
                   datastore: Any = None, reprise: Optional[str] = None,
                   ecrire: bool = True, pages_max: Optional[int] = None,
                   budget_s: float = BUDGET_S) -> dict:
    """Exécute une recette VALIDÉE (`contrat.valider`). `ecrire=False` = l'épreuve :
    les pages sont appelées (et facturées) mais rien n'est écrit ; le reçu porte le
    remplissage par colonne (`fill`)."""
    recu = _recu()
    etat = lire_reprise(reprise)
    recu["units"], recu["pages"] = etat["u"], etat["n"]
    col_cle = corps["key"]["column"]
    mappees = list(corps["map"])
    remplissage = {c: 0 for c in mappees}
    tableau = None
    if datastore is not None:
        try:
            tableau = await run_in_threadpool(ecriture.ouvrir, datastore, col_cle,
                                              ecrire=ecrire)
            if ecrire:
                recu["created_columns"] = await run_in_threadpool(
                    ecriture.creer_colonnes, tableau,
                    mappees + list(corps["values"]) + [col_cle])
        except ecriture.TableauIndisponible as e:
            raise RecetteRefusee(e.code, str(e))
    elif ecrire:
        raise RecetteRefusee("missing_datastore", "`datastore` (the table number) is "
                                                  "required to run a recipe.")
    lim = corps["limits"]
    pages_max = min(pages_max or lim["max_pages"], lim["max_pages"])
    fin = time.monotonic() + budget_s
    pages_ici = 0
    # `finally` : l'épreuve rend son remplissage quelle que soit la sortie de la boucle.
    try:
        while True:
            restant = lim["max_units"] - recu["units"]
            if restant <= 0:
                return _arreter(recu, "spend_cap")
            if recu["pages"] >= lim["max_pages"] or pages_ici >= pages_max:
                return _arreter(recu, "max_pages", resume=_jeton({**etat, "u": recu["units"],
                                                                   "n": recu["pages"]}))
            if pages_ici and time.monotonic() >= fin:
                return _arreter(recu, "time_budget", resume=_jeton({**etat, "u": recu["units"],
                                                                     "n": recu["pages"]}))
            args = _arguments(corps, params, etat, restant)
            try:
                issue = await _appeler(fastmcp, sub, corps["tool"], dict(args))
            except McpError as e:
                # Arguments refusés par le schéma de l'outil, garde d'activation : rien n'a
                # été appelé pour cette page.
                return _arreter(recu, "call_refused", error=str(e.error.message)[:500])
            if not issue.ok:
                return _arreter(recu, issue.code or "tool_failed", error=issue.message,
                                retryable=issue.retryable,
                                resume=_jeton({**etat, "u": recu["units"], "n": recu["pages"]}))
            if issue.retenu:
                return _arreter(recu, "redaction_withheld",
                                error="The org's redaction policy withheld this tool's output.")
            payload = redaction.extract_payload(issue.result)
            elements = co.lire(payload, corps["source"]["items"]) if corps["source"]["items"] \
                else payload
            if not isinstance(elements, list):
                cles = sorted(payload)[:20] if isinstance(payload, dict) else []
                return _arreter(recu, "items_not_found",
                                error=f"`source.items` does not point to a list. Top-level keys "
                                      f"of the result: {cles}")
            pages_ici += 1
            recu["pages"] += 1
            recu["items_seen"] += len(elements)
            recu["units"] += len(elements) if corps["units"] == "items" else 1
            lignes: dict[str, dict] = {}
            for el in elements:
                if not co.garde(el, corps["where"], params):
                    recu["skipped_where"] += 1
                    continue
                rangee = co.ligne(el, corps["map"], corps["values"], params)
                cle = co.cle(el, corps["key"], rangee, params)
                if cle is None:
                    recu["skipped_no_key"] += 1
                    continue
                rangee[col_cle] = cle
                if cle in lignes:
                    recu["duplicates_in_call"] += 1
                    continue
                lignes[cle] = rangee
                for c in mappees:
                    if not co._vide(rangee.get(c)):
                        remplissage[c] += 1
            recu["rows_built"] += len(lignes)
            if ecrire and lignes:
                try:
                    await run_in_threadpool(ecriture.ecrire_page, tableau, lignes,
                                            on_existing=corps["on_existing"],
                                            colonnes_mappees=mappees, recu=recu)
                except ecriture.TableauIndisponible as e:
                    return _arreter(recu, e.code, error=str(e))
            if not _suite(corps, payload, elements, args, etat):
                recu["done"] = True
                break
    finally:
        if not ecrire:
            n = recu["rows_built"] or 1
            recu["fill"] = {c: round(v / n, 3) for c, v in remplissage.items()}
    return recu


def _forme(obj: Any, prefixe: str, out: dict, profondeur: int) -> None:
    if profondeur > 4:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            _forme(v, f"{prefixe}.{k}" if prefixe else k, out, profondeur + 1)
        return
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        _forme(obj[0], f"{prefixe}[0]", out, profondeur + 1)
        return
    entree = out.setdefault(prefixe, {"type": type(obj).__name__, "filled": 0})
    if not co._vide(obj):
        entree["filled"] += 1
        entree["type"] = type(obj).__name__


async def echantillon(fastmcp, sub: Optional[str], outil: str, arguments: dict,
                      items: str = "") -> dict:
    """UN appel de l'outil, et la FORME de ses éléments : chaque chemin, son type,
    sur combien d'éléments il est rempli. Rien n'est écrit. L'appel est facturé comme
    tout appel de l'outil — passe la plus petite taille de page."""
    issue = await _appeler(fastmcp, sub, outil, dict(arguments or {}))
    if not issue.ok:
        raise RecetteRefusee(issue.code or "tool_failed", issue.message or "tool failed")
    if issue.retenu:
        raise RecetteRefusee("redaction_withheld",
                             "The org's redaction policy withheld this tool's output.")
    payload = redaction.extract_payload(issue.result)
    elements = co.lire(payload, items) if items else payload
    if not isinstance(elements, list):
        listes = sorted(k for k, v in payload.items() if isinstance(v, list)) \
            if isinstance(payload, dict) else []
        return {"items_found": False, "list_paths": listes,
                "top_level_keys": sorted(payload)[:30] if isinstance(payload, dict) else []}
    forme: dict = {}
    for el in elements:
        _forme(el, "", forme, 0)
    return {"items_found": True, "items": len(elements),
            "paths": [{"path": p, **v} for p, v in sorted(forme.items())]}
