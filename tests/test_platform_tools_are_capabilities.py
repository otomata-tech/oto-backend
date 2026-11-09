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

# Préfixes de la surface PLATEFORME (≠ un connecteur, qui a son propre datastore).
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
    # …et la file de revue : son point d'entrée (`@app.ui()`) et le gestionnaire de
    # ses boutons (`@app.tool()`, app-only, jamais listé au modèle).
    "data_review_app": True,
    "data_review_decide": True,
    # …et le graphe d'une procédure, peint par le bundle d'un front (`procedure_app`).
    "oto_procedure_app": True,
    # …et la carte « Connecter » d'un connecteur (`connector_app`) : un bouton qui
    # OUVRE un lien, la face REST de la même chose étant `oto_connector op=setup_link`.
    "oto_connector_app": True,
    # Boucle d'usage (ADR 0017) : la pile de run est session-scopée côté MCP.
    # (`feedback`, lui, est DÉJÀ une capacité — `capabilities/usage.py`.)
    "run_start": True,
    "run_finish": True,
    # DETTE — le datastore expose data_* en MCP et /api/datastore/* en REST (deux
    # implémentations du même métier, antérieures à la couche capacité).
    "data_rows": False,
    "data_write": False,
    "data_url": False,
    "data_list_datastores": False,
    "data_create_datastore": False,
    "data_delete_datastore": False,
    "data_rename_datastore": False,
    "data_set_schema": False,
    "data_claim_next": False,
    "data_release": False,
    "data_aggregate": False,
    "data_delete_row": False,
    "data_share": False,
}


def _handwritten_tools() -> dict[str, str]:
    """`{nom de tool: module}` pour tout `@mcp.tool()` déclaré dans `tools/`.

    `@app.ui()` compte aussi : un point d'entrée `FastMCPApp` est un tool servi au
    modèle, et ne pas le voir ici le ferait passer sous la garde sans un mot."""
    found: dict[str, str] = {}
    for path in sorted(TOOLS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                target = deco.func if isinstance(deco, ast.Call) else deco
                if isinstance(target, ast.Attribute) and target.attr in ("tool", "ui"):
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


def test_la_toolbox_est_une_capacite_a_deux_faces():
    """#429 : masquer/démasquer est UNE capacité par geste, servie au dashboard et à
    l'agent ; la liste a deux capacités sur un noyau partagé (`tools/catalogue.py`),
    parce que les deux faces ne posent pas la même question."""
    from oto_mcp.capabilities.registry import CAPABILITIES
    par_cle = {c.key: c for c in CAPABILITIES}
    for cle, outil in (("me.tools.disable", "oto_disable_tool"),
                       ("me.tools.enable", "oto_enable_tool")):
        assert par_cle[cle].mcp == outil and par_cle[cle].rest, cle
    assert par_cle["me.tools.search"].mcp == "oto_list_my_tools"
    assert par_cle["me.tools.list"].rest and par_cle["me.tools.list"].mcp is None
