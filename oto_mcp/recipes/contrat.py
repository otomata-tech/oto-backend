"""Le contrat d'une recette : ce que son corps doit dire, vérifié à l'écriture. Pur.

Une recette mal écrite se refuse quand on la propose, avec la liste de ce qui ne va
pas — jamais à la troisième page d'une exécution, quand des lignes sont déjà écrites et
des crédits déjà dépensés.

**Les noms du corps sont en anglais** : un agent les écrit et les relit, et les
erreurs qu'il reçoit les citent.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Optional

from ..tool_visibility import namespace_of
from ..tools.meta import _NON_DISPATCHABLE as NAMESPACES_INTERDITS
from . import correspondance as co

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_COLONNE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
#: Les connecteurs qui font tourner un MODÈLE : ils lisent, mais une recette est « sans
#: modèle » par contrat — un jugement par modèle sur des lignes, c'est `jev_rows`.
#: (`NAMESPACES_INTERDITS`, importé de `tools/meta` : le méta et le spine, qu'une recette
#: n'appelle jamais — elle contournerait ses propres gardes.)
NAMESPACES_A_MODELE = frozenset({"jev", "lighton"})
PAGINATIONS = ("page", "cursor", "none")
OPS_WHERE = ("eq", "ne", "in", "not_in", "contains_any", "empty", "not_empty",
             "in_table", "not_in_table")
OPS_TABLE = ("in_table", "not_in_table")
PORTEES_ARGUMENTS = {"params"}
PORTEES_ELEMENT = {"params", "item"}
#: `for_each` : chaque ligne d'un tableau PARENT déclenche l'appel ; `{{row.col}}` la cite.
PORTEE_LIGNE = "row"
#: Pas de plafond de plateforme : `max_units` (obligatoire, sans défaut), `max_pages` et
#: `for_each.max_parents` (facultatifs : absents, pas de borne) sont ceux de qui écrit la
#: recette. Sans `max_parents`, les parents se lisent par lots de `LOT_PARENTS`.
LOT_PARENTS = 25


class RecetteInvalide(ValueError):
    """Le corps ne respecte pas le contrat. `problemes` = une phrase par défaut."""

    def __init__(self, problemes: list[str]):
        super().__init__("; ".join(problemes))
        self.problemes = problemes


def _colonne_ok(nom: Any) -> bool:
    return isinstance(nom, str) and bool(_COLONNE.match(nom)) and not nom.startswith("_")


def _portees(probs: list, ou: str, gab: Any, permises: set) -> None:
    try:
        inconnues = co.portees_citees(gab) - permises
    except co.GabaritInvalide as e:
        probs.append(f"{ou}: {e}")
        return
    if inconnues:
        probs.append(f"{ou}: unknown scope(s) {sorted(inconnues)} — allowed here: "
                     f"{sorted(permises)}")


def _source(probs: list, source: Any) -> dict:
    if source is None:
        source = {}
    if not isinstance(source, dict):
        probs.append("`source` must be an object")
        return {}
    pag = source.get("pagination") or {"type": "none"}
    if not isinstance(pag, dict) or pag.get("type") not in PAGINATIONS:
        probs.append(f"`source.pagination.type` must be one of {list(PAGINATIONS)}")
        return source
    if pag["type"] in ("page", "cursor") and not isinstance(pag.get("param"), str):
        probs.append("`source.pagination.param` (the argument that carries the page or "
                     "cursor) is required")
    if pag["type"] == "cursor" and not isinstance(pag.get("next"), str):
        probs.append("`source.pagination.next` (path to the next cursor in the result) "
                     "is required")
    if "size" in pag and (not isinstance(pag["size"], int) or pag["size"] < 1):
        probs.append("`source.pagination.size` must be a positive integer")
    if "size" in pag and not isinstance(pag.get("size_param"), str):
        probs.append("`source.pagination.size_param` is required with `size`")
    return {"items": source.get("items") or "", "pagination": pag}


def _tableau_ok(v: Any) -> bool:
    """Un tableau s'adresse par son NUMÉRO (`docs/datastore.md`)."""
    return (isinstance(v, int) and not isinstance(v, bool) and v > 0) \
        or (isinstance(v, str) and v.isdigit())


def _pour_chaque(probs: list, fe: Any) -> Optional[dict]:
    """`for_each` normalisé, ou None s'il n'est pas déclaré."""
    if fe is None:
        return None
    if not isinstance(fe, dict):
        probs.append("`for_each` must be an object {datastore, status_column, filter, "
                     "max_parents, max_items_per_row}")
        return None
    if not _tableau_ok(fe.get("datastore")):
        probs.append("`for_each.datastore` (the NUMBER of the table whose rows drive the "
                     "calls) is required")
    if not _colonne_ok(fe.get("status_column")):
        probs.append("`for_each.status_column` is required: the parent column where each "
                     "row gets `done` or `empty` once pulled, so a re-run skips it")
    if fe.get("filter") is not None and not isinstance(fe["filter"], dict):
        probs.append("`for_each.filter` must be an object (the grammar of `data_rows`)")
    n = fe.get("max_parents")
    if n is not None and (not isinstance(n, int) or isinstance(n, bool) or n < 1):
        probs.append("`for_each.max_parents` (parent rows per call) is at least 1, or "
                     "absent for no limit")
    m = fe.get("max_items_per_row")
    if m is not None and (not isinstance(m, int) or m < 1):
        probs.append("`for_each.max_items_per_row` must be a positive integer")
    return fe


def _clause(probs: list, i: int, w: Any, portees: set) -> None:
    if not isinstance(w, dict) or not isinstance(w.get("path"), str) \
            or w.get("op") not in OPS_WHERE:
        probs.append(f"`where[{i}]`: needs `path` and `op` in {list(OPS_WHERE)}")
        return
    if w.get("normalize") is not None and w["normalize"] not in co.NORMALISEURS:
        probs.append(f"`where[{i}].normalize` must be one of {list(co.NORMALISEURS)}")
    if w["op"] in OPS_TABLE:
        if not _tableau_ok(w.get("table")) or not _colonne_ok(w.get("column")):
            probs.append(f"`where[{i}]`: `{w['op']}` needs `table` (a table NUMBER) and "
                         "`column` (the column holding the values to match)")
    elif w["op"] in ("in", "not_in", "contains_any") and not (
            isinstance(w.get("value"), list) or co.portees_citees(w.get("value"))):
        probs.append(f"`where[{i}]`: `{w['op']}` takes a list `value`")
    else:
        _portees(probs, f"`where[{i}].value`", w.get("value"), portees)


def _correspondance(probs: list, mapping: Any, portees: set) -> None:
    if not isinstance(mapping, dict) or not mapping:
        probs.append("`map` must be a non-empty object {column: spec}")
        return
    for col, spec in mapping.items():
        if not _colonne_ok(col):
            probs.append(f"`map`: `{col}` is not a valid column name")
        if isinstance(spec, str):
            continue
        if not isinstance(spec, dict) or not ({"path", "template", "const"} & set(spec)):
            probs.append(f"`map.{col}`: a path string, or an object with `path`, "
                         "`template` or `const`")
            continue
        if "template" in spec:
            _portees(probs, f"`map.{col}.template`", spec["template"], portees)
        if "max" in spec and (not isinstance(spec["max"], int) or spec["max"] < 1):
            probs.append(f"`map.{col}.max` must be a positive integer")


def valider(corps: Any) -> dict:
    """Le corps normalisé (défauts posés), ou `RecetteInvalide` avec TOUS les défauts."""
    probs: list[str] = []
    if not isinstance(corps, dict):
        raise RecetteInvalide(["the recipe must be an object"])
    c = copy.deepcopy(corps)
    mode = c.setdefault("mode", "pull")
    if mode != "pull":
        probs.append("`mode`: only `pull` is available in this version")
    outil = c.get("tool")
    if not isinstance(outil, str) or not outil:
        probs.append("`tool` (the connector tool to call) is required")
    elif namespace_of(outil) in NAMESPACES_INTERDITS:
        probs.append(f"`tool`: `{outil}` is a platform tool — a recipe only calls "
                     "connector tools")
    elif namespace_of(outil) in NAMESPACES_A_MODELE:
        probs.append(f"`tool`: `{outil}` runs a model — a recipe never calls one (to "
                     "judge rows with a model, use `jev_rows`)")
    fe = _pour_chaque(probs, c.get("for_each"))
    if fe is None:
        c.pop("for_each", None)
    # `{{row.…}}` n'existe que sous `for_each` : ailleurs, une faute de frappe.
    p_args = PORTEES_ARGUMENTS | ({PORTEE_LIGNE} if fe else set())
    p_elem = PORTEES_ELEMENT | ({PORTEE_LIGNE} if fe else set())
    args = c.setdefault("arguments", {})
    if not isinstance(args, dict):
        probs.append("`arguments` must be an object")
    else:
        _portees(probs, "`arguments`", args, p_args)
    params = c.setdefault("params", {})
    if not isinstance(params, dict):
        probs.append("`params` must be an object {name: {required, default}}")
    c["source"] = _source(probs, c.get("source"))
    for i, w in enumerate(c.setdefault("where", []) or []):
        _clause(probs, i, w, p_args)
    _correspondance(probs, c.get("map"), p_elem)
    valeurs = c.setdefault("values", {})
    if not isinstance(valeurs, dict):
        probs.append("`values` must be an object {column: value or template}")
    else:
        for col, gab in valeurs.items():
            if not _colonne_ok(col):
                probs.append(f"`values`: `{col}` is not a valid column name")
            _portees(probs, f"`values.{col}`", gab, p_args)
    cle = c.get("key")
    if not isinstance(cle, dict) or not _colonne_ok(cle.get("column")):
        probs.append("`key.column` (the column that identifies a row) is required")
    elif cle.get("template") is not None:
        _portees(probs, "`key.template`", cle["template"], p_elem)
    elif cle["column"] not in (c.get("map") or {}):
        probs.append("`key`: without `key.template`, `key.column` must be one of the "
                     "`map` columns")
    if c.setdefault("on_existing", "skip") not in ("skip", "update"):
        probs.append("`on_existing` must be `skip` or `update`")
    lim = c.get("limits")
    if not isinstance(lim, dict) or not isinstance(lim.get("max_units"), int) \
            or isinstance(lim["max_units"], bool) or lim["max_units"] < 1:
        probs.append("`limits.max_units` (a hard cap, in the tool's own billed units, at "
                     "least 1) is required: there is no default, because units "
                     "differ per tool")
    else:
        pages = lim.get("max_pages")
        if pages is not None and (not isinstance(pages, int) or isinstance(pages, bool)
                                  or pages < 1):
            probs.append("`limits.max_pages` is at least 1, or absent for no limit")
    if c.setdefault("units", "items") not in ("items", "calls"):
        probs.append("`units` must be `items` (one unit per item returned) or `calls`")
    pag = (c.get("source") or {}).get("pagination") or {}
    if c["units"] == "items" and pag.get("type") == "page" and isinstance(pag.get("size"), int) \
            and isinstance(lim, dict) and isinstance(lim.get("max_units"), int) \
            and lim["max_units"] < pag["size"]:
        probs.append("`limits.max_units` must be at least `source.pagination.size`: a page "
                     "is never cut, so a cap below one page would never call the tool")
    if probs:
        raise RecetteInvalide(probs)
    return c


#: L'affichage d'une recette (`display`) : ce qu'une page montre à l'endroit où un
#: processus la lance. Pas d'exécution ici — des mots et des chiffres déclarés.
MAX_ETAPES = 10
LABEL_MAX = 60
DETAIL_MAX = 200
SAVES_MAX = 160
CREDITS_MAX = 10_000
UNITES_CREDIT = ("contact", "company", "row")
_CLES_ETAPE = {"label", "detail", "credits", "per"}
_CLES_AFFICHAGE = {"steps", "saves"}


def valider_affichage(affichage: Any) -> dict:
    """`display` normalisé, ou `RecetteInvalide` avec TOUS les défauts.

    `{steps: [{label, detail?, credits?, per?}], saves?}` : 1 à `MAX_ETAPES` étapes dans l'ordre
    où la recette les fait ; `credits` (≥ 0) ce que l'étape facture par `per`
    (`contact` | `company` | `row`), absent = gratuite ; `saves` une phrase MESURÉE de
    ce que la recette épargne (jamais une estimation)."""
    probs: list[str] = []
    if not isinstance(affichage, dict):
        raise RecetteInvalide(["`display` must be an object {steps, saves?}"])
    inconnues = sorted(set(affichage) - _CLES_AFFICHAGE)
    if inconnues:
        probs.append(f"`display`: unknown keys {inconnues} (allowed: {sorted(_CLES_AFFICHAGE)})")
    etapes = affichage.get("steps")
    if not isinstance(etapes, list) or not 1 <= len(etapes) <= MAX_ETAPES:
        probs.append(f"`display.steps` must be a list of 1 to {MAX_ETAPES} steps, in the "
                     "order the recipe runs them")
        etapes = []
    propres = []
    for i, e in enumerate(etapes):
        ou = f"`display.steps[{i}]`"
        if not isinstance(e, dict):
            probs.append(f"{ou} must be an object {{label, detail?, credits?, per?}}")
            continue
        inc = sorted(set(e) - _CLES_ETAPE)
        if inc:
            probs.append(f"{ou}: unknown keys {inc}")
        label = e.get("label")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= LABEL_MAX:
            probs.append(f"{ou}.label: 1 to {LABEL_MAX} characters (what the step does)")
        detail = e.get("detail", "")
        if not isinstance(detail, str) or len(detail) > DETAIL_MAX:
            probs.append(f"{ou}.detail: a sentence of at most {DETAIL_MAX} characters")
        credits, per = e.get("credits"), e.get("per")
        if credits is not None and (isinstance(credits, bool)
                                    or not isinstance(credits, (int, float))
                                    or not 0 <= credits <= CREDITS_MAX):
            probs.append(f"{ou}.credits: a number of credits, 0 to {CREDITS_MAX}")
        if (credits is None) != (per is None):
            probs.append(f"{ou}: `credits` and `per` go together (omit both for a free step)")
        elif per is not None and per not in UNITES_CREDIT:
            probs.append(f"{ou}.per must be one of {list(UNITES_CREDIT)}")
        propre = {"label": label.strip() if isinstance(label, str) else label,
                  "detail": detail.strip() if isinstance(detail, str) else detail}
        if credits is not None:
            propre |= {"credits": credits, "per": per}
        propres.append(propre)
    saves = affichage.get("saves")
    if saves is not None and (not isinstance(saves, str)
                              or not 1 <= len(saves.strip()) <= SAVES_MAX):
        probs.append(f"`display.saves`: one measured sentence, at most {SAVES_MAX} characters")
    if probs:
        raise RecetteInvalide(probs)
    out: dict = {"steps": propres}
    if saves:
        out["saves"] = saves.strip()
    return out


def params_resolus(corps: dict, fournis: Any) -> dict:
    """Les paramètres d'une exécution : ceux fournis, puis les défauts déclarés. Refus
    si un paramètre requis manque ou si un inconnu est passé (une faute de frappe ne
    doit pas filtrer en silence sur une valeur vide)."""
    declares = corps.get("params") or {}
    fournis = dict(fournis or {})
    probs = [f"unknown param `{k}` (declared: {sorted(declares) or 'none'})"
             for k in fournis if declares and k not in declares]
    out: dict = {}
    for nom, spec in declares.items():
        spec = spec if isinstance(spec, dict) else {}
        if nom in fournis:
            out[nom] = fournis[nom]
        elif "default" in spec:
            out[nom] = spec["default"]
        elif spec.get("required"):
            probs.append(f"missing required param `{nom}`")
    if not declares:
        out = fournis
    if probs:
        raise RecetteInvalide(probs)
    return out
