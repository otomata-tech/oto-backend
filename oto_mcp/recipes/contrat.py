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
from typing import Any

from ..tool_visibility import namespace_of
from . import correspondance as co

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_COLONNE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
#: Familles qu'une recette n'appelle jamais : le méta et le spine se parlent
#: directement, et une recette qui appellerait `oto_call` contournerait ses propres
#: gardes. Miroir de `tools/meta._NON_DISPATCHABLE`.
NAMESPACES_INTERDITS = frozenset({"oto", "run", "feedback", "data"})
PAGINATIONS = ("page", "cursor", "none")
OPS_WHERE = ("eq", "ne", "in", "not_in", "contains_any", "empty", "not_empty")
PORTEES_ARGUMENTS = {"params"}
PORTEES_ELEMENT = {"params", "item"}
MAX_PAGES = 50
MAX_PAGES_DEFAUT = 20


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


def _correspondance(probs: list, mapping: Any) -> None:
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
            _portees(probs, f"`map.{col}.template`", spec["template"], PORTEES_ELEMENT)
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
    args = c.setdefault("arguments", {})
    if not isinstance(args, dict):
        probs.append("`arguments` must be an object")
    else:
        _portees(probs, "`arguments`", args, PORTEES_ARGUMENTS)
    params = c.setdefault("params", {})
    if not isinstance(params, dict):
        probs.append("`params` must be an object {name: {required, default}}")
    c["source"] = _source(probs, c.get("source"))
    for i, w in enumerate(c.setdefault("where", []) or []):
        if not isinstance(w, dict) or not isinstance(w.get("path"), str) \
                or w.get("op") not in OPS_WHERE:
            probs.append(f"`where[{i}]`: needs `path` and `op` in {list(OPS_WHERE)}")
        elif w["op"] in ("in", "not_in", "contains_any") and not (
                isinstance(w.get("value"), list) or co.portees_citees(w.get("value"))):
            probs.append(f"`where[{i}]`: `{w['op']}` takes a list `value`")
        else:
            _portees(probs, f"`where[{i}].value`", w.get("value"), PORTEES_ARGUMENTS)
    _correspondance(probs, c.get("map"))
    valeurs = c.setdefault("values", {})
    if not isinstance(valeurs, dict):
        probs.append("`values` must be an object {column: value or template}")
    else:
        for col, gab in valeurs.items():
            if not _colonne_ok(col):
                probs.append(f"`values`: `{col}` is not a valid column name")
            _portees(probs, f"`values.{col}`", gab, PORTEES_ARGUMENTS)
    cle = c.get("key")
    if not isinstance(cle, dict) or not _colonne_ok(cle.get("column")):
        probs.append("`key.column` (the column that identifies a row) is required")
    elif cle.get("template") is not None:
        _portees(probs, "`key.template`", cle["template"], PORTEES_ELEMENT)
    elif cle["column"] not in (c.get("map") or {}):
        probs.append("`key`: without `key.template`, `key.column` must be one of the "
                     "`map` columns")
    if c.setdefault("on_existing", "skip") not in ("skip", "update"):
        probs.append("`on_existing` must be `skip` or `update`")
    lim = c.get("limits")
    if not isinstance(lim, dict) or not isinstance(lim.get("max_units"), int) \
            or lim["max_units"] < 1:
        probs.append("`limits.max_units` (a hard cap, in the tool's own billed units) is "
                     "required: there is no default, because units differ per tool")
    else:
        pages = lim.setdefault("max_pages", MAX_PAGES_DEFAUT)
        if not isinstance(pages, int) or not 1 <= pages <= MAX_PAGES:
            probs.append(f"`limits.max_pages` must be 1 to {MAX_PAGES}")
    if c.setdefault("units", "items") not in ("items", "calls"):
        probs.append("`units` must be `items` (one unit per item returned) or `calls`")
    if probs:
        raise RecetteInvalide(probs)
    return c


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
