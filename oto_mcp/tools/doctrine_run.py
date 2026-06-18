"""Déroulés de doctrine — verbes de cycle de vie (ADR 0017, barreau 2).

`doctrine_start` ouvre un déroulé (mint un `run_id`, le pousse dans l'état de
session) ; chaque appel d'outil jusqu'à `doctrine_finish` est **attribué à ce run**
par le sink calllog (corrélation côté serveur, l'agent ne thread rien). Le chargement
de la doctrine reste `oto_get_doctrine` (inchangé). Spine plateforme : chargé
explicitement dans `register_all`, hors gate d'activation.
"""
from __future__ import annotations

from fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import doctrine_run as dr

_OUTCOMES = ("done", "abandoned", "failed", "blocked")


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def doctrine_start(ctx: Context, slug: str) -> dict:
        """Open a doctrine run (a tracked 'déroulé'). Call this right after loading a
        doctrine/skill you are about to apply, so its execution can be reviewed later.
        Returns a `run_id` — keep it and pass it to `doctrine_finish` when you're done.
        Every tool call until then is automatically attributed to this run.

        Args:
            slug: the doctrine/skill slug you are starting (as passed to oto_get_doctrine).
        """
        run_id = dr.new_run_id()
        await dr.push_run(ctx, run_id, slug)
        return {"run_id": run_id, "slug": slug}

    @mcp.tool()
    async def doctrine_finish(
        ctx: Context, run_id: str, outcome: str, note: str | None = None,
    ) -> dict:
        """Close a doctrine run opened with `doctrine_start`.

        Args:
            run_id: the id returned by doctrine_start.
            outcome: one of done | abandoned | failed | blocked.
            note: optional — what worked, where it broke, what was missing.
        """
        if outcome not in _OUTCOMES:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"outcome must be one of {', '.join(_OUTCOMES)}",
            ))
        removed = await dr.pop_run(ctx, run_id)
        return {"ok": True, "run_id": run_id, "outcome": outcome, "was_open": removed is not None}
