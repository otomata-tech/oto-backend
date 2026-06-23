"""Tests de la logique de planification d'email différé (pure, sans I/O)."""
from datetime import datetime, timezone

import pytest

from oto_mcp import scheduler

# 2026-06-23 : été (CEST, UTC+2). 12:00 UTC = 14:00 Paris ; 20:00 UTC = 22:00 Paris.
HORS = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)   # 14h Paris (hors 20-8)
DANS = datetime(2026, 6, 23, 20, 0, tzinfo=timezone.utc)   # 22h Paris (dans 20-8)


def test_hors_quiet_envoi_immediat():
    assert scheduler.compute_scheduled_at(HORS, None, None, False) is None


def test_dans_quiet_decale_au_prochain_end():
    # 22h Paris → lendemain 08:00 Paris = 06:00 UTC (CEST).
    when = scheduler.compute_scheduled_at(DANS, None, None, False)
    assert when == datetime(2026, 6, 24, 6, 0, tzinfo=timezone.utc)


def test_force_now_ignore_quiet():
    assert scheduler.compute_scheduled_at(DANS, None, None, True) is None


def test_send_at_naif_interprete_en_tz_org():
    # '09:00' naïf → 09:00 Paris = 07:00 UTC.
    when = scheduler.compute_scheduled_at(HORS, None, "2026-06-25T09:00", False)
    assert when == datetime(2026, 6, 25, 7, 0, tzinfo=timezone.utc)


def test_send_at_tz_aware_respecte():
    when = scheduler.compute_scheduled_at(HORS, None, "2026-06-25T09:00:00+00:00", False)
    assert when == datetime(2026, 6, 25, 9, 0, tzinfo=timezone.utc)


def test_send_at_passe_envoi_immediat():
    assert scheduler.compute_scheduled_at(HORS, None, "2020-01-01T00:00:00+00:00", False) is None


def test_send_at_invalide_leve():
    with pytest.raises(ValueError):
        scheduler.compute_scheduled_at(HORS, None, "pas une date", False)


def test_quiet_non_wrap():
    # Fenêtre 13-15 (sans wrap) : 14h Paris (=HORS) est dedans → décale à 15h Paris = 13h UTC.
    qh = {"tz": "Europe/Paris", "start": 13, "end": 15}
    when = scheduler.compute_scheduled_at(HORS, qh, None, False)
    assert when == datetime(2026, 6, 23, 13, 0, tzinfo=timezone.utc)


def test_quiet_desactive_start_egal_end():
    # org=None côté tool passe {start:0,end:0} → jamais de quiet.
    assert scheduler.compute_scheduled_at(DANS, {"start": 0, "end": 0}, None, False) is None
