"""Aucun état d'environnement ne fuit d'un fichier de tests à l'autre (#1111).

Une écriture dans `os.environ` faite à l'IMPORT d'un fichier de tests (au niveau module,
hors de toute fonction) vaut pour TOUT le processus dès la collecte : chaque test collecté
ensuite la voit, sans l'avoir déclarée. Sur un runner unique, chaque worker collectait
toute la suite, donc le voisin était toujours là ; dans une suite répartie en parts, il
n'y est plus. Vécu deux fois :
  - le 15/09/2026, l'adresse publique posée par un fichier d'authentification faisait
    passer au vert des bancs qui ne la déclaraient pas (cf. `tests/conftest.py`) ;
  - le 09/10/2026, `tests/api/test_runner_fleets_rest.py` ne trouvait plus
    `OTO_MCP_PUBLIC_URL` dans la part qui ne contenait pas les fichiers Google qui la
    posaient à l'import (17 erreurs, tronc rouge).

La règle vise l'AXE, pas un fichier : nulle part dans `tests/` (conftest compris, helpers
compris), une écriture d'environnement ne s'exécute à l'import. Une variable dont un banc a
besoin se pose par `monkeypatch` dans une fixture (elle se déclare, se voit, s'annule) ;
une base commune à toute la suite vit dans la fixture de SESSION du conftest racine
(`_base_d_environnement_de_la_session`), qui s'installe avant toute fixture de module.
"""
from __future__ import annotations

import ast
from pathlib import Path

RACINE_TESTS = Path(__file__).resolve().parent

_ECRITURES = {"setdefault", "update", "pop", "popitem", "clear", "__setitem__", "__delitem__"}


def _touche_environ(noeud: ast.AST) -> bool:
    texte = ast.unparse(noeud)
    return "environ" in texte


def ecritures_a_l_import(source: str) -> list[tuple[int, str]]:
    """(ligne, code) de chaque écriture d'environnement exécutée à l'import du module :
    tout le corps du module et des classes, SAUF l'intérieur des fonctions et lambdas
    (qui ne s'exécutent qu'appelées)."""
    trouvees: list[tuple[int, str]] = []

    def visiter(noeud: ast.AST) -> None:
        if isinstance(noeud, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return
        if isinstance(noeud, ast.Call) and isinstance(noeud.func, ast.Attribute):
            f = noeud.func
            if f.attr in _ECRITURES and _touche_environ(f.value):
                trouvees.append((noeud.lineno, ast.unparse(noeud)))
            elif f.attr in ("putenv", "unsetenv"):
                trouvees.append((noeud.lineno, ast.unparse(noeud)))
        cibles: list[ast.AST] = []
        if isinstance(noeud, (ast.Assign, ast.Delete)):
            cibles = list(noeud.targets)
        elif isinstance(noeud, (ast.AugAssign, ast.AnnAssign)):
            cibles = [noeud.target]
        for c in cibles:
            if isinstance(c, ast.Subscript) and _touche_environ(c.value):
                trouvees.append((noeud.lineno, ast.unparse(noeud)))
        for enfant in ast.iter_child_nodes(noeud):
            visiter(enfant)

    visiter(ast.parse(source))
    return trouvees


def test_aucune_ecriture_d_environnement_a_l_import_dans_tests():
    fautes = []
    for chemin in sorted(RACINE_TESTS.rglob("*.py")):
        if "__pycache__" in chemin.parts:
            continue
        for ligne, code in ecritures_a_l_import(chemin.read_text(encoding="utf-8")):
            fautes.append(f"{chemin.relative_to(RACINE_TESTS.parent)}:{ligne}: {code}")
    assert not fautes, (
        "Écriture d'environnement exécutée à l'IMPORT d'un fichier de tests : elle vaut pour "
        "tout le processus et fait passer des bancs d'AUTRES fichiers qui ne la déclarent "
        "pas (#1111). La poser par `monkeypatch` dans une fixture, ou dans la base de "
        "session du conftest racine :\n" + "\n".join(fautes))


def test_temoin_le_balayage_voit_chaque_forme_et_ignore_les_fonctions():
    """Sans ce témoin, le banc du dessus serait vert pour la mauvaise raison (un balayage
    aveugle)."""
    source = '''
import os
from os import environ
os.environ.setdefault("A", "1")
os.environ["B"] = "2"
environ.update({"C": "3"})
del os.environ["D"]
os.putenv("E", "5")
if True:
    os.environ.pop("F", None)
class K:
    os.environ["G"] = "7"
    def m(self):
        os.environ["H"] = "8"
def f(monkeypatch):
    os.environ["I"] = "9"
g = lambda: os.environ.setdefault("J", "10")
x = os.environ.get("K")
'''
    lignes = [l for l, _ in ecritures_a_l_import(source)]
    assert lignes == [4, 5, 6, 7, 8, 10, 12]
