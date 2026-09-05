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
             "share_side": [], "meta": {"rate_limit": 20}}]


@pytest.fixture
def _platform_only(monkeypatch):
    """Aucune clé BYO (user/group/org) ni grant nominatif → seule l'instance
    `open` du coffre peut gagner (ADR 0044 §F)."""
    monkeypatch.setattr(access.rbac, "require_connector_access", lambda p, s=None: None)
    monkeypatch.setattr(db, "get_member_api_key", lambda sub, org, p: None)
    monkeypatch.setattr(access.scope, "current_group", lambda sub: None)
    monkeypatch.setattr(access.scope, "current_org", lambda sub: None)
    monkeypatch.setattr(credentials_store, "list_platform_instances",
                        lambda p: _INSTANCE)
    monkeypatch.setattr(credentials_store, "get_credential",
                        lambda et, eid, p, account="": "SECRET")
    monkeypatch.setattr(grants_chain.db_grants, "edges_for", lambda ref, grantees: [])
    yield


# ── platform_quota_hint : la sonde en lecture seule ──────────────────────────

def test_hint_reflects_usage_under_quota(_platform_only, monkeypatch):
    monkeypatch.setattr(db, "get_usage_today", lambda sub, p: 4)
    assert access.platform_quota_hint("apollo", sub="u") == {
        "used": 4, "limit": 20, "remaining": 16,
    }


def test_hint_remaining_floors_at_zero_over_quota(_platform_only, monkeypatch):
    """Un débit concurrent a pu pousser `used` au-delà de `limit` — `remaining`
    ne doit jamais devenir négatif (ce serait pire à lire que 0)."""
    monkeypatch.setattr(db, "get_usage_today", lambda sub, p: 25)
    assert access.platform_quota_hint("apollo", sub="u") == {
        "used": 25, "limit": 20, "remaining": 0,
    }


def test_hint_is_none_without_a_platform_grant(monkeypatch):
    """Aucune instance plateforme configurée : la question ne se pose pas — on
    ne rend PAS un faux 0/0 qui se lirait comme un quota épuisé."""
    monkeypatch.setattr(access.rbac, "require_connector_access", lambda p, s=None: None)
    monkeypatch.setattr(db, "get_member_api_key", lambda sub, org, p: None)
    monkeypatch.setattr(access.scope, "current_group", lambda sub: None)
    monkeypatch.setattr(access.scope, "current_org", lambda sub: None)
    monkeypatch.setattr(credentials_store, "list_platform_instances", lambda p: [])
    monkeypatch.setattr(grants_chain.db_grants, "edges_for", lambda ref, grantees: [])
    assert access.platform_quota_hint("apollo", sub="u") is None


def test_hint_is_none_when_org_is_unmetered(_platform_only, monkeypatch):
    """Org sur un plan `unmetered` (ADR 0043) : plus de plafond — la sonde ne
    prétend pas en avoir un."""
    monkeypatch.setattr(access.scope, "current_org", lambda sub: 7)
    # `active_org` non-None réveille le barreau MEMBRE de la sonde de présence
    # (walk_cascade) — sondes DB à blanc, pour ne pas taper une base absente ici.
    monkeypatch.setattr(db, "has_member_api_key", lambda s, o, p: False)
    monkeypatch.setattr(org_store, "has_org_secret", lambda o, p: False)
    monkeypatch.setattr(access.quotas, "_org_unmetered", lambda org: True)
    monkeypatch.setattr(db, "get_usage_today", lambda sub, p: 4)
    assert access.platform_quota_hint("apollo", sub="u") is None


# ── Le refus : même contrat historique, plus ce qui manquait aux signaux ─────

def test_exceeded_message_keeps_the_pinned_contract_and_adds_what_was_missing(
        _platform_only, monkeypatch):
    """`test_grants_l5_platform_chain.py` fige déjà "(7/7)" et "la clé `env`" au
    caractère près pour fullenrich — cette même forme doit survivre ici pour
    apollo, avec en plus 0 restant / un délai / un repli explicite."""
    monkeypatch.setattr(session_org, "current_call_instance", lambda: None)
    monkeypatch.setattr(access.scope, "project_pinned_instance", lambda p, *a: None)
    monkeypatch.setattr(db, "get_usage_today", lambda sub, p: 20)  # = rate_limit
    with pytest.raises(McpError) as e:
        access.resolve._resolve_credential_impl("apollo", "auto", "u")
    msg = str(e.value)
    assert "Quota plateforme apollo dépassé aujourd'hui (20/20)" in msg
    assert "la clé `env`" in msg
    assert "0 restant" in msg
    assert "minuit" in msg
    assert "propre clé" in msg
