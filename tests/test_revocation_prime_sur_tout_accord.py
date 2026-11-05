"""La révocation prime sur tout accord, quel que soit le grain — L5 et L7.

Même règle que le chemin non chaîné (`access.platform_grant._platform_denied`) : un
scope de l'appelant (`user:<sub>` ou `org:<org active>`) révoqué sur le connecteur le
refuse, même s'il a une arête vivante sur l'autre scope, et quelle que soit la clé qui
porte la révocation. Sans ça, un accord individuel rouvrait le blocage de crédit de
son org. `user:` > `org:` ne choisit que le quota.

« Révoqué » ≠ « remplacé » : un `grant` archive l'arête précédente mais en pose une
vivante du même scope — ce scope n'est donc pas révoqué.
"""
from __future__ import annotations

import pytest

from oto_mcp import access, credentials_store, grants_chain, group_store
from oto_mcp.access import chain_resolution
from oto_mcp.db import grants as db_grants

pytestmark = pytest.mark.usefixtures("sans_droit_declare")

ANCIENNE = {"label": "env", "share_mode": "open", "share_down": [], "meta": {"rate_limit": 200}}
NEUVE = {**ANCIENNE, "label": "neuve"}


def _edge(label, grantee, revoked=None, quota=None, edge_id=1):
    return {"id": edge_id, "resource_id": grants_chain.instance_ref(label, "serper"),
            "grantor_kind": "platform", "grantor_id": "platform",
            "grantee_kind": grantee[0], "grantee_id": grantee[1],
            "constraints": {"quota": quota} if quota else {}, "parent_id": None,
            "source": "manual", "created_by": None, "created_at": None,
            "revoked_at": revoked}


def _monde(monkeypatch, instances, aretes, equipe=None):
    """`edges_for` filtre par clé ET par grantee, comme le vrai SQL. `equipe` = l'équipe
    ACTIVE de l'appelant, dont il est membre (#1239) ; aucune par défaut."""
    monkeypatch.setattr(access, "current_group", lambda sub: equipe)
    monkeypatch.setattr(group_store, "list_groups_for_user",
                        lambda sub, org: [{"group_id": equipe}] if equipe else [])
    monkeypatch.setattr(credentials_store, "list_platform_instances",
                        lambda p: [dict(i) for i in instances] if p == "serper" else [])
    monkeypatch.setattr(db_grants, "edges_for", lambda ref, grantees: [
        e for e in aretes if e["resource_id"] == ref
        and (e["grantee_kind"], e["grantee_id"]) in set(grantees)])


# ── L5 : `platform_rung`, servi en prod ───────────────────────────────────────

def test_l5_la_revocation_de_l_equipe_prime_sur_l_accord_individuel(monkeypatch):
    """#1239 : le grain `group` (équipe active) suit la même règle que l'org."""
    _monde(monkeypatch, [ANCIENNE], [
        _edge("env", ("user", "x"), quota=500, edge_id=1),
        _edge("env", ("group", "9"), revoked="2026-10-13", edge_id=2)], equipe=9)
    v = grants_chain.platform_rung("x", "serper", 42)
    assert v is not None and v.granted is False and v.grantee == ("group", "9")


def test_l5_un_accord_individuel_ne_rouvre_pas_la_revocation_de_l_org(monkeypatch):
    _monde(monkeypatch, [ANCIENNE], [
        _edge("env", ("user", "x"), quota=500, edge_id=1),
        _edge("env", ("org", "42"), revoked="2026-10-13", edge_id=2)])
    v = grants_chain.platform_rung("x", "serper", 42)
    assert v is not None and v.granted is False
    # Hors de l'org révoquée, l'accord individuel sert, avec son quota.
    v = grants_chain.platform_rung("x", "serper", 77)
    assert v.granted and v.quota == 500


def test_l5_la_revocation_d_une_autre_cle_prime(monkeypatch):
    _monde(monkeypatch, [NEUVE, ANCIENNE], [
        _edge("neuve", ("user", "x"), edge_id=1),
        _edge("env", ("org", "42"), revoked="2026-10-13", edge_id=2)])
    assert grants_chain.platform_rung("x", "serper", 42).granted is False


def test_l5_un_grant_apres_la_revocation_la_leve(monkeypatch):
    """Garde : remplacé n'est pas révoqué. L'arête archivée de l'ancienne clé, et une
    arête vivante du même scope posée ensuite sur la neuve : accordé."""
    _monde(monkeypatch, [NEUVE, ANCIENNE], [
        _edge("neuve", ("org", "42"), quota=50, edge_id=3),
        _edge("env", ("org", "42"), revoked="2026-10-13", edge_id=2),
        _edge("env", ("user", "x"), quota=500, edge_id=1)])
    v = grants_chain.platform_rung("x", "serper", 42)
    assert v.granted and v.label == "neuve"


# ── L7 : `chain_resolution._platform_pick`, éteint en prod ────────────────────

def test_l7_un_accord_individuel_ne_rouvre_pas_la_revocation_de_l_org(monkeypatch):
    _monde(monkeypatch, [ANCIENNE], [
        _edge("env", ("user", "x"), edge_id=1),
        _edge("env", ("org", "42"), revoked="2026-10-13", edge_id=2)])
    pick, _ = chain_resolution._platform_pick("x", "serper", 42)
    assert pick is None


def test_l7_une_cle_ouverte_posee_apres_la_revocation_ne_sert_pas(monkeypatch):
    """La clé neuve porte l'arête « tout le monde » (`seed_everyone_edges`) et aucune
    arête nominative : parcourue en premier, elle accordait par « tous »."""
    _monde(monkeypatch, [NEUVE, ANCIENNE], [
        _edge("neuve", grants_chain.EVERYONE, edge_id=3),
        _edge("env", grants_chain.EVERYONE, edge_id=1),
        _edge("env", ("org", "42"), revoked="2026-10-13", edge_id=2)])
    pick, _ = chain_resolution._platform_pick("x", "serper", 42)
    assert pick is None
    pick, _ = chain_resolution._platform_pick("x", "serper", 77)
    assert pick is not None and pick.via == "tout_le_monde"


def test_l7_un_grant_apres_la_revocation_la_leve(monkeypatch):
    _monde(monkeypatch, [NEUVE, ANCIENNE], [
        _edge("neuve", ("org", "42"), edge_id=3),
        _edge("env", ("org", "42"), revoked="2026-10-13", edge_id=2)])
    pick, _ = chain_resolution._platform_pick("x", "serper", 42)
    assert pick is not None and pick.via == "grant" and pick.entity_id == "neuve"
