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


# ── hook d'une carte de CANAL : « Connecte ton compte X » ────────────────────
#
# Le hook vivait sur `unipile` et disait « Connecte un canal » tant qu'AUCUN des six
# n'était lié. Le split du 2026-08-28 lui donne un grain utile : une carte par canal,
# donc une question par canal. Le cas que l'ancien hook ne pouvait PAS voir —
# quelqu'un qui a LinkedIn mais pas WhatsApp — est celui du 3ᵉ test ci-dessous.

def _st(subscribed=True, linkedin=False, whatsapp=False):
    def ch(connected):
        return {"connected": connected, "account_id": None,
                "account_name": None, "connected_at": None}
    return {"subscribed": subscribed, "mode": "platform", "byo": False,
            "channels": {"linkedin": ch(linkedin), "whatsapp": ch(whatsapp)}}


def test_chaque_canal_a_son_hook_le_compte_nen_a_plus():
    """Le compte pose une CLÉ : sa carte n'a aucun bouton pour connecter quoi que
    ce soit, donc rien à y dire d'une étape manquante."""
    for canal in ("linkedin", "whatsapp", "telegram",
                  "instagram", "messenger", "twitter"):
        assert status_hints.has_hook(canal), canal
    assert not status_hints.has_hook("unipile")


def test_canal_non_lie(monkeypatch):
    monkeypatch.setattr(unipile, "status_for", lambda sub, *, org, group: _st())
    hook = unipile._channel_pending_action("whatsapp", "WhatsApp")
    assert hook("u1", 1, None, {"mode": "platform"}) == "Connecte ton compte WhatsApp"


def test_un_canal_lie_ne_fait_pas_taire_les_autres(monkeypatch):
    """LE cas que l'ancien hook global ratait : il se taisait dès le PREMIER canal
    connecté, donc quelqu'un qui avait LinkedIn ne s'entendait jamais dire qu'il lui
    restait WhatsApp à brancher."""
    monkeypatch.setattr(unipile, "status_for",
                        lambda sub, *, org, group: _st(linkedin=True))
    entry = {"mode": "platform"}
    assert unipile._channel_pending_action("linkedin", "LinkedIn (Unipile)")(
        "u1", 1, None, entry) is None
    assert unipile._channel_pending_action("whatsapp", "WhatsApp")(
        "u1", 1, None, entry) == "Connecte ton compte WhatsApp"


def test_option_fermee(monkeypatch):
    # option fermée → le verdict « option requise » (front) suffit, pas de doublon
    monkeypatch.setattr(unipile, "status_for",
                        lambda sub, *, org, group: _st(subscribed=False))
    hook = unipile._channel_pending_action("whatsapp", "WhatsApp")
    assert hook("u1", 1, None, {"mode": "platform"}) is None


def test_sans_cle_court_circuit(monkeypatch):
    # pas de clé → pas d'appel status_for (les verdicts existants couvrent)
    def boom(sub, *, org, group):
        raise AssertionError("ne doit pas être appelé")
    monkeypatch.setattr(unipile, "status_for", boom)
    hook = unipile._channel_pending_action("whatsapp", "WhatsApp")
    assert hook("u1", 1, None, {"mode": "forbidden"}) is None
