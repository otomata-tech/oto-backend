"""La TOOLBOX du membre — ce que le dashboard montre et pilote des outils exposés
à l'appelant.

Six routes écrites à la main jusqu'au 2026-08-27, portées en capacités (ADR 0009) —
mêmes chemins, mêmes codes, même corps sur le fil :

- `GET    /api/me/tools`               → tous les tools + leur état (activé/désactivé)
- `GET    /api/me/tools/registry`      → registre résolu (ADR 0014), matière des `<tool:slug>`
- `POST   /api/me/tools/{name}`        → DÉSACTIVE (visibilité-only, ADR 0031)
- `DELETE /api/me/tools/{name}`        → RÉACTIVE
- `GET    /api/me/tools/{name}/detail` → fiche : description + schémas + connecteur
- `POST   /api/me/tools/{name}/call`   → exécute un outil TESTABLE sous l'identité de l'appelant

⚠️ **POST désactive, DELETE réactive.** L'inversion est contre-intuitive et elle est
HISTORIQUE : le chemin nomme la ligne de denylist, pas le tool — poser la ligne
(POST) masque, la retirer (DELETE) démasque. Le dashboard s'y branche depuis toujours ;
la migration ne l'a pas « corrigée », elle l'a figée par test.

⚠️ **Les six migrent EN BLOC, et c'est une contrainte de routage, pas de confort.**
`{name}` capture un segment, donc `…/tools/registry` DOIT précéder `…/tools/{name}`.
Les routes de capacité sont montées à la FIN de `make_routes` : migrer `registry` sans
`{name}` (ou l'inverse) placerait le générique avant le spécifique et `registry`
serait servi comme un nom d'outil. L'ordre de déclaration ci-dessous EST cet ordre.

**Pas de face MCP** (`mcp=None`). Non par nature : `oto_list_my_tools`, `oto_enable_tool`
et `oto_disable_tool` sont bien le miroir écrit à la main de ces chemins, et
`tests/test_platform_tools_are_capabilities.py` le dit en toutes lettres. Mais leurs
FORMES diffèrent (l'outil MCP prend un `query` et rend une projection de recherche ;
le REST rend la liste complète pour peindre une grille de gouvernance) : les unifier
casserait une des deux surfaces, ce qui sort du « mêmes réponses au caractère près »
de ce chantier. La réconciliation est une décision de contrat — issue #429.

`mcp_instance` était passé de `make_routes` jusqu'au handler ; il est désormais résolu
à l'APPEL via `tool_registry.bound_instance()` (le singleton lié au boot par
`server._build_mcp`). C'est le MÊME objet : `server.py` appelle `_build_mcp` — donc
`tool_registry.bind` — puis `api_routes.make_routes(verifier, mcp_instance=mcp)` avec
cette instance-là. Résoudre à l'appel plutôt qu'au montage est strictement plus juste
(une re-liaison ultérieure serait suivie), et c'est déjà ce que fait `agent_context`.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import access, auth_hooks, connectors, db, tool_registry
from ..tool_visibility import (
    PROTECTED_TOOLS, is_default_hidden, is_testable, namespace_of)
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_PAR_NOM = "/api/me/tools/{name}"


# --- Entrées ----------------------------------------------------------------

class ToolsListInput(BaseModel):
    """Aucun paramètre : la toolbox lue est celle du porteur du jeton."""


class ToolsRegistryInput(BaseModel):
    """Aucun paramètre — le registre est celui du serveur, pas d'une session."""


class ToolNameInput(BaseModel):
    name: str          # placeholder {name}, auto-mappé


class ToolCallInput(BaseModel):
    name: str
    # Le corps ENTIER (cf. `body_field`). Il est LIBRE par nature : ce sont les
    # arguments de l'outil visé, dont aucun modèle statique ne connaît les champs.
    # Deux formes acceptées depuis toujours, et les deux le restent : l'objet
    # d'arguments nu, ou son enveloppe `{"arguments": {…}}`.
    arguments: Optional[dict] = None


# --- Sorties ----------------------------------------------------------------

class ToolState(BaseModel):
    """⚠️ `enabled` est un état de VISIBILITÉ, pas une autorisation (ADR 0031) :
    un outil visible peut très bien refuser à l'appel (credential absent, connecteur
    restreint dans l'org, connecteur non activé). `protected` = anti-lockout : ces
    outils-là ne peuvent pas être masqués, la bascule échouerait en 400."""
    name: str
    enabled: bool
    protected: bool


class ToolsListView(BaseModel):
    """Tous les outils du serveur, triés par nom, avec l'état PERSONNEL de l'appelant
    dans son org active. Inclut les désactivés (le middleware les retire de la liste
    MCP ; ils sont réinjectés ici, sinon la grille du dashboard ne pourrait plus les
    réactiver)."""
    tools: list[ToolState]


class RegistryEntry(BaseModel):
    """Entrée du registre résolu (ADR 0014). `description` est un RÉSUMÉ d'une ligne
    (la 1ʳᵉ ligne de la docstring, écrêtée), pas la fiche — pour ça, `…/detail`."""
    name: str
    description: str
    source: str                       # 'native' | 'federated'
    # Nom du connecteur fédéré d'origine — absent pour un outil natif.
    mcp: Optional[str] = None


class ToolsRegistryView(BaseModel):
    """Le registre BOOT, immunisé à la visibilité de session (#75) : il répond « cet
    outil existe-t-il dans le produit ? », jamais « m'est-il visible ici ? ». C'est ce
    qui alimente la résolution des marqueurs `<tool:slug>` d'une doctrine."""
    tools: list[RegistryEntry]
    count: int


class ToolConnector(BaseModel):
    name: str
    label: Optional[str] = None


class ToolDetailView(BaseModel):
    """La fiche d'un outil. `input_schema`/`output_schema` sont les JSON Schema dérivés
    par FastMCP — `output_schema` est souvent `null`, un outil n'est pas tenu d'en
    déclarer un. `testable` gate le bouton « tester » : seuls les connecteurs open-data
    en lecture seule le sont, jamais un outil à effet de bord."""
    name: str
    description: str
    input_schema: Optional[dict] = None
    output_schema: Optional[dict] = None
    namespace: Optional[str] = None
    connector: Optional[ToolConnector] = None
    source: str                       # 'native' | 'federated'
    enabled: bool
    protected: bool
    default_hidden: bool
    testable: bool


class ToolToggled(BaseModel):
    """L'état APRÈS la bascule, pas l'ordre reçu."""
    ok: bool
    name: str
    enabled: bool


class ToolCallResult(BaseModel):
    """⚠️ **`ok: false` est une réponse 200, pas une erreur HTTP.** L'échec d'un outil
    est le RÉSULTAT du test — voir ce qu'il renvoie, y compris son message d'erreur,
    est précisément le but du bouton. Les 4xx sont réservés au fait de ne pas avoir pu
    lancer le test (outil inconnu, non testable, arguments invalides)."""
    ok: bool
    name: str
    # Résultat de l'outil, sérialisé défensivement (un outil peut rendre un objet
    # non-JSON, il devient alors sa représentation texte). Absent si `ok: false`.
    result: Optional[Any] = None
    elapsed_ms: Optional[int] = None
    # Message d'erreur de l'outil (ou « timeout (>45s) »). Absent si `ok: true`.
    error: Optional[str] = None


# --- Handlers ---------------------------------------------------------------

async def _tool_by_name(name: str):
    """Objet Tool FastMCP par nom (ou None). `run_middleware=False` : hors session MCP
    (contexte REST) la chaîne de middleware n'a pas de Context et lèverait."""
    instance = tool_registry.bound_instance()
    if instance is None:
        return None
    for t in await instance.list_tools(run_middleware=False):
        if t.name == name:
            return t
    return None


async def _list(ctx: ResolvedCtx, inp: ToolsListInput) -> dict:
    instance = tool_registry.bound_instance()
    all_names: set[str] = set()
    if instance is not None:
        # run_middleware=False : appelé hors session MCP (contexte REST), la
        # chaîne de middleware n'a pas de Context FastMCP et lèverait → on
        # veut la liste statique complète, le filtrage disabled est fait
        # juste après via `disabled`.
        all_names = {t.name for t in await instance.list_tools(run_middleware=False)}

    disabled = set(db.list_user_disabled_tools(ctx.sub, access.current_org(ctx.sub) or 0))
    # Le middleware retire déjà les disabled de `list_tools` selon le sub
    # courant (celui de la requête REST = même token). On ré-ajoute donc
    # les disabled pour avoir la vue complète.
    all_names |= disabled

    return {
        "tools": [
            {"name": n, "enabled": n not in disabled,
             "protected": n in PROTECTED_TOOLS}
            for n in sorted(all_names)
        ],
    }


async def _registry(ctx: ResolvedCtx, inp: ToolsRegistryInput) -> dict:
    try:
        reg = await tool_registry.build_registry(tool_registry.bound_instance())
    except Exception as e:  # noqa: BLE001 — l'échec de listing EST le message rendu
        raise AuthzDenied(500, f"list_tools_failed:{e}")
    out = sorted(reg.values(), key=lambda e: e["name"])
    return {"tools": out, "count": len(out)}


def _disable(ctx: ResolvedCtx, inp: ToolNameInput) -> dict:
    """Désactive un tool pour l'utilisateur courant (live)."""
    if inp.name in PROTECTED_TOOLS:
        raise AuthzDenied(400, f"protected_tool:{inp.name}")
    org = access.current_org(ctx.sub) or 0
    db.add_user_disabled_tool(ctx.sub, inp.name, org)
    db.remove_user_enabled_tool(ctx.sub, inp.name, org)  # lève un éventuel override positif
    return {"ok": True, "name": inp.name, "enabled": False}


def _enable(ctx: ResolvedCtx, inp: ToolNameInput) -> dict:
    """Réactive un tool pour l'utilisateur courant (live).

    Visibilité-only (ADR 0031) — même modèle que le meta-tool `oto_enable_tool` :
    activer = préférence d'affichage, pas une autorisation (accès réel gardé au
    call-time : credential + require_connector_access ADR 0025 + activation).
    """
    org = access.current_org(ctx.sub) or 0
    db.remove_user_disabled_tool(ctx.sub, inp.name, org)
    # Override positif requis pour rendre visible un masqué-par-défaut.
    if is_default_hidden(inp.name):
        db.add_user_enabled_tool(ctx.sub, inp.name, org)
    return {"ok": True, "name": inp.name, "enabled": True}


async def _detail(ctx: ResolvedCtx, inp: ToolNameInput) -> dict:
    tool = await _tool_by_name(inp.name)
    if tool is None:
        raise AuthzDenied(404, f"unknown_tool:{inp.name}")
    ns = namespace_of(inp.name)
    conn = connectors.connector_for_namespace(ns)
    disabled = set(db.list_user_disabled_tools(ctx.sub, access.current_org(ctx.sub) or 0))
    federated = bool(conn and conn.kind == "mount")
    return {
        "name": inp.name,
        "description": (tool.description or "").strip(),
        "input_schema": getattr(tool, "parameters", None),
        "output_schema": getattr(tool, "output_schema", None),
        "namespace": ns,
        "connector": ({"name": conn.name, "label": conn.label} if conn else None),
        "source": "federated" if federated else "native",
        "enabled": inp.name not in disabled,
        "protected": inp.name in PROTECTED_TOOLS,
        "default_hidden": is_default_hidden(inp.name),
        "testable": is_testable(inp.name),
    }


async def _call(ctx: ResolvedCtx, inp: ToolCallInput) -> dict:
    """Exécute un outil TESTABLE sous l'identité de l'appelant (bouton « tester »
    du dashboard). Bornée aux connecteurs open-data en lecture seule
    (`is_testable`) — jamais un outil à effet de bord. Les gates de call-time
    (credential, RBAC connecteur, activation) s'appliquent normalement : le
    sub-override REST fait résoudre la bonne identité (`resolve_api_key`/
    `current_org`). L'erreur d'un outil est renvoyée EN DONNÉE (`ok:false`) —
    voir ce que renvoie l'outil (y compris son erreur) EST le but du test."""
    if not is_testable(inp.name):
        raise AuthzDenied(403, f"not_testable:{inp.name}")
    tool = await _tool_by_name(inp.name)
    if tool is None:
        raise AuthzDenied(404, f"unknown_tool:{inp.name}")
    fn = getattr(tool, "fn", None)
    if fn is None:
        raise AuthzDenied(400, f"not_callable:{inp.name}")
    # Accepte {"arguments": {...}} ou l'objet d'arguments brut. Corps absent, illisible
    # ou non-objet (une liste) ⇒ `arguments` reste None ⇒ aucun argument, comme avant.
    body = inp.arguments if inp.arguments is not None else {}
    args = body.get("arguments") if isinstance(body, dict) and "arguments" in body else body
    if not isinstance(args, dict):
        args = {}

    async def _invoke():
        if asyncio.iscoroutinefunction(fn):
            return await fn(**args)
        return await run_in_threadpool(lambda: fn(**args))

    started = time.monotonic()
    with auth_hooks.sub_override(ctx.sub):
        try:
            result = await asyncio.wait_for(_invoke(), timeout=45)
        except asyncio.TimeoutError:
            return {"ok": False, "name": inp.name, "error": "timeout (>45s)"}
        except TypeError as e:
            # Mauvais arguments (param inconnu / manquant) : signal actionnable.
            raise AuthzDenied(400, f"bad_arguments:{e}")
        except Exception as e:  # noqa: BLE001 — l'erreur d'outil est le résultat
            return {"ok": False, "name": inp.name, "error": str(e)}
    elapsed_ms = int((time.monotonic() - started) * 1000)
    # Sérialisation défensive : un tool peut renvoyer un objet non-JSON.
    try:
        safe = json.loads(json.dumps(result, default=str, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        safe = str(result)
    return {"ok": True, "name": inp.name, "result": safe, "elapsed_ms": elapsed_ms}


_DOC_LIST = (
    "Tous les outils du serveur avec MON état de visibilité dans l'org active. "
    "`enabled` est une préférence d'affichage, PAS une autorisation : un outil visible "
    "peut refuser à l'appel (credential absent, connecteur restreint ou non activé). "
    "`protected` marque les outils anti-lockout, qu'on ne peut pas masquer."
)
_DOC_REGISTRY = (
    "Le registre résolu des outils du produit : nom, résumé d'une ligne, origine "
    "(native ou fédérée). C'est la matière des marqueurs `<tool:slug>` d'une doctrine "
    "et de l'autocomplétion. Immunisé à la visibilité de session — il dit ce qui "
    "EXISTE, pas ce qui m'est visible."
)
_DOC_DISABLE = (
    "MASQUE un outil pour moi (visibilité seule, ADR 0031). Le chemin nomme la ligne "
    "de denylist : la poser (POST) masque, la retirer (DELETE) démasque. Un outil "
    "protégé est refusé (400 `protected_tool:<nom>`) — le masquer n'aurait aucun effet."
)
_DOC_ENABLE = (
    "DÉMASQUE un outil pour moi. Sur un outil masqué par défaut au niveau plateforme, "
    "pose en plus l'override positif qui lève ce masquage."
)
_DOC_DETAIL = (
    "La fiche complète d'un outil : description, schémas d'entrée et de sortie dérivés "
    "par FastMCP, connecteur d'origine, état personnel et testabilité. Alimente le "
    "panneau « en savoir plus » et, si l'outil est testable, le formulaire de test."
)
_DOC_CALL = (
    "Exécute un outil TESTABLE sous mon identité (bouton « tester »). Borné aux "
    "connecteurs open-data en lecture seule — jamais un outil à effet de bord. Le corps "
    "est l'objet d'arguments, nu ou enveloppé dans `{\"arguments\": {…}}`. ⚠️ L'erreur "
    "de l'outil revient EN DONNÉE (`ok: false` en 200) : la voir est le but du test. "
    "Les 4xx disent qu'on n'a pas pu lancer, pas que l'outil a échoué."
)

CAPABILITIES += [
    Capability(
        key="me.tools.list", handler=_list, Input=ToolsListInput, authz=SUB_ONLY,
        Output=ToolsListView, description=_DOC_LIST,
        mcp=None,   # miroir `oto_list_my_tools` de forme différente — issue #429
        rest=RestBinding("GET", "/api/me/tools"),
    ),
    # ⚠️ `registry` AVANT `{name}` : Starlette prend le premier match, et `{name}`
    # capturerait « registry » comme un nom d'outil. Cet ordre EST le contrat.
    Capability(
        key="me.tools.registry", handler=_registry, Input=ToolsRegistryInput,
        authz=SUB_ONLY, Output=ToolsRegistryView, description=_DOC_REGISTRY,
        mcp=None,
        rest=RestBinding("GET", "/api/me/tools/registry"),
    ),
    Capability(
        key="me.tools.disable", handler=_disable, Input=ToolNameInput, authz=SUB_ONLY,
        Output=ToolToggled, description=_DOC_DISABLE,
        mcp=None,   # miroir `oto_disable_tool` — issue #429
        rest=RestBinding("POST", _PAR_NOM),
    ),
    Capability(
        key="me.tools.enable", handler=_enable, Input=ToolNameInput, authz=SUB_ONLY,
        Output=ToolToggled, description=_DOC_ENABLE,
        mcp=None,   # miroir `oto_enable_tool` — issue #429
        rest=RestBinding("DELETE", _PAR_NOM),
    ),
    Capability(
        key="me.tools.detail", handler=_detail, Input=ToolNameInput, authz=SUB_ONLY,
        Output=ToolDetailView, description=_DOC_DETAIL,
        mcp=None,
        rest=RestBinding("GET", _PAR_NOM + "/detail"),
    ),
    Capability(
        key="me.tools.call", handler=_call, Input=ToolCallInput, authz=SUB_ONLY,
        Output=ToolCallResult, description=_DOC_CALL,
        mcp=None,   # `oto_call` est le dispatch MCP, MCP-only par nature
        rest=RestBinding("POST", _PAR_NOM + "/call", body_field="arguments"),
    ),
]
