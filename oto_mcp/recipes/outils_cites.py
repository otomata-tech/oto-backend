"""Les outils qu'une recette CITE — lus dans son corps, jamais devinés.

Une recette lancée par un agent hébergé n'appelle que les outils de la liste de son
travail (`outils_du_travail`, #1256). Jusqu'ici, la recette le découvrait au premier
appel refusé : un outil manquant par exécution, et autant d'exécutions perdues que
d'outils à ajouter. Lire ce qu'elle cite permet deux choses, avec la même liste :

- AVANT le run, sur l'agent (`runner_triggers._avec_tool_warnings`) : « la recette
  `x` de ta procédure appelle `y`, que l'agent n'a pas » — un bouton, un clic ;
- AU run (`code.executer`) : un seul refus qui nomme TOUS les outils manquants, avant
  le premier appel, plutôt que le premier d'entre eux après des écritures.

Recette déclarative : son `tool`, à la racine du corps — là où le moteur le lit
(`moteur.py`, `contrat.py`). Recette-code : chaque `oto.call("<nom>"` à nom LITTÉRAL
de ses sources. Un nom construit à l'exécution ne se lit pas ici : il reste jugé à
l'appel, comme avant — la liste est un plancher, jamais un plafond.

⚠️ 27/10/2026 : on lisait `source.tool`, une clé qu'aucune recette ne porte (`source`
ne tient que `items` et `pagination`). Toute recette déclarative citait donc `[]` :
un agent dont la procédure en lançait une ne voyait jamais son outil manquer, et
l'apprenait au run, refusé (`tool_not_in_job`).

⚠️ 23/11/2026 : citer n'est pas appeler. Une recette-code qui n'appelle le CRM que si
`params["crm"]` en nomme un était refusée d'emblée, au run, par un agent qui n'a pas de
CRM — sur un appel que son run ne fait jamais. `outils_inconditionnels` lit ce que
l'entrée appelle À COUP SÛR (hors `if`, boucle, `except`, expression conditionnelle,
fonction annexe) : seul un outil manquant de cette liste-là refuse avant le premier
appel (`code.executer`). Les autres restent dits — sur l'agent, et dans le reçu — et
jugés à l'appel, comme tout appel (`pont.appeleur_de_recette`).
"""
from __future__ import annotations

import ast
import re
from typing import Any, Optional

_APPEL = re.compile(r"""\boto\s*\.\s*call\s*\(\s*["']([A-Za-z0-9_]+)["']""")
#: Une procédure lance une recette en la nommant : `slug="<recette>"` près d'`oto_recipe`.
_SLUG = re.compile(r"""slug\s*[=:]\s*["`']?([a-z0-9][a-z0-9-]{1,80})""")


def outils_cites(corps: Any) -> list[str]:
    """Les outils que ce corps de recette appelle, dédupliqués, dans l'ordre."""
    if not isinstance(corps, dict):
        return []
    noms: list[str] = []
    if isinstance(corps.get("tool"), str):
        noms.append(corps["tool"])
    for texte in (corps.get("sources") or {}).values():
        if isinstance(texte, str):
            noms += _APPEL.findall(texte)
    vus: set[str] = set()
    return [n for n in noms if not (n in vus or vus.add(n))]


def recettes_citees(texte: str) -> list[str]:
    """Les slugs de recette qu'une procédure nomme (`oto_recipe … slug="x"`) — des
    candidats : l'appelant ne garde que ceux qui sont des recettes de l'org."""
    if "oto_recipe" not in (texte or ""):
        return []
    vus: set[str] = set()
    return [s for s in _SLUG.findall(texte) if not (s in vus or vus.add(s))]


#: Les nœuds d'expression dont une partie seulement s'évalue : un appel dedans peut ne
#: jamais partir (`a if c else b`, `x and y`, compréhensions, lambda).
_EXPR_CONDITIONNELLES = (ast.IfExp, ast.BoolOp, ast.Lambda, ast.ListComp, ast.SetComp,
                         ast.DictComp, ast.GeneratorExp, ast.NamedExpr)


def _appels_d_expression(noeud: ast.AST, noms: list[str]) -> None:
    """Les `oto.call("<nom>"…)` d'une expression qui s'évalue en entier — on ne descend
    pas dans ses parties conditionnelles."""
    if isinstance(noeud, _EXPR_CONDITIONNELLES):
        return
    if isinstance(noeud, ast.Call):
        f = noeud.func
        if (isinstance(f, ast.Attribute) and f.attr == "call"
                and isinstance(f.value, ast.Name) and f.value.id == "oto"
                and noeud.args and isinstance(noeud.args[0], ast.Constant)
                and isinstance(noeud.args[0].value, str)):
            noms.append(noeud.args[0].value)
    for enfant in ast.iter_child_nodes(noeud):
        _appels_d_expression(enfant, noms)


def _appels_du_corps(instructions: list, noms: list[str]) -> None:
    """Les appels qu'une suite d'instructions fait à coup sûr : chaque instruction
    simple, la condition d'un `if`/`while` et l'itérable d'un `for` (évalués une fois au
    moins), le corps d'un `with` et d'un `try` (et son `finally`) — jamais une branche,
    un corps de boucle, un `except` ni une fonction définie là."""
    for ins in instructions:
        if isinstance(ins, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(ins, (ast.If, ast.While)):
            _appels_d_expression(ins.test, noms)
        elif isinstance(ins, (ast.For, ast.AsyncFor)):
            _appels_d_expression(ins.iter, noms)
        elif isinstance(ins, (ast.With, ast.AsyncWith)):
            for item in ins.items:
                _appels_d_expression(item.context_expr, noms)
            _appels_du_corps(ins.body, noms)
        elif isinstance(ins, ast.Try) or type(ins).__name__ == "TryStar":
            _appels_du_corps(ins.body, noms)
            _appels_du_corps(ins.finalbody, noms)
        elif isinstance(ins, ast.Match):
            _appels_d_expression(ins.subject, noms)
        else:
            _appels_d_expression(ins, noms)
        if isinstance(ins, (ast.Return, ast.Raise)):
            return


def outils_inconditionnels(corps: Any) -> Optional[list[str]]:
    """Les outils que l'entrée d'une recette-code appelle À COUP SÛR, dans l'ordre,
    dédupliqués. `None` quand on ne sait pas lire l'entrée (source absente, syntaxe,
    fonction introuvable) : l'appelant retombe alors sur `outils_cites`, le plancher
    d'avant. Une recette déclarative appelle son `tool` à chaque run."""
    if not isinstance(corps, dict):
        return None
    if corps.get("kind") != "code":
        return outils_cites(corps)
    module, _, fonction = str(corps.get("entrypoint") or "").partition(":")
    texte = (corps.get("sources") or {}).get(f"{module}.py")
    if not (fonction and isinstance(texte, str)):
        return None
    try:
        arbre = ast.parse(texte)
    except SyntaxError:
        return None
    entree = next((n for n in arbre.body if isinstance(n, (ast.FunctionDef,
                   ast.AsyncFunctionDef)) and n.name == fonction), None)
    if entree is None:
        return None
    noms: list[str] = []
    _appels_du_corps(entree.body, noms)
    vus: set[str] = set()
    return [n for n in noms if not (n in vus or vus.add(n))]
