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


def test_org_store_lists_owned_without_sub(monkeypatch):
    # Store agissant-org (endpoint secret opt-in) : sub-less, contexte = org
    # propriétaire seule, aucun groupe, pas de gouvernance.
    rec = {}
    _wire(monkeypatch, rec, org=None)  # current_org ne doit PAS être consulté
    out = D.make_org_store(99).list_namespaces()
    assert rec["owners"] == [("org", "99")]
    assert rec["granted_to"] == (None, [99], [])   # sub=None, org propriétaire, zéro groupe
    assert "groups_for" not in rec                 # pas de scope de groupe (sub-less)
    by_id = {e["id"]: e for e in out}
    assert by_id[1]["can_govern"] is False and by_id[1]["is_personal"] is False


def test_org_store_write_uses_org_principal(monkeypatch):
    # L'écriture d'un store agissant-org se décide sur `org_can_access(org, …)`,
    # jamais sur `can_access(sub, …)` (il n'y a pas de sub).
    seen = {}
    monkeypatch.setattr(D.db, "resolve_datastore_ns",
                        lambda ns, sub, org_ids, group_ids: {"id": 1} if ns == "leads" else None)
    monkeypatch.setattr(D.ownership, "org_can_access",
                        lambda org_id, t, rid, want="read": seen.setdefault("org", (org_id, want)) or True)
    monkeypatch.setattr(D.ownership, "can_access",
                        lambda *a, **k: pytest.fail("can_access(sub) ne doit pas être appelé en mode org"))
    ns_id = D.make_org_store(99)._resolve("leads", write=True)
    assert ns_id == 1 and seen["org"] == (99, "write")


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


# ── Endpoint partagé : scope aux tableaux liés au projet + read-only (#193) ──
def test_org_store_scoped_to_allowed_ns_ids(monkeypatch):
    # Le store agissant-org d'un endpoint partagé est SCOPÉ aux tableaux liés au projet
    # (allowed_ns_ids) : list_namespaces ne renvoie QUE ces ids (anti-fuite — sans ça
    # l'endpoint exposerait tout le datastore de l'org).
    rec = {}
    _wire(monkeypatch, rec, org=None)          # OWNED id=1, GRANTED id=2
    out = D.make_org_store(99, allowed_ns_ids={1}).list_namespaces()
    assert [e["id"] for e in out] == [1]        # id 2 (hors scope) filtré
    assert D.make_org_store(99, allowed_ns_ids=set()).list_namespaces() == []  # scope vide = rien


def test_org_store_resolve_outside_scope_not_found(monkeypatch):
    # Résoudre un namespace HORS du scope projet lève NamespaceNotFound (on ne divulgue
    # pas l'existence d'un namespace hors périmètre).
    monkeypatch.setattr(D.db, "resolve_datastore_ns",
                        lambda ns, *, sub, org_ids, group_ids: {"id": 2})   # existe côté org
    with pytest.raises(D.NamespaceNotFound):
        D.make_org_store(99, allowed_ns_ids={1})._resolve("accords")        # id 2 ∉ {1}
    monkeypatch.setattr(D.db, "resolve_datastore_ns",
                        lambda ns, *, sub, org_ids, group_ids: {"id": 1})
    assert D.make_org_store(99, allowed_ns_ids={1})._resolve("leads") == 1   # dans le scope → OK


def test_org_store_read_only_blocks_write(monkeypatch):
    # read_only=True : l'écriture lève NamespaceReadOnly AVANT le check ownership.
    monkeypatch.setattr(D.db, "resolve_datastore_ns",
                        lambda ns, *, sub, org_ids, group_ids: {"id": 1})
    monkeypatch.setattr(D.ownership, "org_can_access",
                        lambda *a, **k: pytest.fail("pas de check ownership en read_only"))
    store = D.make_org_store(99, allowed_ns_ids={1}, read_only=True)
    with pytest.raises(D.NamespaceReadOnly):
        store._resolve("leads", write=True)
    assert store._resolve("leads") == 1        # lecture OK en read_only
