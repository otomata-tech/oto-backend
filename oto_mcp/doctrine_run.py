"""Runs — pile de runs en état de session (ADR 0017, barreau 1-2).

Un **run** = un déroulé borné (`run_start` → `run_finish`) : soit l'exécution d'une
doctrine nommée (champ `doctrine`), soit un run one-shot/ad-hoc (sans `doctrine`).
Le `run_id` actif vit dans l'**état de session FastMCP** (session-scopé, TTL natif),
sous forme de **pile** (runs imbriqués : un run peut en démarrer un autre).

- `run_start` (tools/doctrine_run.py) **pousse** un run.
- Le sink calllog (`server._calllog_sink`) lit le run **actif** (sommet de pile) et
  **stampe** chaque `tool_call` avec — corrélation côté serveur, l'agent ne thread rien.
- `run_finish` **dépile** (par run_id, robuste à l'imbrication).

State-only : aucune table. La trace durable se dérive de `tool_calls` (run_id) et,
plus tard, des facts promus (barreau 4).
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

# Clé d'état de session (préfixée oto pour ne pas collisionner avec d'autres états).
_STACK_KEY = "oto_doctrine_runs"


def new_run_id() -> str:
    return uuid.uuid4().hex


async def _read_stack(ctx: Any) -> list[dict]:
    try:
        stack = await ctx.get_state(_STACK_KEY)
    except Exception:
        return []
    return list(stack) if isinstance(stack, list) else []


async def push_run(ctx: Any, run_id: str, label: str, doctrine: Optional[str] = None) -> None:
    stack = await _read_stack(ctx)
    stack.append({"run_id": run_id, "label": label, "doctrine": doctrine})
    await ctx.set_state(_STACK_KEY, stack)


async def pop_run(ctx: Any, run_id: str) -> Optional[dict]:
    """Retire le run `run_id` (n'importe où dans la pile, robuste à l'imbrication).
    Renvoie l'entrée retirée ou None si absente."""
    stack = await _read_stack(ctx)
    removed = None
    for i in range(len(stack) - 1, -1, -1):
        if stack[i].get("run_id") == run_id:
            removed = stack.pop(i)
            break
    await ctx.set_state(_STACK_KEY, stack)
    return removed


async def active_run_id(ctx: Any) -> Optional[str]:
    """run_id du run actif (sommet de pile) ou None hors de tout run."""
    stack = await _read_stack(ctx)
    return stack[-1]["run_id"] if stack else None
