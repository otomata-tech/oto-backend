"""Seam générique `pending_action` (lot 2) — registre + hook unipile.

Un connecteur à connexion en deux temps enregistre un hook `status_hints` qui
répond « quelle étape manque ? » ; /api/me l'expose tel quel (`pending_action`),
le front l'affiche comme verdict + CTA sans rien connaître du connecteur.
Fail-open : un hook cassé ne casse jamais /api/me.
"""
import pytest

from oto_mcp import status_hints
from oto_mcp.tools import unipile


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch):
    # Copie du registre réel (le hook unipile enregistré à l'import reste visible),
    # les register() du test n'y laissent pas de trace.
    monkeypatch.setattr(status_hints, "_HOOKS", dict(status_hints._HOOKS))


def test_no_hook_returns_none():
    assert status_hints.pending_action("serper", "u1", 1, None, {}) is None
    assert not status_hints.has_hook("serper")


def test_hook_value_passes_through():
    status_hints.register("fake", lambda sub, org, group, entry: "Fais un truc")
    assert status_hints.pending_action("fake", "u1", 1, None, {}) == "Fais un truc"


def test_broken_hook_fails_open():
    def boom(sub, org, group, entry):
        raise RuntimeError("db down")
    status_hints.register("fake", boom)
    assert status_hints.pending_action("fake", "u1", 1, None, {}) is None


# ── hook unipile : « Connecte un canal » ─────────────────────────────────────

def _st(subscribed=True, connected=False):
    ch = {"connected": connected, "account_id": None,
          "account_name": None, "connected_at": None}
    return {"subscribed": subscribed, "mode": "platform", "byo": False,
            "channels": {"linkedin": dict(ch), "whatsapp": dict(ch)}}


def test_unipile_hook_registered():
    assert status_hints.has_hook("unipile")


def test_unipile_no_channel_linked(monkeypatch):
    monkeypatch.setattr(unipile, "status_for", lambda sub, *, org, group: _st())
    assert unipile._status_pending_action(
        "u1", 1, None, {"mode": "platform"}) == "Connecte un canal"


def test_unipile_channel_linked(monkeypatch):
    monkeypatch.setattr(unipile, "status_for",
                        lambda sub, *, org, group: _st(connected=True))
    assert unipile._status_pending_action("u1", 1, None, {"mode": "platform"}) is None


def test_unipile_option_closed(monkeypatch):
    # option fermée → le verdict « option requise » (front) suffit, pas de doublon
    monkeypatch.setattr(unipile, "status_for",
                        lambda sub, *, org, group: _st(subscribed=False))
    assert unipile._status_pending_action("u1", 1, None, {"mode": "platform"}) is None


def test_unipile_forbidden_short_circuits(monkeypatch):
    # pas de clé → pas d'appel status_for (les verdicts existants couvrent)
    def boom(sub, *, org, group):
        raise AssertionError("ne doit pas être appelé")
    monkeypatch.setattr(unipile, "status_for", boom)
    assert unipile._status_pending_action("u1", 1, None, {"mode": "forbidden"}) is None
