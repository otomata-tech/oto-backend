"""Capacités « signaux d'usage » (ADR 0017, barreau 3) : feedback volontaire sur un
outil + remontée des cas d'usage non couverts (gap).

Co-déclarées MCP + REST (ADR 0009) → émises par les **agents** (tools `tool_feedback`
/ `report_gap`, auto-journalisés dans tool_calls + corrélés run_id) ET par des
**humains** (dashboard, POST REST). Le contenu durable atterrit dans `usage_signals`
(hors prune). Le `gap` fait de l'agent un capteur de demande non satisfaite.

Handlers SYNC (les adaptateurs n'awaitent pas) : on capte `session_id` (propriété
sync du contexte) ; le `run_id` du face-agent vit déjà dans la row tool_calls jumelle.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from .. import db, org_store
from ._authz import SUB_ONLY
from ._types import Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class FeedbackInput(BaseModel):
    tool: str
    kind: str = "other"   # bug | misleading_doc | wrong_result | praise | other
    text: str


class GapInput(BaseModel):
    intent: str           # ce que tu essayais de faire
    kind: str = "missing_tool"   # missing_tool | missing_doctrine | missing_data | other
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


def _tool_feedback(ctx: ResolvedCtx, inp: FeedbackInput) -> dict:
    source, session_id = _correlation()
    sid = db.insert_usage_signal(
        sub=ctx.sub, org_id=ctx.org_id or _active_org(ctx.sub),
        signal="tool_feedback", kind=inp.kind, target=inp.tool, body=inp.text,
        session_id=session_id, source=source,
    )
    return {"ok": True, "id": sid}


def _gap(ctx: ResolvedCtx, inp: GapInput) -> dict:
    source, session_id = _correlation()
    sid = db.insert_usage_signal(
        sub=ctx.sub, org_id=ctx.org_id or _active_org(ctx.sub),
        signal="gap", kind=inp.kind, target=inp.intent, body=inp.text,
        session_id=session_id, source=source,
    )
    return {"ok": True, "id": sid}


CAPABILITIES += [
    Capability(
        key="usage.tool_feedback", handler=_tool_feedback, Input=FeedbackInput, authz=SUB_ONLY,
        description="Report feedback about a specific oto tool you just used — a bug, a "
                    "misleading docstring, a wrong/empty result, or praise. Helps improve "
                    "the tools. kind: bug | misleading_doc | wrong_result | praise | other.",
        mcp="tool_feedback", rest=RestBinding("POST", "/api/me/usage/tool-feedback"),
    ),
    Capability(
        key="usage.gap", handler=_gap, Input=GapInput, authz=SUB_ONLY,
        description="Report a use case oto could NOT do for you — a missing tool, a missing "
                    "doctrine/skill, or missing data. Call this whenever you wanted to act but "
                    "no oto capability covered it. kind: missing_tool | missing_doctrine | "
                    "missing_data | other ; intent = what you were trying to accomplish.",
        mcp="report_gap", rest=RestBinding("POST", "/api/me/usage/gap"),
    ),
]
