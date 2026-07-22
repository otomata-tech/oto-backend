"""Discipline du quota amont unipile : cooldown 429 + mapping STOP (incident 2026-07-21)."""
import time

import pytest
from mcp.shared.exceptions import McpError

from oto_mcp.tools import unipile as U


def test_guard_noop_without_cooldown():
    U._RATE_LIMIT_UNTIL.clear()
    U._rate_limit_guard("s1")  # ne lève pas


def test_note_then_guard_blocks():
    U._RATE_LIMIT_UNTIL.clear()

    class _E:
        retry_after = 120

    U._note_rate_limited("s1", _E())
    assert U._RATE_LIMIT_UNTIL["s1"] > time.time()
    with pytest.raises(McpError):
        U._rate_limit_guard("s1")


def test_note_default_1h_when_no_hint():
    U._RATE_LIMIT_UNTIL.clear()

    class _E:
        retry_after = None

    U._note_rate_limited("s2", _E())
    assert U._RATE_LIMIT_UNTIL["s2"] > time.time() + 3000  # ~1h, pas 12h


def test_scrape_maps_rate_limit_and_arms_cooldown():
    U._RATE_LIMIT_UNTIL.clear()
    from oto.tools.unipile.client import UnipileRateLimited

    def _boom():
        raise UnipileRateLimited("Unipile 429: Retry in 12 hours.", retry_after=43200)

    with pytest.raises(McpError):
        U._scrape("s3", _boom)
    assert "s3" in U._RATE_LIMIT_UNTIL  # cooldown armé pour STOPPER les suivants


def test_scrape_passes_through_result_and_other_errors():
    U._RATE_LIMIT_UNTIL.clear()
    assert U._scrape("s4", lambda: {"ok": 1}) == {"ok": 1}

    def _other():
        raise ValueError("bteh")

    with pytest.raises(ValueError):  # une erreur non-429 remonte telle quelle
        U._scrape("s4", _other)
