"""Capacités « signaux d'usage » (ADR 0017, barreau 3) : un seul signal volontaire
`feedback` — retour sur un outil (`signal='tool_feedback'`) OU remontée d'un cas
d'usage non couvert (`signal='gap'`). Même substrat, axe explicite.

Co-déclaré MCP + REST (ADR 0009) → émis par les **agents** (tool `feedback`,
auto-journalisé dans tool_calls + corrélé run_id) ET par des **humains** (dashboard,
POST REST). Le contenu durable atterrit dans `usage_signals` (hors prune). Le `gap`
fait de l'agent un capteur de demande non satisfaite.

Handler SYNC (les adaptateurs n'awaitent pas) : on capte `session_id` (propriété
sync du contexte) ; le `run_id` du face-agent vit déjà dans la row tool_calls jumelle.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from .. import db, org_store
from ._authz import PLATFORM_ADMIN, SUB_ONLY
from ._types import Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class FeedbackInput(BaseModel):
    signal: Literal["tool_feedback", "gap"]
    # tool_feedback: bug | misleading_doc | wrong_result | praise | other
    # gap:           missing_tool | missing_doctrine | missing_data | other
    kind: str
    target: str           # tool_feedback: nom de l'outil ; gap: ce que tu voulais faire
    text: Optional[str] = None


def _correlation() -> tuple[str, Optional[str]]:
    """(source, session_id). Contexte MCP présent → 'agent' + session ; sinon
    (REST humain) → 'human' + None. Best-effort, jamais bloquant."""
    try:
        from fastmcp.server.dependencies import get_context
        ctx = get_context()
        return "agent", ctx.session_id
    except Exception:
        return "human", None


def _active_org(sub: str) -> Optional[int]:
    try:
        return org_store.get_active_org(sub)
    except Exception:
        return None


def _feedback(ctx: ResolvedCtx, inp: FeedbackInput) -> dict:
    source, session_id = _correlation()
    sid = db.insert_usage_signal(
        sub=ctx.sub, org_id=ctx.org_id or _active_org(ctx.sub),
        signal=inp.signal, kind=inp.kind, target=inp.target, body=inp.text,
        session_id=session_id, source=source,
    )
    return {"ok": True, "id": sid}


# --- Projections de lecture (barreau 4) — opérateur plateforme -------------

class RunsInput(BaseModel):
    limit: int = 100


class RunInput(BaseModel):
    run_id: str


class DaysInput(BaseModel):
    days: int = 30


class SignalsInput(BaseModel):
    signal: Optional[str] = None
    target: Optional[str] = None
    status: Optional[str] = None   # 'open' | 'resolved' | None (tous)
    limit: int = 200


class ResolveSignalInput(BaseModel):
    signal_id: int
    note: Optional[str] = None     # ce qui a été fait
    resolved: bool = True          # False = ré-ouvrir


def _runs(ctx: ResolvedCtx, inp: RunsInput) -> dict:
    return {"runs": db.list_runs(inp.limit)}


def _run(ctx: ResolvedCtx, inp: RunInput) -> dict:
    return {"run_id": inp.run_id, "calls": db.get_run(inp.run_id)}


def _gaps(ctx: ResolvedCtx, inp: DaysInput) -> dict:
    return {"gaps": db.aggregate_gaps(inp.days)}


def _tool_quality(ctx: ResolvedCtx, inp: DaysInput) -> dict:
    return {"tools": db.aggregate_tool_feedback(inp.days)}


def _signals(ctx: ResolvedCtx, inp: SignalsInput) -> dict:
    return {"signals": db.list_usage_signals(
        inp.signal, inp.target, inp.limit, status=inp.status)}


def _resolve_signal(ctx: ResolvedCtx, inp: ResolveSignalInput) -> dict:
    row = db.resolve_usage_signal(
        inp.signal_id, resolved_by=ctx.sub, note=inp.note, resolved=inp.resolved)
    if row is None:
        return {"ok": False, "error": "not_found", "id": inp.signal_id}
    return {"ok": True, "signal": row}


CAPABILITIES += [
    Capability(
        key="usage.feedback", handler=_feedback, Input=FeedbackInput, authz=SUB_ONLY,
        description="Report a usage signal about oto. signal='tool_feedback' = feedback on a "
                    "tool you just used (target = the tool name ; kind = bug | misleading_doc | "
                    "wrong_result | praise | other). signal='gap' = a use case oto could NOT do, "
                    "call it whenever you wanted to act but no oto capability covered it "
                    "(target = what you were trying to accomplish ; kind = missing_tool | "
                    "missing_doctrine | missing_data | other). text = optional detail.",
        mcp="feedback", rest=RestBinding("POST", "/api/me/usage/feedback"),
    ),
    # --- projections de lecture (opérateur plateforme) ---------------------
    Capability(key="usage.runs", handler=_runs, Input=RunsInput, authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/usage/runs")),
    Capability(key="usage.run", handler=_run, Input=RunInput, authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/usage/runs/{run_id}")),
    Capability(key="usage.gaps", handler=_gaps, Input=DaysInput, authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/usage/gaps")),
    Capability(key="usage.tool_quality", handler=_tool_quality, Input=DaysInput, authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/usage/tool-quality")),
    Capability(key="usage.signals", handler=_signals, Input=SignalsInput, authz=PLATFORM_ADMIN,
               mcp="oto_admin_list_signals",
               description="List usage signals (feedback/gap) reported about oto, most recent "
                           "first. Filters: signal ('tool_feedback'|'gap'), target, status "
                           "('open'|'resolved'). Platform-admin only.",
               rest=RestBinding("GET", "/api/admin/usage/signals")),
    Capability(key="usage.resolve_signal", handler=_resolve_signal, Input=ResolveSignalInput,
               authz=PLATFORM_ADMIN, mcp="oto_admin_resolve_signal",
               description="Mark a usage signal (feedback/gap) as resolved. signal_id = the "
                           "signal's id (from oto_admin list / usage.signals). note = what was "
                           "done about it. resolved=false re-opens it.",
               rest=RestBinding("POST", "/api/admin/usage/signals/{signal_id}/resolve")),
]
