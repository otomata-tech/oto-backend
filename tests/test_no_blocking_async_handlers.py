"""Garde-fou perf (oto-backend#68) : aucun handler `@mcp.tool` ne doit être un
`async def` SANS `await` dans son propre scope.

Pourquoi : le serveur est mono-event-loop ; FastMCP route un handler **sync** (`def`)
en threadpool mais exécute un `async def` **dans la boucle**. Un `async def` qui ne
fait que de l'I/O bloquant (clients `requests` sync) gèle alors TOUT le serveur le
temps de l'appel (cf. mémoire `project_backend_perf_funnel`, incident discovery à
10 s sous un flot de `serper_scrape`). Ce test fige le gain du lot connecteurs :
un futur handler bloquant déguisé en async casse le CI au lieu de re-geler la prod.

Auto-maintenu (pas de whitelist) : un tool async est ACCEPTÉ ssi son source contient
réellement un `await`/`async with`/`async for` dans son propre scope (= il rend la
main à la boucle). Un async sans await = bloquant = rejeté.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest
from fastmcp import FastMCP


def _awaits_in_own_scope(fn) -> bool:
    """True si `fn` await/async-with/async-for dans SON scope (pas dans une fonction
    imbriquée). Si le source est illisible (ex. ProxyTool de fédération), on ne flag
    pas (return True) — conservateur."""
    try:
        src = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError):
        return True
    try:
        node = ast.parse(src).body[0]
    except (SyntaxError, IndexError):
        return True

    flag: list[int] = []

    class V(ast.NodeVisitor):
        def visit_Await(self, n):  # noqa: N802
            flag.append(1)

        def visit_AsyncWith(self, n):  # noqa: N802
            flag.append(1)

        def visit_AsyncFor(self, n):  # noqa: N802
            flag.append(1)

        def visit_FunctionDef(self, n):  # noqa: N802 — ne pas descendre
            pass

        def visit_AsyncFunctionDef(self, n):  # noqa: N802
            if n is node:                       # racine : visiter son corps
                for c in n.body:
                    self.visit(c)
            # imbriquée : ignorer

    V().visit(node)
    return bool(flag)


def test_no_async_tool_without_await():
    import asyncio

    from oto_mcp.tools import register_all

    m = FastMCP("guard")
    try:
        register_all(m)
    except Exception as e:  # deps optionnelles absentes en CI minimal
        pytest.skip(f"register_all indisponible: {e}")

    tools = asyncio.run(m.list_tools())
    offenders = []
    for t in tools:
        fn = getattr(t, "fn", None)
        if fn is not None and inspect.iscoroutinefunction(fn) and not _awaits_in_own_scope(fn):
            offenders.append(t.name)

    assert not offenders, (
        "handlers `async def` SANS await (bloquent la boucle mono-worker — "
        "les passer en `def` sync, cf. #68) : " + ", ".join(sorted(offenders))
    )
