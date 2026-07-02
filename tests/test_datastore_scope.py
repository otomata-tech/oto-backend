"""Scoping de `DatastorePg.list_namespaces` (ADR 0023/0030).

La liste est une LISTE DE CONTENU : elle scope sur l'org ACTIVE (owner
`ownership.active_owner`) et sur MES groupes DE CETTE ORG — jamais l'union
cross-org (`accessor_scope`), cf. la règle de `ownership.active_owner` et le
tripwire `test_owner_scope_tripwire.py`. Pendant datastore du test projets
`test_list_includes_projects_shared_to_my_team`.

On monkeypatche les seams (access/group_store/db/ownership), pas de DB.
"""
import pytest

import oto_mcp.datastore as D
from oto_mcp import access, group_store


OWNED = {"id": 1, "namespace": "leads", "owner_type": "org", "owner_id": "99",
         "created_at": "2026-07-02", "schema": None}
GRANTED = {"id": 2, "namespace": "accords", "owner_type": "org", "owner_id": "42",
           "created_at": "2026-07-02", "schema": None, "permission": "read"}


def _wire(monkeypatch, rec, *, org=99, groups=({"group_id": 5, "org_id": 99, "name": "sales"},)):
    monkeypatch.setattr(access, "current_org", lambda sub: org)

    def fake_groups(sub, org_id):  # positionnel strict : droppe l'arg org = TypeError
        rec["groups_for"] = (sub, org_id)
        return list(groups)

    monkeypatch.setattr(group_store, "list_groups_for_user", fake_groups)

    def fake_owned(owners):
        rec["owners"] = owners
        return [OWNED]

    def fake_granted(sub, org_ids, group_ids):
        rec["granted_to"] = (sub, org_ids, group_ids)
        return [GRANTED]

    monkeypatch.setattr(D.db, "list_datastore_namespaces_for_owners", fake_owned)
    monkeypatch.setattr(D.db, "list_datastore_namespaces_granted_to", fake_granted)
    monkeypatch.setattr(D.ownership, "can_govern", lambda sub, t, rid: False)


def test_list_namespaces_scopes_groups_on_active_org(monkeypatch):
    # Les grants interrogés = org active + TOUS mes groupes DE L'ORG ACTIVE
    # (pas le seul groupe actif, pas les groupes de mes autres orgs).
    rec = {}
    _wire(monkeypatch, rec)
    out = D.make_store("u1").list_namespaces()

    assert rec["groups_for"] == ("u1", 99)          # le filtre org est bien passé
    assert rec["owners"] == [("org", "99")]         # contenu possédé = org active seule
    assert rec["granted_to"] == ("u1", [99], [5])   # grants org active + mes groupes de cette org

    by_id = {e["id"]: e for e in out}
    assert by_id[1]["shared"] is False and by_id[1]["can_write"] is True
    assert by_id[2]["shared"] is True and by_id[2]["permission"] == "read"
    assert by_id[2]["can_write"] is False


def test_list_namespaces_dedups_owned_over_granted(monkeypatch):
    # Un namespace possédé ET accordé ne sort qu'une fois, en possédé.
    rec = {}
    _wire(monkeypatch, rec)
    monkeypatch.setattr(D.db, "list_datastore_namespaces_granted_to",
                        lambda sub, org_ids, group_ids: [dict(OWNED, permission="read")])
    out = D.make_store("u1").list_namespaces()
    assert [e["id"] for e in out] == [1]
    assert out[0]["shared"] is False


def test_list_namespaces_no_active_org_is_empty(monkeypatch):
    # Filet : sans org active (ne devrait plus arriver post-abolition du perso),
    # la liste est vide — pas de retombée sur un scope plus large.
    rec = {}
    _wire(monkeypatch, rec, org=None)
    assert D.make_store("u1").list_namespaces() == []
    assert "groups_for" not in rec and "granted_to" not in rec


def test_resolve_by_name_scopes_to_active_org(monkeypatch):
    # RÉGRESSION (fuite cross-org, symétrique au fix projets) : la résolution PAR NOM
    # scope sur l'org active — `resolve_datastore_ns` reçoit [org active] + mes groupes
    # DE CETTE ORG, jamais l'union de toutes mes orgs (`accessor_scope`). Un namespace
    # d'une AUTRE de mes orgs (introuvable dans ce scope) lève NamespaceNotFound.
    rec = {}
    monkeypatch.setattr(access, "current_org", lambda sub: 44)
    monkeypatch.setattr(group_store, "list_groups_for_user",
                        lambda sub, org_id: [{"group_id": 7, "org_id": org_id, "name": "x"}])

    def fake_resolve(namespace, *, sub, org_ids, group_ids):
        rec["args"] = (namespace, sub, org_ids, group_ids)
        return None    # possédé par une autre org → hors de [44] → introuvable

    monkeypatch.setattr(D.db, "resolve_datastore_ns", fake_resolve)
    with pytest.raises(D.NamespaceNotFound):
        D.make_store("u1").resolve_ns_id("leads")
    assert rec["args"] == ("leads", "u1", [44], [7])   # org active seule, pas l'union


def test_resolve_finds_active_org_namespace(monkeypatch):
    # Un namespace possédé par l'org active se résout bien (org_ids = [org active]).
    monkeypatch.setattr(access, "current_org", lambda sub: 99)
    monkeypatch.setattr(group_store, "list_groups_for_user", lambda sub, org_id: [])
    monkeypatch.setattr(
        D.db, "resolve_datastore_ns",
        lambda namespace, *, sub, org_ids, group_ids: {"id": 1} if org_ids == [99] else None)
    assert D.make_store("u1").resolve_ns_id("leads") == 1
