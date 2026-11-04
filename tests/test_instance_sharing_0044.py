"""ADR 0038 B6 — le pin d'une instance (`_instance=`) : la garde d'accès par palier.

Le prêt nominatif (`share_side`, ADR 0044) est RETIRÉ par la bascule des comptes
(#1499, L3) : le compte d'une autre personne se juge par ses règles
(`tests/access/test_niveaux_*`). Restent ici les paliers org, équipe et propriétaire."""
import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import access, group_store, roles, instance_refs


def test_guard_org_pin_open_to_any_member(monkeypatch):
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    ref = instance_refs.parse_ref(instance_refs.make_org_ref(35, "zoho"))
    # tout membre de l'org → OK, co-pose l'org de l'instance (un share_down
    # résiduel en base est SANS effet — le cran BYO est retiré)
    assert access.guard_instance_access("member", ref) == 35

def _ailleurs(monkeypatch):
    """L'appelant agit dans une autre org, et aucune règle ne lui ouvre le compte
    (#1499 : hors de l'appartenance, seule une règle qui le nomme ouvre un compte)."""
    monkeypatch.setattr(access, "current_org", lambda sub: 99)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(group_store, "is_group_member", lambda sub, gid: False)
    monkeypatch.setattr(roles, "can_admin_group", lambda sub, gid: False)


def test_guard_org_pin_rejects_non_member(monkeypatch):
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: False)
    _ailleurs(monkeypatch)
    ref = instance_refs.parse_ref(instance_refs.make_org_ref(35, "zoho"))
    with pytest.raises(McpError, match="not a member of org"):
        access.guard_instance_access("intrus", ref)


# ── guard : pin d'une instance de GROUPE = lecteurs du groupe (admin inclus) ──
def test_guard_group_pin_reader_and_org_admin(monkeypatch):
    # `can_read_group` escalade pour l'org_admin (roles.py) : c'est le chemin par
    # lequel un admin d'org utilise l'instance d'une équipe de son org.
    monkeypatch.setattr(roles, "can_read_group",
                        lambda sub, gid: sub in ("finance_guy", "clemence_admin"))
    monkeypatch.setattr(group_store, "get_group", lambda gid: {"id": gid, "org_id": 35})
    ref = instance_refs.parse_ref(instance_refs.make_group_ref(2, "pennylane"))
    assert access.guard_instance_access("finance_guy", ref) == 35
    assert access.guard_instance_access("clemence_admin", ref) == 35
    _ailleurs(monkeypatch)
    with pytest.raises(McpError, match="not a member of group"):
        access.guard_instance_access("other_dept", ref)


def test_guard_member_owner_still_works(monkeypatch):
    # le propriétaire garde le chemin owner (pas de lecture share_side nécessaire)
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    ref = instance_refs.parse_ref(instance_refs.make_member_ref(8, "owner", "zoho"))
    assert access.guard_instance_access("owner", ref) == 8
