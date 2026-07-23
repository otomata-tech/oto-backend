"""Journal des appels de tools MCP — middleware inliné (ex-lib `otomata-calllog`).

Une ligne par `call_tool` reçu : qui (sub du JWT), quel tool, arguments tronqués,
durée, succès/erreur. L'écriture part en tâche de fond : zéro latence ajoutée, un
échec de journalisation n'échoue jamais le tool.

Historique : lib `otomata-calllog` (extraite d'ogic 2026-06-12), inlinée ici le
2026-07-23 (otomata-calllog#1) — le backend était son dernier consommateur, et le
**contrat canonique** (schéma de ligne `tool_calls`, dashboards comparables entre
serveurs MCP) vit désormais dans le socle `otomata-mcp` (`logging.py`). Le schéma
local étend ce contrat en OTO-LOCAL : `kind`, `session_id`, `run_id`, `org_id`,
`client_id` (cf. `db/_schema.py`).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from fastmcp.server.middleware import Middleware

logger = logging.getLogger("oto_mcp.calllog")

MAX_ARG_CHARS = 300
MAX_ERROR_CHARS = 500

Sink = Callable[[dict], Awaitable[None]]

# Références fortes sur les écritures en tâche de fond (anti-GC asyncio).
_PENDING: set = set()


def truncated_args(arguments: dict | None, max_chars: int = MAX_ARG_CHARS) -> dict | None:
    """Arguments journalisables : scalaires gardés tels quels, le reste
    stringifié et coupé — le journal montre l'intention, pas le payload."""
    if not arguments:
        return None
    out: dict[str, Any] = {}
    for k, v in arguments.items():
        if not (v is None or isinstance(v, (int, float, bool))):
            v = str(v)
            if len(v) > max_chars:
                v = v[:max_chars] + "…"
        out[k] = v
    return out


class ToolCallLogger(Middleware):
    """Middleware FastMCP : journalise chaque on_call_tool via le sink fourni.

    Sous-classe `fastmcp.server.middleware.Middleware` : son `__call__` dispatche
    vers nos hooks `on_*`. Un simple duck-typing (juste `on_call_tool`) ne suffit
    pas — fastmcp ≥3 appelle le middleware comme un callable et lèverait
    « 'ToolCallLogger' object is not callable », cassant le handshake MCP.

    `identity` = callable → {"sub": …, "email": …} (auth Logto custom d'oto :
    le `get_access_token` fastmcp par défaut ne la voit pas).
    """

    def __init__(self, sink: Sink, server: str, identity: Callable[[], dict] | None = None):
        self.sink = sink
        self.server = server
        self.identity = identity or (lambda: {})

    async def on_call_tool(self, context, call_next):
        row: dict[str, Any] = {
            "server": self.server,
            "sub": None,
            "email": None,
            "tool": context.message.name,
            "args": truncated_args(context.message.arguments),
        }
        try:
            row.update({k: v for k, v in self.identity().items() if k in ("sub", "email")})
        except Exception:
            pass
        t0 = time.monotonic()
        try:
            result = await call_next(context)
        except Exception as e:
            self._record({**row, "ok": False, "error": str(e)[:MAX_ERROR_CHARS]}, t0)
            raise
        self._record({**row, "ok": True, "error": None}, t0)
        return result

    def _record(self, row: dict, t0: float) -> None:
        row["duration_ms"] = int((time.monotonic() - t0) * 1000)
        task = asyncio.create_task(self._insert(row))
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)

    async def _insert(self, row: dict) -> None:
        try:
            await self.sink(row)
        except Exception:
            logger.exception("journalisation tool_call en échec (%s)", row.get("tool"))
