"""Résolution des références d'outils d'une doctrine (ADR 0014).

Source UNIQUE partagée par les deux faces :
- REST `/api/me/tools/registry` (le dashboard résout les `<tool:slug>` côté UI) ;
- MCP `get_claude_md` / `oto_get_instruction` (manifeste « referenced_tools »
  appended à la livraison, pour que l'AGENT voie les noms canoniques, la
  description tirée de l'outil, et le **drift** d'une référence morte).

« derive don't duplicate » : la logique « marqueur → outil réel » ne vit qu'ici.
"""
from __future__ import annotations

import re

from . import providers
from .tool_visibility import is_grant_only, namespace_of

_MARKER = re.compile(r"<tool:([a-z0-9_]+)>")


def ref_names(text: str) -> list[str]:
    """Noms d'outils cités via `<tool:slug>`, dédupliqués, dans l'ordre."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _MARKER.finditer(text or ""):
        n = m.group(1)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _entry(tool) -> dict:
    """Entrée registre d'un tool MCP : nom + description (1ʳᵉ ligne de la docstring
    = champ `description`, ce que le modèle voit déjà) + source native/federated."""
    conn = providers.connector_for_namespace(namespace_of(tool.name))
    federated = bool(conn and conn.kind == "mount")
    e = {
        "name": tool.name,
        "description": (tool.description or "").strip().split("\n", 1)[0].strip(),
        "source": "federated" if federated else "native",
    }
    if federated and conn:
        e["mcp"] = conn.name
    return e


async def build_registry(mcp_instance) -> dict[str, dict]:
    """Map nom → entrée pour tous les tools exposés (hors grant-only)."""
    if mcp_instance is None:
        return {}
    tools = await mcp_instance.list_tools(run_middleware=False)
    return {t.name: _entry(t) for t in tools if not is_grant_only(t.name)}


def resolve_refs(names: list[str], registry: dict[str, dict]) -> list[dict]:
    """Manifeste : pour chaque nom, l'outil résolu (`status=ok`) ou un signal de
    drift (`status=missing`) — la référence n'existe plus dans le registre."""
    out: list[dict] = []
    for name in names:
        entry = registry.get(name)
        out.append({**entry, "status": "ok"} if entry else {"name": name, "status": "missing"})
    return out


async def manifest_for(mcp_instance, *texts: str) -> list[dict]:
    """Manifeste « outils référencés » des corps `texts` (base + groupe, ou un
    skill). **Court-circuit zéro-coût** : aucune liste de tools n'est construite
    si les corps ne citent aucun outil (cas des doctrines legacy en backticks)."""
    names: list[str] = []
    seen: set[str] = set()
    for t in texts:
        for n in ref_names(t):
            if n not in seen:
                seen.add(n)
                names.append(n)
    if not names:
        return []
    registry = await build_registry(mcp_instance)
    return resolve_refs(names, registry)
