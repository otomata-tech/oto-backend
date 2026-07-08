"""Livraison d'un projet complet vers l'org d'un client (#52).

Trois pièces, testées au grain unitaire (style monkeypatch maison) :
- `oto_resource` : partage à une ORG (`org_id`) + `cascade=true` sur share/transfer
  d'un projet — tableaux suivent le geste, procédures grantées read (share) ou
  copiées + lien re-pointé (transfer), connecteurs rapportés `recipient_credential`.
- kind `doctrine` du seam ownership (owner DÉRIVÉ d'org_id).
- `oto_get_doctrine(doctrine_id=…)` : lecture par id honorant les grants (le chemin
  de consommation cross-org du client).
"""
import asyncio

import pytest

from oto_mcp import ownership
from oto_mcp.capabilities import orgs_instructions as oi
from oto_mcp.capabilities import resources as R
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="oto", org_id=1)

LINKS = [
    {"target_type": "tableau", "target_ref": "11", "label": "leads"},
    {"target_type": "tableau", "target_ref": "12", "label": "hors périmètre"},
    {"target_type": "procedure", "target_ref": "77", "label": "process mutuelle"},
    {"target_type": "connecteur", "target_ref": "unipile", "label": None},
    {"target_type": "doc", "target_ref": "36", "label": None},
]


def _wire(monkeypatch, *, governed=("11", "77")):
    """Câble un projet #7 lié aux entités LINKS ; l'acteur gouverne `governed`."""
    calls = {"grants": [], "transfers": [], "revokes": [], "copies": [], "repoints": []}
    monkeypatch.setattr(R.access, "is_platform_operator", lambda sub: False)
    monkeypatch.setattr(R.org_store, "get_org", lambda oid: {"name": "movinmotion"})
    monkeypatch.setattr(R.roles, "is_org_member", lambda sub, oid: True)
    monkeypatch.setattr(R.db, "list_project_links", lambda pid: list(LINKS))
    monkeypatch.setattr(R.db, "log_project_activity", lambda *a, **k: None)
    monkeypatch.setattr(R.ownership, "can_govern",
                        lambda sub, rt, rid: rid in governed)
    # transfert re-gardé (ADR 0048) : le projet #7 est transférable par l'acteur.
    monkeypatch.setattr(R.ownership, "can_transfer", lambda sub, rt, rid: True)
    # ADR 0048 : grant est désormais keyé par RÔLE (viewer/editor/manager) ; on
    # enregistre la permission dérivée pour garder les assertions read/write lisibles.
    def _grant(rt, rid, pt, pid, perm=None, granted_by=None, role=None):
        eff = perm or {"viewer": "read", "editor": "write", "manager": "write"}.get(role, "write")
        calls["grants"].append((rt, rid, pt, pid, eff))
    monkeypatch.setattr(R.ownership, "grant", _grant)
    monkeypatch.setattr(R.ownership, "transfer",
                        lambda rt, rid, ot, oid: calls["transfers"].append((rt, rid, ot, oid)))
    monkeypatch.setattr(R.ownership, "revoke",
                        lambda rt, rid, pt, pid: calls["revokes"].append((rt, rid, pt, pid)) or True)
    monkeypatch.setattr(R.org_store, "copy_instruction_to_org",
                        lambda iid, org, set_by=None:
                        calls["copies"].append((iid, org)) or {"id": 501, "slug": "process-mutuelle", "org_id": org})
    monkeypatch.setattr(R.db, "update_project_link_ref",
                        lambda pid, t, old, new: calls["repoints"].append((pid, t, old, new)) or 1)
    return calls


# ── oto_resource : partage à une org ─────────────────────────────────────────

def test_share_to_org_principal(monkeypatch):
    calls = _wire(monkeypatch)
    out = R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                            resource_id="7", org_id=35))
    assert ("project", "7", "org", "35", "write") in calls["grants"]
    assert out["shared_with"] == "movinmotion" and out["principal_type"] == "org"


def test_share_to_unknown_org_404(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(R.org_store, "get_org", lambda oid: None)
    with pytest.raises(AuthzDenied) as e:
        R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                          resource_id="7", org_id=99))
    assert e.value.code == "unknown_org"


def test_share_to_user_still_works(monkeypatch):
    calls = _wire(monkeypatch)
    monkeypatch.setattr(R.db, "get_user_by_email", lambda e: {"sub": "u2", "email": e})
    monkeypatch.setattr(R.db, "get_user", lambda sub: {"email": "sharer@x.co"})
    monkeypatch.setattr(R.db, "get_project_by_id", lambda pid: {"name": "Campagne mutuelle"})
    sent = {}
    monkeypatch.setattr(R.email, "send_resource_shared_email",
                        lambda to, **kw: sent.update({"to": to, **kw}) or True)
    out = R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                            resource_id="7", email="jb@x.co"))
    assert ("project", "7", "user", "u2", "write") in calls["grants"]
    assert out["principal_type"] == "user"
    # Le bénéficiaire user est notifié par email (best-effort, une seule fois).
    assert out["notified"] is True
    assert sent["to"] == "jb@x.co" and sent["type_label"] == "projet"
    assert sent["name"] == "Campagne mutuelle" and sent["permission"] == "write"


def test_transfer_to_user_emails_new_owner(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(R.db, "get_user_by_email", lambda e: {"sub": "u2", "email": e})
    monkeypatch.setattr(R.db, "get_user", lambda sub: {"email": "sharer@x.co"})
    monkeypatch.setattr(R.db, "get_project_by_id", lambda pid: {"name": "Campagne mutuelle"})
    sent = {}
    monkeypatch.setattr(R.email, "send_resource_transferred_email",
                        lambda to, **kw: sent.update({"to": to, **kw}) or True)
    out = R._resources(CTX, R.ResourceInput(op="transfer", resource_type="project",
                                            resource_id="7", new_owner_email="jb@x.co"))
    assert out["new_owner"] == "jb@x.co" and out["notified"] is True
    assert sent["to"] == "jb@x.co" and sent["name"] == "Campagne mutuelle"


def test_share_to_org_does_not_email(monkeypatch):
    """Partage à une ORG : pas de notif user (qui reçoit reste à trancher, #77)."""
    _wire(monkeypatch)
    monkeypatch.setattr(R.email, "send_resource_shared_email",
                        lambda *a, **k: pytest.fail("ne doit pas notifier une org"))
    out = R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                            resource_id="7", org_id=35))
    assert out["principal_type"] == "org" and "notified" not in out


# ── cascade au PARTAGE (modèle licence : oto garde l'ownership) ──────────────

def test_share_cascade_carries_linked_entities(monkeypatch):
    calls = _wire(monkeypatch)
    out = R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                            resource_id="7", org_id=35,
                                            permission="write", cascade=True))
    # tableau gouverné → même geste, même permission ; doctrine → READ toujours.
    assert ("datastore_namespace", "11", "org", "35", "write") in calls["grants"]
    assert ("doctrine", "77", "org", "35", "read") in calls["grants"]
    by_ref = {(e["target_type"], e["target_ref"]): e for e in out["cascade"]}
    assert by_ref[("tableau", "11")]["status"] == "shared"
    assert by_ref[("tableau", "12")] == {"target_type": "tableau", "target_ref": "12",
                                         "label": "hors périmètre", "status": "skipped",
                                         "reason": "not_governed"}
    assert by_ref[("procedure", "77")]["permission"] == "read"
    assert by_ref[("connecteur", "unipile")]["status"] == "action_required"
    assert by_ref[("doc", "36")]["status"] == "skipped"


def test_share_without_cascade_touches_nothing_linked(monkeypatch):
    calls = _wire(monkeypatch)
    out = R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                            resource_id="7", org_id=35))
    assert "cascade" not in out
    assert all(g[0] == "project" for g in calls["grants"])


# ── cascade au TRANSFERT (remise des clés) ────────────────────────────────────

def test_transfer_cascade_to_org(monkeypatch):
    calls = _wire(monkeypatch)
    out = R._resources(CTX, R.ResourceInput(op="transfer", resource_type="project",
                                            resource_id="7", new_owner_org=35,
                                            cascade=True))
    assert ("project", "7", "org", "35") in calls["transfers"]
    assert ("datastore_namespace", "11", "org", "35") in calls["transfers"]
    # procédure : COPIÉE chez la cible (l'originale reste), lien re-pointé sur la copie.
    assert calls["copies"] == [(77, 35)]
    assert calls["repoints"] == [(7, "procedure", "77", "501")]
    by_ref = {(e["target_type"], e["target_ref"]): e for e in out["cascade"]}
    assert by_ref[("procedure", "77")]["status"] == "copied"
    assert by_ref[("procedure", "77")]["new_ref"] == "501"


def test_transfer_cascade_to_user_skips_doctrine(monkeypatch):
    calls = _wire(monkeypatch)
    monkeypatch.setattr(R.db, "get_user_by_email", lambda e: {"sub": "u2", "email": e})
    out = R._resources(CTX, R.ResourceInput(op="transfer", resource_type="project",
                                            resource_id="7", new_owner_email="jb@x.co",
                                            cascade=True))
    assert calls["copies"] == []
    by_ref = {(e["target_type"], e["target_ref"]): e for e in out["cascade"]}
    assert by_ref[("procedure", "77")]["reason"] == "doctrine_needs_org_owner"


def test_cascade_entity_failure_does_not_break_delivery(monkeypatch):
    calls = _wire(monkeypatch)

    def _boom(rt, rid, pt, pid, perm=None, granted_by=None, role=None):
        if rt == "datastore_namespace":
            raise RuntimeError("pg down")
        eff = perm or {"viewer": "read", "editor": "write", "manager": "write"}.get(role, "write")
        calls["grants"].append((rt, rid, pt, pid, eff))
    monkeypatch.setattr(R.ownership, "grant", _boom)
    out = R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                            resource_id="7", org_id=35, cascade=True))
    by_ref = {(e["target_type"], e["target_ref"]): e for e in out["cascade"]}
    assert by_ref[("tableau", "11")]["status"] == "failed"
    assert by_ref[("procedure", "77")]["status"] == "shared"   # la suite continue


# ── cascade à la RÉVOCATION ───────────────────────────────────────────────────

def test_unshare_cascade_revokes_linked(monkeypatch):
    calls = _wire(monkeypatch)
    out = R._resources(CTX, R.ResourceInput(op="unshare", resource_type="project",
                                            resource_id="7", org_id=35, cascade=True))
    assert ("project", "7", "org", "35") in calls["revokes"]
    assert ("datastore_namespace", "11", "org", "35") in calls["revokes"]
    assert ("doctrine", "77", "org", "35") in calls["revokes"]
    assert {(e["target_type"], e["target_ref"]) for e in out["cascade"]} == \
        {("tableau", "11"), ("procedure", "77")}


# ── kind `doctrine` (ownership, owner dérivé d'org_id) ───────────────────────

def test_doctrine_owner_derives_from_org(monkeypatch):
    monkeypatch.setattr(ownership.org_store, "get_instruction_by_id",
                        lambda i: {"id": 77, "org_id": 42, "slug": "process"})
    assert ownership.owner_of("doctrine", "77") == ("org", "42")


def test_doctrine_owner_none_for_slug_ref():
    assert ownership.owner_of("doctrine", "vieux-slug") is None


def test_doctrine_reparent_rejects_user_owner():
    with pytest.raises(ValueError):
        ownership._doctrine_reparent("77", "user", "u1")


def test_doctrine_listed_in_resource_ops():
    assert "doctrine" in R._OPS


# ── oto_get_doctrine(doctrine_id) : lecture par id + grants ──────────────────

def _wire_doctrine_read(monkeypatch, *, can_access):
    monkeypatch.setattr(oi.org_store, "get_instruction_by_id",
                        lambda i: {"id": 77, "org_id": 42, "slug": "process",
                                   "title": "T", "description": "d", "version": 3,
                                   "body_md": "corps"} if i == 77 else None)
    monkeypatch.setattr(ownership, "can_access",
                        lambda sub, rt, rid, want="read": can_access)

    async def _manifest(*a, **k):
        return []
    monkeypatch.setattr(oi.tool_registry, "manifest_for", _manifest)


def test_get_doctrine_by_id_with_grant(monkeypatch):
    _wire_doctrine_read(monkeypatch, can_access=True)
    out = asyncio.run(oi._get_doctrine(ResolvedCtx(sub="client", org_id=35),
                                       oi.DoctrineGetInput(doctrine_id=77)))
    # l'org_id rendu = l'org PROPRIÉTAIRE de la doctrine, pas l'org active du lecteur
    assert out["org_id"] == 42 and out["doctrine_id"] == 77
    assert out["slug"] == "process" and out["body_md"] == "corps"


def test_get_doctrine_by_id_denied_without_grant(monkeypatch):
    _wire_doctrine_read(monkeypatch, can_access=False)
    with pytest.raises(AuthzDenied) as e:
        asyncio.run(oi._get_doctrine(ResolvedCtx(sub="intrus", org_id=9),
                                     oi.DoctrineGetInput(doctrine_id=77)))
    assert e.value.status == 403


def test_get_doctrine_by_id_unknown_404(monkeypatch):
    _wire_doctrine_read(monkeypatch, can_access=True)
    with pytest.raises(AuthzDenied) as e:
        asyncio.run(oi._get_doctrine(ResolvedCtx(sub="client", org_id=35),
                                     oi.DoctrineGetInput(doctrine_id=999)))
    assert e.value.code == "unknown_doctrine"
