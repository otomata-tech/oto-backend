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
from .tool_visibility import namespace_of

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


def bound_instance():
    """L'instance FastMCP liée au boot (ou None hors serveur, ex. tests). Permet à
    la face REST de réutiliser la logique de visibilité MCP (`compute_hidden_tools`,
    qui attend `ctx.fastmcp.list_tools`) sans contexte MCP."""
    return _INSTANCE


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


# Registre boot mis en cache, réchauffé au DÉMARRAGE hors de tout contexte de
# session (`warm_registry`, appelé au lifespan). Le manifeste « referenced_tools »
# doit répondre « cet outil existe-t-il dans le produit ? » (fait BOOT), jamais
# « m'est-il visible dans CETTE session ? » : `list_tools(run_middleware=False)`
# saute le middleware mais applique QUAND MÊME `apply_session_transforms` (fastmcp) ;
# l'appeler depuis un handler de session polluait donc le manifeste (faux
# `status=missing` sur un outil masqué par la session, ex. `bridge_*` post-
# `oto_use_org` — otomata-private#75). Le cache coupe cette contamination.
_REGISTRY: dict[str, dict] | None = None


def boot_tool_names() -> list[str]:
    """Noms de TOUS les tools du registre BOOT (réchauffé hors session au lifespan,
    immunisé à la visibilité, #75) — tri stable ; [] si non réchauffé (tests).
    Sert la découverte post-activation (#186 : donner les NOMS à oto_call)."""
    return sorted(_REGISTRY or {})


async def _build_registry_live(mcp_instance=None) -> dict[str, dict]:
    """Construit la map nom → entrée à la volée. ⚠️ Si appelée DANS un contexte de
    session, la visibilité de session filtre le résultat (cf. `_REGISTRY`)."""
    mcp_instance = mcp_instance or _INSTANCE
    if mcp_instance is None:
        return {}
    tools = await mcp_instance.list_tools(run_middleware=False)
    return {t.name: _entry(t) for t in tools}


async def warm_registry(mcp_instance=None) -> dict[str, dict]:
    """Construit et met en cache le registre boot. À appeler au DÉMARRAGE, hors de
    tout contexte de session (lifespan) → `apply_session_transforms` ne trouve
    aucune règle de visibilité et renvoie le registre complet. Idempotent."""
    global _REGISTRY
    reg = await _build_registry_live(mcp_instance)
    if reg:
        _REGISTRY = reg
    return _REGISTRY or {}


async def build_registry(mcp_instance=None) -> dict[str, dict]:
    """Map nom → entrée pour tous les tools boot. Sert le cache réchauffé au
    démarrage (immunisé à la visibilité de session, #75) ; à défaut (tests, cache
    non réchauffé) construit à la volée."""
    if _REGISTRY is not None:
        return _REGISTRY
    return await _build_registry_live(mcp_instance)


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
