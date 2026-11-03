"""The in-app agent's toolbox, and how one of its calls runs.

**The toolbox is the person's own**: what their agent sees for (sub, org viewed) at the
handshake (`session_visibility.compute_hidden_tools`, the function `me.agent_toolbox`
serves), minus what the in-app agent never calls (`classement.interdit`). The model is
offered a small core set; the rest of the visible toolbox is reached through `oto_call`.

**A call runs in-process, exactly like `oto_call`'s target** (`tools/meta.executer_cible`):
same guards (axes membership, activation, access level), same `tool_calls` row, same
redaction of the served result, same settlement of a re-billed connector key. Run as the
person (`sub_override`), under a FastMCP context bound to the served instance (the
handful of tools that take a `Context` need one), and under a GESTE of its own, which the
revision journal stamps on every row it writes — what `annulation.py` reads to undo.
"""
from __future__ import annotations

import json
import logging
import types
from dataclasses import dataclass
from typing import Optional

from fastmcp.server.context import Context

from ... import geste, session_visibility, tool_registry
from ...auth import hooks as auth_hooks
from ...mcp_errors import McpError
from ...tools import meta
from .._types import AuthzDenied
from . import classement
from .texte import texte_du_resultat

logger = logging.getLogger(__name__)


def instance():
    inst = tool_registry.bound_instance()
    if inst is None:
        raise AuthzDenied(503, "server_not_ready", "The tool registry is not bound yet.")
    return inst


def _schema_sans_axes(schema: Optional[dict]) -> dict:
    """A tool's input schema without the axes the server sets: the model is never
    offered `_org`, `_project`, `_run_id`, `_group`."""
    s = json.loads(json.dumps(schema or {"type": "object"}, default=str))
    props = s.get("properties")
    if isinstance(props, dict):
        for k in classement.AXES_DU_SERVEUR:
            props.pop(k, None)
    if isinstance(s.get("required"), list):
        s["required"] = [r for r in s["required"] if r not in classement.AXES_DU_SERVEUR]
    return s


@dataclass
class Boite:
    visibles: frozenset
    outils: dict           # name → FastMCP tool, visible and not forbidden
    offerts: list          # the core tools' specs for the model (cache breakpoint on last)

    def annotations(self, name: str):
        return getattr(self.outils.get(name), "annotations", None)


async def boite(sub: str, org_id: Optional[int]) -> Boite:
    """The toolbox for (sub, org). Fails closed: if visibility cannot be derived, the
    agent gets no tool rather than every tool."""
    from fastmcp.server.providers.base import Provider
    inst = instance()
    tous = await Provider.list_tools(inst)
    caches = await session_visibility.compute_hidden_tools(
        types.SimpleNamespace(fastmcp=inst), sub, org=org_id)
    outils = {t.name: t for t in tous
              if t.name not in caches and not classement.interdit(t.name)}
    # `oto_call` itself is forbidden as a TARGET, never as the entry point.
    for t in tous:
        if t.name == "oto_call" and t.name not in caches:
            outils["oto_call"] = t
    offerts = []
    for name in classement.CORE_TOOLS:
        t = outils.get(name)
        if t is None:
            continue
        offerts.append({"name": name, "description": (t.description or "")[:4000],
                        "input_schema": _schema_sans_axes(getattr(t, "parameters", None))})
    if offerts:
        offerts[-1]["cache_control"] = {"type": "ephemeral"}
    return Boite(frozenset(outils), outils, offerts)


def a_la_demande(offerts: list[dict]) -> list[dict]:
    """The core tools for a conversation that loads tools on demand (`palier`): the
    `classement.CORE_INLINE` ones first, the cache breakpoint on the last of them, then
    the others with `defer_loading` (the provider refuses a breakpoint on one of those).
    COPIES: `Boite.offerts` stays as it is for the conversations of the first version."""
    garde = [{k: v for k, v in o.items() if k != "cache_control"}
             for o in offerts if o["name"] in classement.CORE_INLINE]
    differes = [{**{k: v for k, v in o.items() if k != "cache_control"},
                 "defer_loading": True}
                for o in offerts if o["name"] not in classement.CORE_INLINE]
    if not garde:
        return [dict(o) for o in offerts]
    garde[-1]["cache_control"] = {"type": "ephemeral"}
    return [*garde, *differes]


@dataclass
class Issue:
    ok: bool
    texte: str
    #: The parsed result when it is JSON (for undo), else None.
    donnees: object = None


def _donnees(texte: str):
    try:
        return json.loads(texte)
    except (ValueError, TypeError):
        return None


#: What a read returns to the model when it did not ask for `fields`. Only tools whose
#: rows are known to carry a bulky key the model rarely needs: `data_list_datastores`
#: returns each table with its whole schema (`data_get_schema` gives it for ONE table).
#: The served notice names what was left out, and `fields=["*"]` asks for the raw result.
CHAMPS_PAR_DEFAUT = {
    "data_list_datastores": ["id", "ns_id", "datastore", "namespace", "name", "owner_type",
                             "shared", "permission", "context_org_id"],
}


def champs_de(name: str, args: dict, target: str):
    """The `fields` to apply: the caller's (`oto_call fields=`), else the default of the
    target, else None (the result is served whole)."""
    voulus = args.get("fields") if name == "oto_call" else None
    if isinstance(voulus, list) and voulus:
        return voulus
    return CHAMPS_PAR_DEFAUT.get(target)


async def executer(b: Boite, sub: str, name: str, args: dict, *, org: Optional[int],
                   projet: Optional[int], run_id: Optional[str],
                   geste_id: Optional[str] = None) -> Issue:
    """Runs `name` (`oto_call` unwrapped to its target) with the server's axes pinned.
    A guard refusal or the target's failure comes back as `Issue(ok=False, …)`: it is
    the model's to read, never an exception of the loop."""
    target, targs = classement.cible(name, args)
    if name == "oto_tool_schema":
        return _schema_d_outil(b, str(args.get("name") or ""))
    tool = b.outils.get(target)
    if tool is None or classement.interdit(target):
        return Issue(False, f"Unknown tool `{target}`: it is not in your toolbox here.")
    pinned = classement.epingler(targs, org, projet, run_id)
    try:
        async with Context(fastmcp=instance()):
            with auth_hooks.sub_override(sub), \
                    geste.portee(geste.AGENT, sub, geste_id or geste.nouvel_identifiant()):
                issue = await meta.executer_cible(tool, sub, target, target, pinned)
    except McpError as e:
        return Issue(False, str(getattr(getattr(e, "error", None), "message", "") or e))
    if not issue.ok:
        return Issue(False, json.dumps({"ok": False, "error": issue.message,
                                        "code": issue.code}, ensure_ascii=False))
    result = issue.result
    champs = champs_de(name, args, target)
    if isinstance(champs, list) and champs and not issue.retenu:
        result = meta._selectionner(result, champs)
    if getattr(result, "is_error", False):
        return Issue(False, texte_du_resultat(result))
    texte = texte_du_resultat(result)
    return Issue(True, texte, _donnees(texte))


def _schema_d_outil(b: Boite, name: str) -> Issue:
    """`oto_tool_schema`, served from the toolbox itself: the schema of a tool the
    person can reach here, without the server's axes; any other name is unknown."""
    tool = b.outils.get(name)
    if tool is None:
        return Issue(False, f"Unknown tool `{name}`: it is not in your toolbox here.")
    return Issue(True, json.dumps({
        "name": name, "description": (tool.description or "").strip(),
        "input_schema": _schema_sans_axes(getattr(tool, "parameters", None)),
    }, ensure_ascii=False, default=str))
