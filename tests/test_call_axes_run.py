"""Axe-contexte d'appel `run_id=` (#108/#112, ADR 0017) — corrélation d'un appel à
un déroulé, robuste au renouvellement de session.

Contrats : exposition SÉLECTIVE sur les tools de travail (connecteurs + data, pas le
spine méta/boucle), pose/reset de la ContextVar, priorité de l'axe sur la pile de
session dans le sink, et clôture `finish_run` SCOPÉE au propriétaire (sub)."""
import pytest

from oto_mcp import call_axes, session_org
from oto_mcp.db import usage


def _params(name):
    return {a.param for a in call_axes.axes_for(name)}


def test_run_axis_applies_to_work_tools():
    for name in ("folk_search", "gmail_search", "data_write", "serper_web_search",
                 "pennylane_company"):
        assert "run_id" in _params(name), name


def test_run_axis_excludes_spine_meta_loop():
    # corréler la machinerie de la boucle d'usage / l'identité à un run n'a pas de sens
    for name in ("run_start", "run_finish", "feedback", "oto_whoami", "oto_create_org"):
        assert "run_id" not in _params(name), name


def _run_axis():
    return next(a for a in call_axes.AXES if a.param == "run_id")


@pytest.mark.asyncio
async def test_pin_run_poses_and_resets():
    undo = await _run_axis().pin("abc123")
    try:
        assert session_org.current_call_run() == "abc123"
    finally:
        for reset, tok in reversed(undo):
            reset(tok)
    assert session_org.current_call_run() is None


@pytest.mark.asyncio
async def test_pin_run_inert_on_empty():
    assert await _run_axis().pin("") == []
    assert await _run_axis().pin(None) == []
    assert session_org.current_call_run() is None


# ── finish_run scopé au propriétaire (sub) ───────────────────────────────────

class _Conn:
    def __init__(self, captured):
        self.captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.captured["sql"] = sql
        self.captured["params"] = params


def test_finish_run_scopes_by_sub(monkeypatch):
    captured = {}
    monkeypatch.setattr(usage, "_connect", lambda: _Conn(captured))
    usage.finish_run("run-1", "done", "note", sub="owner")
    assert captured["params"] == ("done", "note", "run-1", "owner")
    assert "sub IS NOT DISTINCT FROM" in captured["sql"]


def test_finish_run_stdio_sub_none(monkeypatch):
    captured = {}
    monkeypatch.setattr(usage, "_connect", lambda: _Conn(captured))
    usage.finish_run("run-1", "abandoned")           # sub par défaut None (stdio)
    assert captured["params"] == ("abandoned", None, "run-1", None)
