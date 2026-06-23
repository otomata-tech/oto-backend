"""Résolution des références d'outils d'une doctrine (ADR 0014).

Source UNIQUE partagée par les deux faces :
- REST `/api/me/tools/registry` (le dashboard résout les `<tool:slug>` côté UI) ;
- MCP `oto_get_doctrine` / `oto_get_doctrine` (manifeste « referenced_tools »
  appended à la livraison, pour que l'AGENT voie les noms canoniques, la
  description tirée de l'outil, et le **drift** d'une référence morte).

« derive don't duplicate » : la logique « marqueur → outil réel » ne vit qu'ici.
"""
from __future__ import annotations

import re

from . import providers
from .tool_visibility import is_grant_only, namespace_of

_MARKER = re.compile(r"<tool:([a-z0-9_]+)>")

# Instance FastMCP du serveur, liée au boot (`server._build_mcp` → `bind`). Les
# handlers de la couche capacité ne reçoivent que `(ctx, inp)` — pas l'instance —
# et la face REST n'a pas de contexte MCP : ce singleton leur sert de défaut pour
# résoudre le registre d'outils (manifeste « referenced_tools », ADR 0014).
_INSTANCE = None


def bind(instance) -> None:
    """Mémorise l'instance FastMCP servie (appelée une fois au boot)."""
    global _INSTANCE
    _INSTANCE = instance


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


def namespaces_in(text: str) -> set[str]:
    """Namespaces (1er token avant `_`) des outils référencés `<tool:slug>` dans
    `text`. Sert le compteur « référencé par N doctrines » (posture doctrine-only,
    ADR 0024) — dérivation pure, sans toucher au registre live."""
    return {n.split("_", 1)[0] for n in ref_names(text)}


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


async def build_registry(mcp_instance=None) -> dict[str, dict]:
    """Map nom → entrée pour tous les tools exposés (hors grant-only).
    `mcp_instance` omise = l'instance liée au boot (`bind`)."""
    mcp_instance = mcp_instance or _INSTANCE
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


async def manifest_for(*texts: str, mcp_instance=None) -> list[dict]:
    """Manifeste « outils référencés » des corps `texts` (base + groupe, ou un
    skill). **Court-circuit zéro-coût** : aucune liste de tools n'est construite
    si les corps ne citent aucun outil (cas des doctrines legacy en backticks).
    `mcp_instance` omise = l'instance liée au boot (`bind`)."""
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


async def write_check(body_md: str, mcp_instance=None) -> dict:
    """Validation à l'écriture (ADR 0014) : résout les `<tool:slug>` du corps et
    renvoie le manifeste + les références non résolues. **Non bloquant** —
    l'écriture a lieu, mais l'auteur (IA ou UI) reçoit le signal de drift avant
    que l'agent n'échoue sur l'appel. `mcp_instance` omise = instance liée au boot."""
    manifest = await manifest_for(body_md, mcp_instance=mcp_instance)
    return {
        "referenced_tools": manifest,
        "unresolved_tools": [t["name"] for t in manifest if t.get("status") == "missing"],
    }
