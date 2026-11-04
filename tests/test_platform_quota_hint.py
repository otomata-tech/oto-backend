"""`access.platform_quota_hint` — lecture seule du quota plateforme du jour,
SANS consommer ni déchiffrer (oto-backend#710, signaux #311/#312/#313).

Les signaux : le quota plateforme Apollo est atteint sans indication de ce qui
reste ni de délai (échec sec), et aucun moyen de le connaître AVANT d'appeler
pour qu'un worker batch arbitre ses dépenses au lieu de découvrir la limite au
milieu d'un lead. Ce qui est gardé ici :

1. Le refus (`_resolve_credential_impl`) et la sonde en lecture seule
   (`platform_quota_hint`) passent tous les deux par le MÊME calcul
   (`_win_quota`) — un même chiffre calculé à deux endroits finit par diverger
   (ADR 0024).
2. `None` quand la question ne se pose pas : pas de grant plateforme gagnant
   (une clé BYO gagnerait avant, ou aucun grant), ou aucun plafond (illimité,
   org `unmetered`).
3. Le message de refus continue de porter used/limit et la clé (contrat déjà
   figé par `test_grants_l5_platform_chain.py`), et dit maintenant en plus ce
   que les signaux disaient absent : 0 restant, un délai (« minuit »), un repli.

Fixture reprise de `test_free_tier_platform_key.py` (même mécanique free-tier,
provider `apollo`) : aucune clé BYO, aucun grant nominatif, seule l'instance
`open` du coffre gagne.
"""
from __future__ import annotations
from oto_mcp import credentials_store
from oto_mcp import db
from oto_mcp import org_store
from oto_mcp import session_org

import pytest

from oto_mcp import access, grants_chain
from oto_mcp.mcp_errors import McpError

_INSTANCE = [{"label": "env", "share_mode": "open", "share_down": [],
             "meta": {"rate_limit": 20}}]


@pytest.fixture
def _platform_only(monkeypatch):
    """Aucune clé BYO (user/group/org) ni grant nominatif → seule l'instance
    `open` du coffre peut gagner (ADR 0044 §F)."""
    monkeypatch.setattr(db, "get_member_api_key", lambda sub, org, p: None)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(credentials_store, "list_platform_instances",
                        lambda p: _INSTANCE)
    monkeypatch.setattr(credentials_store, "get_credential",
                        lambda et, eid, p, account="": "SECRET")
    monkeypatch.setattr(grants_chain.db_grants, "edges_for", lambda ref, grantees: [])
    # Sans base : aucun droit déclaré posé, le défaut d'instance (quotas non levés).
    monkeypatch.setattr(access.db_entitlements, "valeurs_posees",
                        lambda oid, sub, droit, now=None:
                        access.db_entitlements.Posees(None, None))
    yield


# ── platform_quota_hint : la sonde en lecture seule ──────────────────────────

def test_hint_reflects_usage_under_quota(_platform_only, monkeypatch):
    monkeypatch.setattr(db, "get_usage_today", lambda sub, p: 4)
    assert access.platform_quota_hint("apollo", sub="u") == {
        "kind": "oto_daily_allowance", "used": 4, "limit": 20, "remaining": 16,
    }


def test_hint_remaining_floors_at_zero_over_quota(_platform_only, monkeypatch):
    """Un débit concurrent a pu pousser `used` au-delà de `limit` — `remaining`
    ne doit jamais devenir négatif (ce serait pire à lire que 0)."""
    monkeypatch.setattr(db, "get_usage_today", lambda sub, p: 25)
    assert access.platform_quota_hint("apollo", sub="u") == {
        "kind": "oto_daily_allowance", "used": 25, "limit": 20, "remaining": 0,
    }


def test_hint_is_none_without_a_platform_grant(monkeypatch):
    """Aucune instance plateforme configurée : la question ne se pose pas — on
    ne rend PAS un faux 0/0 qui se lirait comme un quota épuisé."""
    monkeypatch.setattr(db, "get_member_api_key", lambda sub, org, p: None)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(credentials_store, "list_platform_instances", lambda p: [])
    monkeypatch.setattr(grants_chain.db_grants, "edges_for", lambda ref, grantees: [])
    assert access.platform_quota_hint("apollo", sub="u") is None


def test_hint_is_none_when_org_is_unmetered(_platform_only, monkeypatch):
    """Org sur un plan `unmetered` (ADR 0043) : plus de plafond — la sonde ne
    prétend pas en avoir un."""
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    # `active_org` non-None réveille le barreau MEMBRE de la sonde de présence
    # (walk_cascade) — sondes DB à blanc, pour ne pas taper une base absente ici.
    monkeypatch.setattr(db, "has_member_api_key", lambda s, o, p: False)
    monkeypatch.setattr(org_store, "has_org_secret", lambda o, p: False)
    monkeypatch.setattr(access.db_entitlements, "valeurs_posees",
                        lambda oid, sub, droit, now=None: access.db_entitlements.Posees(
                            1 if (oid, droit) == (7, access.PLATFORM_UNMETERED) else None,
                            None))
    monkeypatch.setattr(db, "get_usage_today", lambda sub, p: 4)
    assert access.platform_quota_hint("apollo", sub="u") is None


# ── Le refus : même contrat historique, plus ce qui manquait aux signaux ─────

def test_exceeded_message_keeps_the_pinned_contract_and_adds_what_was_missing(
        _platform_only, monkeypatch):
    """`test_grants_l5_platform_chain.py` fige déjà "(7/7)" et "la clé `env`" au
    caractère près pour fullenrich — cette même forme doit survivre ici pour
    apollo, avec en plus 0 restant / un délai / un repli explicite."""
    monkeypatch.setattr(session_org, "current_call_instance", lambda: None)
    monkeypatch.setattr(access, "project_pinned_instance", lambda p, *a: None)
    monkeypatch.setattr(db, "get_usage_today", lambda sub, p: 20)  # = rate_limit
    with pytest.raises(McpError) as e:
        access.resolve._resolve_credential_impl("apollo", "auto", "u")
    msg = str(e.value)
    assert "Platform quota apollo exceeded today (20/20)" in msg
    assert "for key `env`" in msg
    assert "0 remaining" in msg
    assert "midnight" in msg
    assert "your own key" in msg


# ── Un lot est vérifié pour SA taille (oto#168) ──────────────────────────────

def _resolve_with(monkeypatch, used, units=None):
    monkeypatch.setattr(session_org, "current_call_instance", lambda: None)
    monkeypatch.setattr(access, "project_pinned_instance", lambda p, *a: None)
    monkeypatch.setattr(db, "get_usage_today", lambda sub, p: used)
    kw = {} if units is None else {"units": units}
    return access.resolve.resolve_credential("apollo", "auto", "u", **kw)


def test_a_lot_larger_than_what_remains_is_refused_before_the_call(
        _platform_only, monkeypatch):
    """1 unité restante (19/20) et un lot de 10 : avant, `used >= limit` laissait
    passer puis le débit poussait la clé commune à 29/20."""
    with pytest.raises(McpError) as e:
        _resolve_with(monkeypatch, used=19, units=10)
    msg = str(e.value)
    assert "1 unit(s) left" in msg
    assert "this batch needs 10" in msg
    assert "(19/20)" in msg
    assert "reduce the batch" in msg
    assert "your own key" in msg


def test_a_lot_larger_than_the_WHOLE_quota_is_refused_by_its_name(
        _platform_only, monkeypatch):
    """À zéro utilisé, un lot de 30 sur un quota de 20 ne passera JAMAIS : le refus
    le dit, avec la taille qui passe, et un code — pas « réduis le lot » sans chiffre."""
    with pytest.raises(McpError) as e:
        _resolve_with(monkeypatch, used=0, units=30)
    msg = str(e.value)
    assert "this batch (30) exceeds the TOTAL quota" in msg
    assert "(20/day)" in msg
    assert "even at zero used" in msg
    assert "batches of ≤ 20" in msg
    assert "your own key" in msg
    assert e.value.error.data["code"] == "platform_quota_lot_trop_grand"


def test_a_lot_over_what_remains_carries_its_code(_platform_only, monkeypatch):
    with pytest.raises(McpError) as e:
        _resolve_with(monkeypatch, used=19, units=10)
    assert e.value.error.data == {"code": "platform_quota_lot_depasse_le_reste",
                                  "units": 10, "limit": 20, "used": 19}


def test_a_lot_that_exactly_fits_is_accepted(_platform_only, monkeypatch):
    assert _resolve_with(monkeypatch, used=10, units=10).is_platform is True


def test_a_single_call_is_unchanged_by_the_lot_check(_platform_only, monkeypatch):
    """Défaut `units=1` : 19/20 passe encore, 20/20 est refusé par le message
    historique (« dépassé », « 0 restant »), pas par celui du lot."""
    assert _resolve_with(monkeypatch, used=19).is_platform is True
    with pytest.raises(McpError) as e:
        _resolve_with(monkeypatch, used=20)
    assert "exceeded today (20/20)" in str(e.value)
    assert "this batch" not in str(e.value)
