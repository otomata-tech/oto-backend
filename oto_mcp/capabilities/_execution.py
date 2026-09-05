"""Exécution commune : préparation et I/O sync au thread, coroutine à la boucle.

`prepare` valide/résout le contexte et rend `(ctx, inp)`. Appeler une fonction
async ne joue pas son corps : le thread ne fait que construire sa coroutine,
attendue ensuite dans la boucle de l'appel. Le contexte anyio est copié, comme
pour les adaptateurs avant cette extraction.
"""
from __future__ import annotations

import inspect

from starlette.concurrency import run_in_threadpool


async def execute(handler, prepare):
    """Une préparation, un handler ; aucune traduction d'erreur de transport."""
    def work():
        ctx, inp = prepare()
        return ctx, handler(ctx, inp)

    ctx, result = await run_in_threadpool(work)
    if inspect.isawaitable(result):
        result = await result
    return ctx, result
