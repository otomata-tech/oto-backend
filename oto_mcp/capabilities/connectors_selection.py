"""Capacités « sélection de connecteurs » — marketplace (ADR 0019).

Per-membre, scopé à l'org active (`SUB_ONLY` injecte `ctx.org_id`). Trois faits
distincts (cf. `connector_selection`) : exposition (`connector_activation`, plafond),
proposition (`orgs.default_connectors`), sélection (`user_selected_connectors`).

- `connectors.me` (lecture) = catalogue exposé pour l'org active, fusionné avec
  l'état per-membre (`not_selected` | `active` | `paused`) + `recommended` (baseline org).
  Source unique consommée par le dashboard (library + « mes connecteurs »).
- `connectors.select` / `.pause` / `.unselect` (mutation) = installe / met en pause /
  retire un connecteur. Garde : refuser un connecteur non-exposé pour l'org active
  (le plafond d'exposition `connector_activation` n'est jamais relâché).

Handlers SYNC (les adaptateurs n'awaitent pas). NB : la mutation n'a **pas** encore
d'effet de visibilité — le masquage de la pause est branché au middleware en B5
(derrière flag `OTO_CONNECTOR_SELECTION_ENABLED`).
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from .. import access, connector_activation, connector_selection, db, org_store, providers, tool_registry
from ._authz import ORG_ADMIN_OF, SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

# Mapping placeholder de route {id} → champ Input `org_id` (routes réelles en {id}).
_ID = {"id": "org_id"}


class MyConnectorsInput(BaseModel):
    """Filtre/projection de `connectors.me`. Défaut = **compact** (identité + état) :
    la vue pleine (doc_sections/auth/credential_fields/…) gonfle le payload à ~90 KB
    pour 55 connecteurs et dépasse le plafond de tokens MCP (oto-backend#109)."""
    verbose: bool = False                # True = payload complet (dashboard / setup credential)
    state: Optional[str] = None          # filtre : not_selected | active | paused


class ConnectorActionInput(BaseModel):
    name: str                            # nom de connecteur (registre providers.py)


class RecommendInput(BaseModel):
    org_id: int                          # injecté du placeholder {id} (ORG_ADMIN_OF)
    connectors: list[str] = []           # baseline proposée ; [] = aucune recommandation


def _visible_catalog(ctx: ResolvedCtx) -> list[dict]:
    """Catalogue exposé pour l'org active du caller — miroir du filtrage de
    `api_routes.connectors_catalog` : activation (plafond) + RBAC org (ADR 0025).
    L'admin plateforme voit tout l'exposé."""
    exposed = connector_activation.exposed_connectors(ctx.org_id)
    is_admin = access.is_platform_operator(ctx.sub)
    # RBAC connecteur interne à l'org (ADR 0025) : un connecteur restreint dans l'org
    # n'apparaît dans la marketplace du membre que s'il y est autorisé (département/user).
    # Miroir de l'enforcement call-time → la page « voir en tant que » reflète l'effet réel.
    restricted = db.org_restricted_connectors(ctx.org_id) if ctx.org_id else set()
    allowed = (db.member_allowed_connectors(ctx.sub, ctx.org_id)
               if (ctx.org_id and restricted) else set())
    out = []
    for c in providers.public_catalog():
        if c["name"] not in exposed:
            continue
        # RBAC org : restreint + non autorisé + pas admin plateforme → masqué.
        if c["name"] in restricted and not is_admin and c["name"] not in allowed:
            continue
        out.append(c)
    return out


def _doctrine_refs_by_ns(org_id: int | None) -> dict[str, set]:
    """namespace → ensemble des doctrines de l'org qui le référencent (`<tool:slug>`).
    Vide si pas d'org. Dérivation pure depuis les bodies de doctrine (posture
    « doctrine-only », ADR 0024) — best-effort, ne fait jamais échouer la lecture."""
    if not org_id:
        return {}
    try:
        refs: dict[str, set[str]] = {}
        for d in org_store.list_instruction_bodies(org_id):
            slug = d.get("slug") or ""
            for ns in tool_registry.namespaces_in(d.get("body_md") or ""):
                refs.setdefault(ns, set()).add(slug)
        return refs
    except Exception:
        return {}


# Champs conservés en mode COMPACT (défaut MCP) : identité + axes de tri + état.
# Les gros champs (doc_sections/auth/credential_fields/description/namespaces) ne
# reviennent qu'en `verbose=True` (dashboard, setup credential). Cf. #109.
_COMPACT_KEYS = ("name", "label", "help", "family", "category", "availability", "logo_url")


def _me(ctx: ResolvedCtx, inp: MyConnectorsInput) -> dict:
    org_id = ctx.org_id or 0
    selection = connector_selection.list_selection(ctx.sub, org_id)
    recommended = set(org_store.get_org_default_connectors(ctx.org_id) or []) if ctx.org_id else set()
    doc_refs = _doctrine_refs_by_ns(ctx.org_id)
    connectors = []
    for c in _visible_catalog(ctx):
        state = selection.get(c["name"], "not_selected")
        if inp.state and state != inp.state:
            continue
        refset: set = set()
        for ns in c.get("namespaces") or []:
            refset |= doc_refs.get(ns, set())
        base = c if inp.verbose else {k: c.get(k) for k in _COMPACT_KEYS}
        # Couche 3 (option payante) sur la surface USER — sans ça le front ne peut pas
        # dire LAQUELLE des 3 conditions manque (le bandeau « État pour toi », ADR 0044) :
        # `mode==forbidden` conflate option/activation/RBAC. `option_ok=True` si aucune
        # option requise. Verbose seulement (compact = catalogue).
        opt = access.paid_option_for(c["name"])
        connectors.append({
            **base,
            "state": state,
            "recommended": c["name"] in recommended,
            "doctrine_ref_count": len(refset),
            "paid_option": opt,
            "option_ok": opt is None or access.has_option(ctx.sub, opt, org=ctx.org_id),
        })
    return {"connectors": connectors, "verbose": inp.verbose}


def _require_exposed(ctx: ResolvedCtx, name: str) -> None:
    """Plafond : un connecteur non-exposé pour l'org active ne peut être ni
    sélectionné ni mis en pause (deny-by-default jamais relâché)."""
    if name not in connector_activation.exposed_connectors(ctx.org_id):
        raise AuthzDenied(404, "unknown_connector",
                          f"Connecteur `{name}` indisponible pour ton org active.")


# Guidage post-activation (oto-backend#111). Le registre d'outils d'une session MCP est
# FIGÉ à l'ouverture de la conversation : un connecteur activé en cours de session n'y
# monte pas ses outils (le hot-reload `tools/list_changed` n'est pas appliqué par
# claude.ai). Le pont fiable = `oto_call` (dispatch universel, ADR 0036) ; sinon, nouvelle
# conversation. On le DIT à l'agent au moment où il installe, pour qu'il enchaîne sans
# conclure « la capacité n'existe pas ».
def _connector_tools(name: str) -> list[str]:
    """Noms des tools du connecteur, depuis le registre BOOT (immunisé à la
    visibilité de session) — la moitié « découverte » de #186 : le hint ordonnait
    « appelle via oto_call » sans donner UN SEUL nom, et l'introspection de session
    ne voyait pas les tools d'un connecteur fraîchement activé."""
    from .. import providers, tool_registry
    from ..tool_visibility import namespace_of
    con = providers.REGISTRY.get(name)
    ns = set(con.namespaces) if con else {name}
    return [t for t in tool_registry.boot_tool_names() if namespace_of(t) in ns]


def _activation_hint(name: str, tools: list[str]) -> str:
    listing = (f" Ses outils : {', '.join(tools[:12])}." if tools
               else " (noms indisponibles — oto_tool_schema les décrit à la demande).")
    return (f"`{name}` est actif. Ses outils ne sont pas encore montés dans CETTE conversation "
            f"(le registre d'outils est figé à l'ouverture) —{listing} Appelle-les DÈS "
            f"MAINTENANT via `oto_call(name=…, arguments={{…}})` (schéma d'un outil : "
            f"`oto_tool_schema`), ou ouvre une NOUVELLE conversation pour les voir listés.")


def _select(ctx: ResolvedCtx, inp: ConnectorActionInput) -> dict:
    _require_exposed(ctx, inp.name)
    connector_selection.set_state(ctx.sub, inp.name, connector_selection.ACTIVE, ctx.org_id or 0)
    tools = _connector_tools(inp.name)
    return {"connector": inp.name, "state": "active", "tools": tools,
            "hint": _activation_hint(inp.name, tools)}


def _pause(ctx: ResolvedCtx, inp: ConnectorActionInput) -> dict:
    _require_exposed(ctx, inp.name)
    connector_selection.set_state(ctx.sub, inp.name, connector_selection.PAUSED, ctx.org_id or 0)
    return {"connector": inp.name, "state": "paused"}


def _unselect(ctx: ResolvedCtx, inp: ConnectorActionInput) -> dict:
    removed = connector_selection.unselect(ctx.sub, inp.name, ctx.org_id or 0)
    return {"connector": inp.name, "state": "not_selected", "removed": removed}


def _recommend(ctx: ResolvedCtx, inp: RecommendInput) -> dict:
    """« Org propose » : l'org_admin pose la baseline de connecteurs recommandés.
    Consultatif — n'impose rien aux membres (cf. ADR 0019)."""
    ok = org_store.set_org_default_connectors(inp.org_id, inp.connectors)
    if not ok:
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    return {"org_id": inp.org_id, "recommended": inp.connectors}


CAPABILITIES += [
    Capability(
        key="connectors.me", handler=_me, Input=MyConnectorsInput, authz=SUB_ONLY,
        description="List every connector available to you (the marketplace catalog) with your "
                    "per-workspace state: not_selected (in the library) / active / paused, plus "
                    "`recommended` when your org proposes it. Source for both the connector "
                    "library and your installed connectors. Returns a COMPACT row per connector "
                    "by default (name/label/family/category/state/…) — pass verbose=true for the "
                    "full card (doc, auth descriptor, credential fields). Filter with state="
                    "active|paused|not_selected.",
        mcp="oto_my_connectors", rest=RestBinding("GET", "/api/me/connectors"),
    ),
    Capability(
        key="connectors.select", handler=_select, Input=ConnectorActionInput, authz=SUB_ONLY,
        description="Install a connector into your active workspace (state=active). name = "
                    "connector name from the catalog. Its tools do NOT mount in the current "
                    "conversation (the tool registry is frozen at open) — the response `hint` "
                    "tells you to reach them right away via oto_call, or open a new conversation.",
        mcp="oto_select_connector", rest=RestBinding("POST", "/api/me/connectors/{name}/select"),
    ),
    Capability(
        key="connectors.pause", handler=_pause, Input=ConnectorActionInput, authz=SUB_ONLY,
        description="Pause an installed connector (state=paused): kept installed but its tools "
                    "are hidden. Resume by selecting it again.",
        mcp="oto_pause_connector", rest=RestBinding("POST", "/api/me/connectors/{name}/pause"),
    ),
    Capability(
        key="connectors.unselect", handler=_unselect, Input=ConnectorActionInput, authz=SUB_ONLY,
        description="Remove a connector from your workspace (back to the library). Does not touch "
                    "credentials, only your selection.",
        mcp="oto_unselect_connector", rest=RestBinding("DELETE", "/api/me/connectors/{name}"),
    ),
    Capability(
        key="connectors.recommend", handler=_recommend, Input=RecommendInput,
        authz=ORG_ADMIN_OF("org_id"),
        description="[org admin] Set your org's baseline of recommended connectors (the ones "
                    "proposed to members in the library). Advisory — members stay free to "
                    "select/deselect. connectors = list of connector names ([] clears).",
        mcp="oto_set_org_connectors",
        rest=RestBinding("PUT", "/api/orgs/{id}/default-connectors", _ID),
    ),
]
