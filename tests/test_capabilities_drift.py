"""Tests anti-drift de la couche capacité (ADR 0009) : parité + unicité.

Garantissent que ce qui est déclaré au registre est réellement monté, et qu'un
nom de tool n'est pas enregistré deux fois (legacy + capacité).
"""
import ast
import asyncio
import pathlib

import pytest
from fastmcp import FastMCP

from oto_mcp.capabilities import _mcp_adapter, _rest_adapter, registry

# Modules `tools/<m>.py` chargés EXPLICITEMENT par register_all (spine + génériques),
# hors dérivation du registre (cf. oto_mcp/tools/__init__.py). Le reste des fichiers
# tools/ DOIT être dérivé du registre `kind="tools"`. Cette liste-ci change rarement
# (≠ un nouveau connecteur, qui n'a RIEN à ajouter ici).
_EXPLICIT_TOOL_MODULES = {
    "meta", "profile", "whoami", "guide", "email", "datastore", "docs_app",
    "datastore_review_app", "datastore_table_app", "procedure_app", "connector_app", "guide_run", "remote", "mount",
}


_TOOLS_DIR = pathlib.Path(__file__).resolve().parent.parent / "oto_mcp" / "tools"


def _tools_module_files() -> set[str]:
    """Les modules de `tools/` qui prétendent SERVIR un outil, c'est-à-dire ceux
    qui définissent un `register()`.

    Un module de `tools/` sans `register()` n'est pas un connecteur qui dort : il
    ne peut pas être monté, donc il n'a rien à déclarer au registre. C'est un
    HELPER partagé entre modules voisins (`mail_obfuscation`, importé par
    `serper`). Le critère est dérivé du fichier, pas d'une liste de noms à tenir
    à jour : c'est la classe qui est fermée, pas les cas connus."""
    modules = set()
    for f in _TOOLS_DIR.glob("*.py"):
        if f.stem == "__init__":
            continue
        arbre = ast.parse(f.read_text(encoding="utf-8"))
        if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "register" for n in arbre.body):
            modules.add(f.stem)
    return modules


def test_mcp_caps_are_mounted():
    m = FastMCP("t")
    _mcp_adapter.register(m, registry.CAPABILITIES)

    async def go():
        for cap in registry.caps_with_mcp():
            if not cap.is_exposed():  # feature flag off (dark launch) → non montée
                continue
            assert await m.get_tool(cap.mcp) is not None, cap.key

    asyncio.run(go())


def test_tools_module_derivation_matches_filesystem():
    """register_all dérive le chargement du registre (`Connector.modules` ou le nom,
    #24). Garde-fou AUTO-MAINTENU (≠ liste figée à resync à chaque connecteur) :
    le filesystem `tools/*.py` EST la source de vérité, croisée avec le registre.

    1. Aucun module fantôme : tout module dérivé du registre a son fichier (sinon
       faute dans `modules=`/nom du provider → ImportError silencieux au boot).
    2. Aucun fichier orphelin : tout `tools/<m>.py` est soit dérivé du registre,
       soit chargé explicitement (spine/générique) — sinon un connecteur posé mais
       jamais déclaré au registre dort, invisible. Pur registre+FS, dep-indépendant."""
    from oto_mcp import providers
    derived: set[str] = set()
    for c in providers.REGISTRY.values():
        if c.kind == "tools":
            derived |= set(c.modules or (c.name,))
    files = _tools_module_files()

    phantom = derived - files
    assert not phantom, f"providers référencent des modules sans fichier tools/: {sorted(phantom)}"

    orphan = files - derived - _EXPLICIT_TOOL_MODULES
    assert not orphan, (
        f"tools/<m>.py ni dérivés du registre ni chargés explicitement: {sorted(orphan)} "
        "— déclarer le connecteur dans providers/<nom>.py, ou l'ajouter à _EXPLICIT_TOOL_MODULES si c'est un module spine")


def test_tools_datastores_are_matchable():
    """Un datastore de provider kind="tools" doit pouvoir être produit par
    `namespace_of(tool)` (= 1er token avant `_`) — sinon le gate d'activation
    fail-open en silence. Un datastore multi-mot (`culture_spectacle`) ne matche
    JAMAIS → bug (#24). Pur registre, dep-indépendant.

    (L'ex-datastore `sirene_stock` qui forçait une whitelist a été fusionné dans
    le connecteur `sirene` sous le datastore `fr` — tools `fr_stock_*`, 2026-06-22.)"""
    from oto_mcp import providers
    from oto_mcp.tool_visibility import namespace_of

    for c in providers.REGISTRY.values():
        if c.kind != "tools":
            continue
        for ns in c.namespaces:
            assert namespace_of(f"{ns}_x") == ns, (
                f"datastore non matchable {c.name}:{ns} (multi-mot → fail-open du gate)")


def test_rest_caps_are_mounted():
    routes = _rest_adapter.make_routes(None, None, None, None, None, registry.CAPABILITIES)
    paths = {r.path for r in routes}
    for cap in registry.caps_with_rest():
        if not cap.is_exposed():  # feature flag off (dark launch) → non montée
            continue
        for b in cap.rest_bindings():
            assert b.path in paths, cap.key


def test_mcp_names_unique_within_registry():
    names = [c.mcp for c in registry.caps_with_mcp()]
    assert len(names) == len(set(names))


def test_rest_paths_unique_within_registry():
    keys = [(b.verb, b.path) for c in registry.caps_with_rest() for b in c.rest_bindings()]
    assert len(keys) == len(set(keys))


def test_no_mcp_name_collision_with_legacy():
    """Aucun nom de capacité MCP n'est aussi enregistré par un register() legacy.
    Skip si les deps optionnelles manquent en local (CI les a)."""
    from oto_mcp.tools import register_all
    m = FastMCP("t")
    try:
        register_all(m)
    except Exception as e:  # france_opendata & co. absents du venv local
        pytest.skip(f"register_all indisponible: {e}")
    _mcp_adapter.register(m, registry.CAPABILITIES)  # lèverait si doublon

    async def go():
        for cap in registry.caps_with_mcp():
            if not cap.is_exposed():  # feature flag off (dark launch) → non montée
                continue
            assert await m.get_tool(cap.mcp) is not None, cap.key

    asyncio.run(go())


def test_feature_gate_hides_surface(monkeypatch):
    """Le gate (ADR 0043, dark launch) montre/masque la surface d'une capacité au
    MONTAGE selon l'env, sans jamais quitter le registre. Billing = pilote."""
    billing_keys = {c.key for c in registry.CAPABILITIES if c.key.startswith("billing.")}
    assert billing_keys, "billing doit être au registre (introspection/catalogue)"

    def rest_paths():
        rs = _rest_adapter.make_routes(None, None, None, None, None, registry.CAPABILITIES)
        return {r.path for r in rs}

    # OFF (défaut prod) : billing absent de la surface REST montée.
    monkeypatch.setenv("OTO_BILLING_ENABLED", "0")
    assert not any(p.startswith("/api/me/billing") or p == "/api/billing/plans" for p in rest_paths())

    # ON (canari / go-live) : billing exposé.
    monkeypatch.setenv("OTO_BILLING_ENABLED", "1")
    assert "/api/billing/plans" in rest_paths()
