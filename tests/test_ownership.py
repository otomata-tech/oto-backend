"""Seam d'ownership (ADR 0030) — plans CONTENU (can_access) vs GOUVERNANCE (can_govern).

On stubbe l'owner de la ressource, les grants, l'appartenance org/groupe et l'escalade
roles. Invariant clé : l'escalade plateforme GOUVERNE une ressource perso mais n'en LIT
pas le contenu (privacy by default).
"""
import pytest

from oto_mcp import ownership

RT = "datastore_namespace"
RID = "7"


def _wire(monkeypatch, *, owner, grant=None, org_ids=(), group_ids=(),
          super_admin=False, org_admin_of=(), org_member_of=(), group_admin_of=(),
          group_read_of=()):
    # owner de la ressource
    monkeypatch.setattr(ownership, "owner_of", lambda rt, rid: owner)
    # scope de l'acteur
    monkeypatch.setattr(ownership, "accessor_scope",
                        lambda sub: ownership.AccessorScope(sub, list(org_ids), list(group_ids)))
    # grant unique (ou None) — get_resource_grant matche n'importe quel principal de l'acteur
    def _get_grant(rt, rid, ptype, pid):
        if grant and grant[0] == ptype and grant[1] == pid:
            # grant = (ptype, pid, permission[, role]) — role optionnel (ADR 0048).
            role = grant[3] if len(grant) > 3 else {"read": "viewer"}.get(grant[2], "editor")
            return {"permission": grant[2], "role": role}
        return None
    monkeypatch.setattr(ownership.db, "get_resource_grant", _get_grant)
    # escalade roles
    monkeypatch.setattr(ownership.roles, "is_platform_admin", lambda sub: super_admin)
    monkeypatch.setattr(ownership.roles, "is_org_admin",
                        lambda sub, oid: super_admin or oid in org_admin_of)
    monkeypatch.setattr(ownership.roles, "is_org_member",
                        lambda sub, oid: super_admin or oid in org_member_of or oid in org_admin_of)
    monkeypatch.setattr(ownership.roles, "can_admin_group",
                        lambda sub, gid: super_admin or gid in group_admin_of)
    monkeypatch.setattr(ownership.roles, "can_read_group",
                        lambda sub, gid: super_admin or gid in group_read_of or gid in group_admin_of)


def test_owner_user_has_full_content(monkeypatch):
    _wire(monkeypatch, owner=("user", "alice"))
    assert ownership.can_access("alice", RT, RID, "read")
    assert ownership.can_access("alice", RT, RID, "write")
    assert ownership.can_govern("alice", RT, RID)


def test_stranger_denied(monkeypatch):
    _wire(monkeypatch, owner=("user", "alice"))
    assert not ownership.can_access("bob", RT, RID, "read")
    assert not ownership.can_govern("bob", RT, RID)


def test_grantee_write(monkeypatch):
    _wire(monkeypatch, owner=("user", "alice"), grant=("user", "bob", "write"))
    assert ownership.can_access("bob", RT, RID, "read")
    assert ownership.can_access("bob", RT, RID, "write")
    # un grant de contenu ne donne PAS la gouvernance
    assert not ownership.can_govern("bob", RT, RID)


def test_grantee_read_only_cannot_write(monkeypatch):
    _wire(monkeypatch, owner=("user", "alice"), grant=("user", "bob", "read"))
    assert ownership.can_access("bob", RT, RID, "read")
    assert not ownership.can_access("bob", RT, RID, "write")


def test_super_admin_governs_but_does_not_read_personal(monkeypatch):
    """Invariant privacy : le super_admin GOUVERNE un classeur perso (transfert)
    mais n'en LIT pas le contenu."""
    _wire(monkeypatch, owner=("user", "alice"), super_admin=True)
    assert ownership.can_govern("ops", RT, RID)         # peut transférer
    assert not ownership.can_access("ops", RT, RID, "read")  # mais pas lire


def test_org_owned_member_reads_admin_governs(monkeypatch):
    _wire(monkeypatch, owner=("org", "42"), org_member_of={42})
    assert ownership.can_access("m", RT, RID, "read")
    assert ownership.can_access("m", RT, RID, "write")   # membre = read+write (classeur d'équipe)
    assert not ownership.can_govern("m", RT, RID)        # membre simple ne gouverne pas


def test_org_owned_admin_governs(monkeypatch):
    _wire(monkeypatch, owner=("org", "42"), org_admin_of={42})
    assert ownership.can_govern("adm", RT, RID)


def test_org_owned_outsider_denied(monkeypatch):
    _wire(monkeypatch, owner=("org", "42"))
    assert not ownership.can_access("x", RT, RID, "read")
    assert not ownership.can_govern("x", RT, RID)


def test_group_grant_read_honored(monkeypatch):
    # Partage à une ÉQUIPE : un membre du groupe (scope group_ids) lit via le grant,
    # n'écrit pas (read), ne gouverne pas.
    _wire(monkeypatch, owner=("user", "alice"), grant=("group", "5", "read"), group_ids=(5,))
    assert ownership.can_access("bob", RT, RID, "read")
    assert not ownership.can_access("bob", RT, RID, "write")
    assert not ownership.can_govern("bob", RT, RID)


def test_group_grant_write(monkeypatch):
    _wire(monkeypatch, owner=("user", "alice"), grant=("group", "5", "write"), group_ids=(5,))
    assert ownership.can_access("bob", RT, RID, "write")


def test_group_grant_non_member_denied(monkeypatch):
    # Le grant vise le groupe 5 ; un acteur hors de ce groupe ne matche aucun principal.
    _wire(monkeypatch, owner=("user", "alice"), grant=("group", "5", "write"), group_ids=(6,))
    assert not ownership.can_access("eve", RT, RID, "read")


def test_group_owned_member_reads_admin_governs(monkeypatch):
    # Ressource POSSÉDÉE par un groupe (namespace d'équipe REST) : membre = contenu,
    # chef d'équipe = gouvernance.
    _wire(monkeypatch, owner=("group", "5"), group_read_of={5})
    assert ownership.can_access("m", RT, RID, "write")
    assert not ownership.can_govern("m", RT, RID)
    _wire(monkeypatch, owner=("group", "5"), group_admin_of={5})
    assert ownership.can_govern("chef", RT, RID)


# --- ADR 0048 : gouvernance GRANTABLE (rôle `gérant`) + tripwire ---------------

def test_manager_grant_governs_but_not_transfers(monkeypatch):
    """Le gérant (grant role=manager) GOUVERNE (re-partage/supprime) mais ne TRANSFÈRE
    pas la propriété (ADR 0048 §3) et n'a pas d'escalade structurelle."""
    _wire(monkeypatch, owner=("user", "alice"), grant=("user", "bob", "write", "manager"))
    assert ownership.can_govern("bob", RT, RID)          # gouvernance grantée
    assert not ownership.can_transfer("bob", RT, RID)    # mais pas le transfert
    # un gérant a aussi le contenu (manager ⇒ permission write)
    assert ownership.can_access("bob", RT, RID, "write")


def test_editor_grant_never_governs(monkeypatch):
    """TRIPWIRE gouvernance : un éditeur (write, non-manager) ne gouverne JAMAIS."""
    _wire(monkeypatch, owner=("user", "alice"), grant=("user", "bob", "write", "editor"))
    assert ownership.can_access("bob", RT, RID, "write")
    assert not ownership.can_govern("bob", RT, RID)
    assert not ownership.can_transfer("bob", RT, RID)


def test_govern_tripwire_stranger_never_governs(monkeypatch):
    """TRIPWIRE : ni un inconnu ni un lecteur/éditeur ne gouvernent une ressource
    qu'ils ne possèdent pas et sur laquelle ils n'ont pas de grant `gérant`."""
    for g in (None, ("user", "bob", "read", "viewer"), ("user", "bob", "write", "editor")):
        _wire(monkeypatch, owner=("user", "alice"), grant=g)
        assert not ownership.can_govern("bob", RT, RID)


def test_manager_grant_via_team(monkeypatch):
    """Gérant accordé à une ÉQUIPE : un membre du groupe gouverne via ce grant."""
    _wire(monkeypatch, owner=("org", "42"), grant=("group", "5", "write", "manager"),
          group_ids=(5,))
    assert ownership.can_govern("m", RT, RID)
    assert not ownership.can_transfer("m", RT, RID)   # gérant ≠ org_admin


def test_transfer_reparents_and_keeps_previous_owner_write(monkeypatch):
    calls = {}
    _wire(monkeypatch, owner=("user", "alice"))
    monkeypatch.setattr(ownership, "_kind", lambda rt: ownership.ResourceKind(
        owner_getter=lambda rid: ("user", "alice"),
        reparent=lambda rid, nt, ni: calls.setdefault("reparent", (rid, nt, ni)),
    ))
    monkeypatch.setattr(ownership.db, "revoke_resource_grant",
                        lambda *a: calls.setdefault("revoke", a))
    monkeypatch.setattr(ownership.db, "grant_resource",
                        lambda *a: calls.setdefault("grant", a))
    ownership.transfer(RT, RID, "user", "bob")
    assert calls["reparent"] == (RID, "user", "bob")
    # le nouveau propriétaire perd son éventuel grant
    assert calls["revoke"] == (RT, RID, "user", "bob")
    # l'ancien propriétaire user garde un accès write
    assert calls["grant"] == (RT, RID, "user", "alice", "write")
