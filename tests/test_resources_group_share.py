"""Partage à une ÉQUIPE (principal groupe) via `oto_resource` — ouverture de la
surface (la DB et le seam ownership étaient déjà group-ready, ADR 0030).

Règle anti-IDOR (D1) : un grant `group_id` cible un groupe d'une org dont
l'ACTEUR est membre (granularité interne, jamais cross-org — contrairement au
principal `org_id` livraison client). Unshare tolérant (grant orphelin d'un
groupe supprimé) ; labels lisibles dans la vue des grants.
"""
import pytest

from oto_mcp import ownership
from oto_mcp.capabilities import resources as R
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="u1", org_id=1)
GROUP = {"id": 5, "org_id": 42, "name": "sales"}


def _wire(monkeypatch, *, member_of=(42,), group=GROUP):
    calls = {"grants": [], "revokes": []}
    monkeypatch.setattr(R.access, "is_platform_operator", lambda sub: False)
    monkeypatch.setattr(R.group_store, "get_group",
                        lambda gid: dict(group) if group and gid == group["id"] else None)
    monkeypatch.setattr(R.roles, "is_org_member", lambda sub, oid: oid in member_of)
    # ADR 0048 : grant keyé par RÔLE ; on enregistre la permission dérivée (assertions read/write).
    def _grant(rt, rid, pt, pid, perm=None, granted_by=None, role=None):
        eff = perm or {"viewer": "read", "editor": "write", "manager": "write"}.get(role, "write")
        calls["grants"].append((rt, rid, pt, pid, eff))
    monkeypatch.setattr(R.ownership, "grant", _grant)
    monkeypatch.setattr(R.ownership, "revoke",
                        lambda rt, rid, pt, pid: calls["revokes"].append((rt, rid, pt, pid)) or True)
    return calls


def test_share_to_group(monkeypatch):
    calls = _wire(monkeypatch)
    out = R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                            resource_id="7", group_id=5, permission="read"))
    assert ("project", "7", "group", "5", "read") in calls["grants"]
    assert out["shared_with"] == "sales" and out["principal_type"] == "group"


def test_share_to_group_requires_org_membership(monkeypatch):
    # Anti-IDOR : le groupe 5 appartient à l'org 42 ; l'acteur n'en est pas membre.
    calls = _wire(monkeypatch, member_of=())
    with pytest.raises(AuthzDenied) as e:
        R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                          resource_id="7", group_id=5))
    assert e.value.code == "group_not_visible" and e.value.status == 403
    assert calls["grants"] == []


def test_share_to_unknown_group_404(monkeypatch):
    _wire(monkeypatch, group=None)
    with pytest.raises(AuthzDenied) as e:
        R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                          resource_id="7", group_id=99))
    assert e.value.code == "unknown_group"


def test_unshare_group(monkeypatch):
    calls = _wire(monkeypatch)
    out = R._resources(CTX, R.ResourceInput(op="unshare", resource_type="project",
                                            resource_id="7", group_id=5))
    assert ("project", "7", "group", "5") in calls["revokes"]
    assert out["removed"] is True


def test_unshare_deleted_group_still_revokes(monkeypatch):
    # Groupe supprimé après le grant : la révocation reste possible (grant orphelin),
    # label de repli, pas de 404.
    calls = _wire(monkeypatch, group=None)
    out = R._resources(CTX, R.ResourceInput(op="unshare", resource_type="project",
                                            resource_id="7", group_id=5))
    assert ("project", "7", "group", "5") in calls["revokes"]
    assert out["unshared_with"] == "groupe #5"


def test_grants_view_labels(monkeypatch):
    # Le front affiche `label` : email pour un user, nom résolu pour org/groupe.
    _wire(monkeypatch)
    monkeypatch.setattr(R.org_store, "get_org", lambda oid: {"name": "movinmotion"})
    monkeypatch.setattr(R.ownership, "list_grants", lambda rt, rid: [
        {"principal_type": "user", "principal_id": "u2", "email": "jb@x.co",
         "permission": "write", "granted_at": "2026-07-01"},
        {"principal_type": "group", "principal_id": "5", "email": None,
         "permission": "read", "granted_at": "2026-07-01"},
        {"principal_type": "org", "principal_id": "35", "email": None,
         "permission": "read", "granted_at": "2026-07-01"},
    ])
    view = R._grants_view("project", "7")
    assert [g["label"] for g in view] == ["jb@x.co", "sales", "movinmotion"]


def test_share_cascade_to_group_carries_linked_entities(monkeypatch):
    # cascade=true avec un principal groupe : tableau = même grant, procédure = read
    # forcé — la cascade est principal-générique (aucun code dédié groupe).
    calls = _wire(monkeypatch)
    monkeypatch.setattr(R.db, "list_project_links", lambda pid: [
        {"target_type": "tableau", "target_ref": "11", "label": "leads"},
        {"target_type": "procedure", "target_ref": "77", "label": "process"},
    ])
    monkeypatch.setattr(R.db, "log_project_activity", lambda *a, **k: None)
    monkeypatch.setattr(R.ownership, "can_govern", lambda sub, rt, rid: True)
    R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                      resource_id="7", group_id=5,
                                      permission="write", cascade=True))
    assert ("datastore_namespace", "11", "group", "5", "write") in calls["grants"]
    assert ("doctrine", "77", "group", "5", "read") in calls["grants"]


def test_list_governed_includes_admined_groups(monkeypatch):
    # Un chef d'équipe voit dans `op=list` les ressources POSSÉDÉES par ses groupes
    # (plan gouvernance) — pas celles des groupes où il est simple membre.
    _wire(monkeypatch)
    monkeypatch.setattr(R.ownership, "accessor_scope",
                        lambda sub: ownership.AccessorScope(sub, [], [5, 6]))
    monkeypatch.setattr(R.roles, "is_org_admin", lambda sub, oid: False)
    monkeypatch.setattr(R.roles, "can_admin_group", lambda sub, gid: gid == 5)
    seen = {}
    monkeypatch.setattr(R.db, "list_projects_for_owners",
                        lambda owners: seen.update(owners=owners) or [])
    R._resources(CTX, R.ResourceInput(op="list", resource_type="project"))
    assert ("group", "5") in seen["owners"] and ("group", "6") not in seen["owners"]
