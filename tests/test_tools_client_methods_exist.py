"""Garde-fou version-skew (otomata-private, leçon folk_get_user) — un tool ne doit
PAS référencer une méthode absente de l'oto-core ÉPINGLÉ.

Contexte : backend et oto-core sont deux repos ; le backend épingle une version
d'oto-core (pin git dans pyproject, ADR 0020). Un tool mergé en avance de phase —
qui appelle `client.methode()` avant que le tag épinglé la contienne — passe la
CI (l'import du module réussit, la méthode n'est touchée qu'à l'appel) puis lève
`AttributeError` en prod à la 1ʳᵉ invocation (vécu 2026-07-01→03 : `folk_get_user`
→ `FolkClient` sans `get_user` sur v1.11.0, corrigé par le bump v1.12.0).

Cette sonde ferme la fenêtre : en CI de PR, oto-core est installé AU TAG ÉPINGLÉ
(runner neuf → pin du pyproject) ; on vérifie STATIQUEMENT que chaque `_client().m()`
d'un tool existe sur la vraie classe. Une méthode manquante casse la PR au lieu
d'atteindre la prod.

Portée = la convention `def _client() -> <ClasseConcrète>` + appels `_client().<m>()`
(≈ 30 connecteurs, folk inclus). Les modules à autre pattern (`_client() -> tuple`
avec `client, is_platform = _client()`, dispatch `_run(...)`) sont SKIPPÉS et
listés (pas de couverture silencieuse — cf. principe « no silent caps »)."""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "oto_mcp" / "tools"


def _client_class_name(tree: ast.Module) -> str | None:
    """Nom de classe annoté en retour d'un `def _client(...) -> Name`. None si le
    module n'a pas de `_client` annoté par un simple Name (tuple/Subscript/absent)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_client":
            ret = node.returns
            if isinstance(ret, ast.Name):
                return ret.id
    return None


def _import_of(tree: ast.Module, clsname: str) -> str | None:
    """Module d'origine d'un `from <module> import <clsname>` (n'importe où, y
    compris imports imbriqués dans `register`)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name == clsname:
                    return node.module
    return None


def _methods_called_on_client(tree: ast.Module) -> set[str]:
    """Toutes les méthodes appelées via `_client().<m>` (Attribute dont la value est
    un Call à Name('_client'))."""
    methods: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "_client"):
            methods.add(node.attr)
    return methods


def _covered_modules() -> list[tuple[str, str, str, set[str]]]:
    """(module_tool, clsname, import_module, méthodes) pour chaque tool suivant la
    convention `_client() -> ClasseConcrète` avec ≥1 appel `_client().m()`."""
    out = []
    for path in sorted(_TOOLS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        cls = _client_class_name(tree)
        if not cls:
            continue
        methods = _methods_called_on_client(tree)
        if not methods:
            continue
        mod = _import_of(tree, cls)
        if not mod:
            continue
        out.append((path.stem, cls, mod, methods))
    return out


_CASES = _covered_modules()


def test_convention_coverage_not_silently_shrinking():
    """Filet anti-régression de couverture : si ce nombre chute brutalement (un
    connecteur bascule hors convention), la sonde couvre moins sans le dire."""
    covered = {c[0] for c in _CASES}
    assert "folk" in covered, "folk doit rester couvert (cas d'école du garde-fou)"
    assert len(_CASES) >= 20, f"couverture anormalement basse ({len(_CASES)} modules)"


@pytest.mark.parametrize("tool_mod, clsname, import_mod, methods",
                         _CASES, ids=[c[0] for c in _CASES])
def test_client_methods_exist_on_pinned_core(tool_mod, clsname, import_mod, methods):
    """Chaque `_client().m()` du tool existe sur la classe oto-core épinglée."""
    try:
        cls = getattr(importlib.import_module(import_mod), clsname)
    except Exception as e:  # noqa: BLE001 — extra non installé, etc.
        pytest.skip(f"{clsname} non importable ({import_mod}) : {e}")
    missing = sorted(m for m in methods if not hasattr(cls, m))
    assert not missing, (
        f"{tool_mod}.py appelle des méthodes absentes de {clsname} "
        f"(oto-core épinglé) : {missing} — bump le pin oto-core dans CETTE PR "
        f"(version-skew, cf. leçon folk_get_user).")
