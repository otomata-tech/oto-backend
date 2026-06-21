"""Runs — verbes de cycle de vie d'un déroulé (ADR 0017, barreau 2).

`run_start` ouvre un run (mint un `run_id`, le pousse dans l'état de session) ;
chaque appel d'outil jusqu'à `run_finish` est **attribué à ce run** par le sink
calllog (corrélation côté serveur, l'agent ne thread rien). Un run avec `doctrine`
= l'exécution d'une doctrine nommée (répétable) ; sans `doctrine` = un run one-shot
(ad-hoc), même trace. Le chargement d'une doctrine reste `oto_get_doctrine`
(inchangé). Spine plateforme : chargé explicitement dans `register_all`, hors gate
d'activation.
"""
from __future__ import annotations

from fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import doctrine_run as dr

_OUTCOMES = ("done", "abandoned", "failed", "blocked")


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def run_start(ctx: Context, label: str, doctrine: str | None = None) -> dict:
        """Open a run (a tracked 'déroulé') so a procedure can be reviewed later.
        Returns a `run_id` — keep it and pass it to `run_finish` when you're done.
        Every tool call until then is automatically attributed to this run.

        Use it for a repeatable doctrine/skill (pass `doctrine`) AND for any one-shot
        procedure worth logging (omit `doctrine`).

        Args:
            label: short human description of what this run does (always logged).
            doctrine: optional — the doctrine/skill slug being executed (as passed to
                oto_get_doctrine). Omit for a one-shot/ad-hoc run.
        """
        run_id = dr.new_run_id()
        await dr.push_run(ctx, run_id, label, doctrine)
        return {"run_id": run_id, "label": label, "doctrine": doctrine}

    @mcp.tool()
    async def run_finish(
        ctx: Context, run_id: str, outcome: str, note: str | None = None,
    ) -> dict:
        """Close a run opened with `run_start`.

        Args:
            run_id: the id returned by run_start.
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
