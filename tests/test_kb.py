"""Capacité `oto_kb` — base de connaissance d'org, ANCRÉE PAR ID (lot 3, chantier 0.3).

`orgs.kb_project_id` = la source de vérité (plus le nom) : renommer ne casse rien,
deux appels concurrents ne créent plus deux KB (claim optimiste, le perdant archive
son doublon), une ancre pendouillante (transfert/archive) s'auto-répare.
Logique pure — seams org_store/db stubés.
"""
import pytest

from oto_mcp.capabilities import kb as K
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


def _proj(pid, org="7", *, archived=None, owner_type="org"):
    return {"id": pid, "name": K.KB_NAME, "brief_md": K.KB_BRIEF,
            "owner_type": owner_type, "owner_id": org, "archived_at": archived}


@pytest.fixture
def seams(monkeypatch):
    rec = {"created": [], "archived": [], "cleared": [], "anchor": None,
           "claim_ok": True, "projects": {}}

    monkeypatch.setattr(K.org_store, "get_kb_project_id",
                        lambda org: rec["anchor"])
    def _claim(org, pid):
        if rec["claim_ok"] and rec["anchor"] is None:
            rec["anchor"] = pid
            return True
        return False
    monkeypatch.setattr(K.org_store, "claim_kb_project", _claim)
    monkeypatch.setattr(K.org_store, "clear_kb_project",
                        lambda org, pid: rec["cleared"].append(pid) or
                        rec.update(anchor=None) if rec["anchor"] == pid else None)
    def _create(ot, oid, name, brief, created_by=None):
        pid = 42 + len(rec["created"])
        rec["created"].append((ot, oid, name))
        rec["projects"][pid] = _proj(pid, oid)
        return pid
    monkeypatch.setattr(K.db, "create_project", _create)
    monkeypatch.setattr(K.db, "get_project_by_id",
                        lambda pid: rec["projects"].get(pid))
    monkeypatch.setattr(K.db, "archive_project",
                        lambda pid: rec["archived"].append(pid))
    monkeypatch.setattr(K.db, "log_project_activity", lambda *a, **k: None)
    return rec


def test_anchored_kb_returned_without_creation(seams):
    seams["anchor"] = 9
    seams["projects"][9] = _proj(9)
    out = K._kb(ResolvedCtx(sub="u1", org_id=7), K.KbInput(op="get"))
    assert out["project_id"] == 9 and seams["created"] == []


def test_renamed_kb_still_resolves(seams):
    # Le nom n'est plus un marqueur : une KB renommée reste LA KB (l'ancre tient).
    seams["anchor"] = 9
    seams["projects"][9] = {**_proj(9), "name": "Wiki interne"}
    out = K._kb(ResolvedCtx(sub="u1", org_id=7), K.KbInput(op="get"))
    assert out["project_id"] == 9 and out["name"] == "Wiki interne"
    assert seams["created"] == []


def test_no_anchor_creates_and_claims(seams):
    out = K._kb(ResolvedCtx(sub="u1", org_id=7), K.KbInput(op="get"))
    assert seams["created"] == [("org", "7", K.KB_NAME)]
    assert out["project_id"] == 42 and seams["anchor"] == 42


def test_dangling_anchor_transferred_project_repairs(seams):
    # Le projet ancré a été transféré hors org → clear + recréation + re-claim.
    seams["anchor"] = 9
    seams["projects"][9] = _proj(9, org="99")   # owner ≠ org active
    out = K._kb(ResolvedCtx(sub="u1", org_id=7), K.KbInput(op="get"))
    assert seams["cleared"] == [9]
    assert out["project_id"] == 42 and seams["anchor"] == 42


def test_dangling_anchor_archived_project_repairs(seams):
    seams["anchor"] = 9
    seams["projects"][9] = _proj(9, archived="2026-07-01")
    out = K._kb(ResolvedCtx(sub="u1", org_id=7), K.KbInput(op="get"))
    assert out["project_id"] == 42


def test_lost_claim_archives_duplicate_and_returns_winner(seams):
    # Un appel concurrent a posé l'ancre entre ma création et mon claim → mon
    # doublon est archivé, je renvoie LA KB du gagnant.
    winner = 9
    seams["projects"][winner] = _proj(winner)
    real_claim = K.org_store.claim_kb_project
    def _racing_claim(org, pid):
        seams["anchor"] = winner        # le concurrent gagne juste avant moi
        return False
    K.org_store.claim_kb_project = _racing_claim
    try:
        out = K._kb(ResolvedCtx(sub="u1", org_id=7), K.KbInput(op="get"))
    finally:
        K.org_store.claim_kb_project = real_claim
    assert seams["archived"] == [42]          # mon doublon archivé
    assert out["project_id"] == winner


def test_no_active_org(seams):
    with pytest.raises(AuthzDenied) as e:
        K._kb(ResolvedCtx(sub="u1", org_id=None), K.KbInput(op="get"))
    assert e.value.code == "no_active_org"


def test_capability_registered():
    from oto_mcp.capabilities.registry import CAPABILITIES
    cap = next((c for c in CAPABILITIES if c.key == "me.kb"), None)
    assert cap is not None and cap.mcp == "oto_kb"
