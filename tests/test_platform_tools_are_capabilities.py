"""Garde-fou : un verbe de PLATEFORME est une capacité, pas un tool écrit à la main.

ADR 0042 §Convergence des surfaces, Décision 4. Deux régimes d'exposition coexistent :
la **capacité** (`capabilities/`, les adaptateurs GÉNÈRENT la face MCP et/ou REST depuis
un descripteur unique) et le **tool `@mcp.tool()` écrit à la main** (`tools/`, MCP-only
par construction). Le second est le régime normal des CONNECTEURS (`fr_*`, `folk_*` :
une seule face, jamais de REST) ; il est toxique pour un verbe de plateforme, car le jour
où le dashboard en a besoin on ne peut pas dériver la face REST — on écrit une SECONDE
implémentation. C'est arrivé deux fois (`oto_profile`, `oto_guide`), chaque fois avec sa
propre autz à tenir en phase, et des trous asymétriques quand une face manquait.

Ce test fige la liste des résidus. Elle doit DÉCROÎTRE : ajouter un `oto_*`/`run_*`
écrit à la main casse la CI (le réflexe attendu = le déclarer en capacité), et migrer un
résidu casse aussi (retirer sa ligne ici). Discipline mécanique plutôt que tenue à la main
— cf. la leçon des tripwires d'org/ownership.
"""
from __future__ import annotations

import ast
import pathlib

TOOLS_DIR = pathlib.Path(__file__).resolve().parent.parent / "oto_mcp" / "tools"

# Préfixes de la surface PLATEFORME (≠ un connecteur, qui a son propre namespace).
_PLATFORM_PREFIXES = ("oto_", "run_", "data_", "feedback")

# Résidus CONNUS, avec leur raison. `True` = MCP-only par NATURE (aucune face REST
# n'aurait de sens) ; `False` = DETTE (une face REST existe déjà, écrite à la main
# ailleurs → à fusionner en capacité).
_KNOWN: dict[str, bool] = {
    # Dispatch universel (ADR 0036) : exécute une cible par son nom via `Tool.run` sur
    # l'instance FastMCP — le dashboard n'appelle pas des tools, il appelle des routes.
    "oto_call": True,
    "oto_tool_schema": True,
    # Identité de la SESSION MCP courante (org/groupe résolus pour cette conversation).
    "oto_whoami": True,
    # MCP App (SEP-1865) : renvoie une UI `prefab_ui` peinte par le host.
    "oto_doc_app": True,
    "data_app": True,
    # Boucle d'usage (ADR 0017) : la pile de run est session-scopée côté MCP.
    # (`feedback`, lui, est DÉJÀ une capacité — `capabilities/usage.py`.)
    "run_start": True,
    "run_finish": True,
    # DETTE — miroir de `/api/me/tools…`, donc deux implémentations de la même règle
    # de visibilité. ⚠️ Depuis le 2026-08-27 la face REST n'est plus écrite à la main :
    # ce sont des capacités (`capabilities/tools_me.py`). La dette restante est celle-ci,
    # et elle seule — mais elle ne se rembourse PAS par un refactor : les deux faces n'ont
    # pas la même forme (l'outil MCP prend un `query` et rend une projection de recherche ;
    # le REST rend la liste complète pour peindre une grille de gouvernance). Les unifier
    # casse l'une des deux : décision de contrat, suivie en oto-backend#429.
    "oto_list_my_tools": False,
    "oto_enable_tool": False,
    "oto_disable_tool": False,
    # DETTE — le datastore expose data_* en MCP et /api/datastore/* en REST (deux
    # implémentations du même métier, antérieures à la couche capacité).
    "data_rows": False,
    "data_write": False,
    "data_url": False,
    "data_list_namespaces": False,
    "data_create_namespace": False,
    "data_delete_namespace": False,
    "data_rename_namespace": False,
    "data_set_schema": False,
    "data_claim_next": False,
    "data_release": False,
    "data_aggregate": False,
    "data_delete_row": False,
    "data_share": False,
}


def _handwritten_tools() -> dict[str, str]:
    """`{nom de tool: module}` pour tout `@mcp.tool()` déclaré dans `tools/`."""
    found: dict[str, str] = {}
    for path in sorted(TOOLS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                target = deco.func if isinstance(deco, ast.Call) else deco
                if isinstance(target, ast.Attribute) and target.attr == "tool":
                    found[node.name] = path.name
    return found


def test_no_new_handwritten_platform_tool():
    """Un nouveau verbe de plateforme doit naître capacité (mcp= et/ou rest=)."""
    platform = {name: mod for name, mod in _handwritten_tools().items()
                if name.startswith(_PLATFORM_PREFIXES)}
    unexpected = {n: m for n, m in platform.items() if n not in _KNOWN}
    assert not unexpected, (
        f"Tools de plateforme écrits à la main hors liste connue : {unexpected}. "
        "Déclare-les comme des capacités dans `oto_mcp/capabilities/` (motif "
        "`platform.instructions` : une capacité op-aware pour le MCP + des capacités "
        "par-verbe pour REST, mêmes handlers) — cf. ADR 0042 §Convergence des surfaces.")
    migrated = {n for n in _KNOWN if n not in platform}
    assert not migrated, (
        f"Ces tools ne sont plus écrits à la main : {sorted(migrated)}. "
        "Retire-les de `_KNOWN` — la liste doit décroître, jamais mentir.")


def test_converged_verbs_are_capabilities_with_both_faces():
    """`oto_profile` et `oto_guide` : une capacité qui porte VRAIMENT les deux faces."""
    from oto_mcp.capabilities.registry import CAPABILITIES
    by_mcp = {c.mcp: c for c in CAPABILITIES if c.mcp}
    for tool in ("oto_profile", "oto_guide"):
        assert tool in by_mcp, f"{tool} doit être exposé par une capacité"
    keys = {c.key for c in CAPABILITIES}
    for rest_key in ("me.profile.get", "me.profile.set", "me.guides.get", "me.guides.set"):
        assert rest_key in keys, f"{rest_key} (face REST) manquante"
