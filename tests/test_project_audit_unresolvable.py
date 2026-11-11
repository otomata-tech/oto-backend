"""Audit `unresolvable_connectors` (#218/#219) — un projet ORG-owned qui lie un
connecteur dont le credential n'existe qu'au niveau d'une ÉQUIPE de l'org ne le résout
pas en contexte projet. Le signal pointe le remède (transfert à l'équipe / instance_ref).

Logique pure (helpers hors DB stubés) — le chemin SQL est couvert au déploiement.
"""
from oto_mcp import project_audit
from oto_mcp import access, group_store, db, providers


def _stub_group_secret(monkeypatch, *, org_secret: bool, group_has: bool):
    monkeypatch.setattr(access, "connector_resolvable_for_org",
                        lambda name, org_id: org_secret)
    monkeypatch.setattr(group_store, "list_groups", lambda org_id: [{"id": 7}])
    # Les équipes qui détiennent la clé se lisent en lot (`groups_holding`).
    monkeypatch.setattr(group_store, "groups_holding",
                        lambda gids, org_id, name: set(gids) if group_has else set())


def test_why_flags_group_only_credential(monkeypatch):
    # credential absent de l'org, présent sur une équipe, aucun instance_ref → flag.
    _stub_group_secret(monkeypatch, org_secret=False, group_has=True)
    why = project_audit._unresolvable_connector_why("zoho", 35, {"config": {}})
    assert why is not None
    assert "transfère le projet à l'équipe" in why.lower() or "transfère" in why.lower()


def test_why_silent_when_org_resolves(monkeypatch):
    # l'org a le credential (secret d'org / plateforme) → rien à signaler.
    _stub_group_secret(monkeypatch, org_secret=True, group_has=True)
    assert project_audit._unresolvable_connector_why("zoho", 35, {"config": {}}) is None


def test_why_silent_for_pure_member_byo(monkeypatch):
    # ni org ni équipe n'ont de secret (connecteur BYO par-membre) → PAS de faux positif.
    _stub_group_secret(monkeypatch, org_secret=False, group_has=False)
    assert project_audit._unresolvable_connector_why("attio", 35, {"config": {}}) is None


def test_why_silent_when_instance_pinned(monkeypatch):
    # instance_ref épinglé = intention déclarée (RBAC gère) → le signal est levé.
    _stub_group_secret(monkeypatch, org_secret=False, group_has=True)
    link = {"config": {"instance_ref": "zoho@group:7"}}
    assert project_audit._unresolvable_connector_why("zoho", 35, link) is None


def test_audit_project_surfaces_signal(monkeypatch):
    # Intégration : audit_project d'un projet ORG-owned liant `zoho` group-only.
    _stub_group_secret(monkeypatch, org_secret=False, group_has=True)
    monkeypatch.setattr(db, "get_project_by_id",
                        lambda pid: {"owner_type": "org", "owner_id": "35"})
    monkeypatch.setattr(db, "list_project_links", lambda pid: [
        {"target_type": "connecteur", "target_ref": "zoho", "config": {},
         "identity_ref": None},
    ])
    monkeypatch.setattr(db, "project_run_stats", lambda pid: {"runs": 0})
    assert "zoho" in providers.REGISTRY  # garde : le connecteur existe au registre

    out = project_audit.audit_project(1)
    assert len(out["unresolvable_connectors"]) == 1
    assert out["unresolvable_connectors"][0]["target_ref"] == "zoho"
    assert out["dead_links"] == []


def test_audit_project_team_owned_not_flagged(monkeypatch):
    # Un projet d'ÉQUIPE co-pose son groupe (fix B) → le même lien n'est PAS signalé.
    _stub_group_secret(monkeypatch, org_secret=False, group_has=True)
    monkeypatch.setattr(db, "get_project_by_id",
                        lambda pid: {"owner_type": "group", "owner_id": "7"})
    monkeypatch.setattr(db, "list_project_links", lambda pid: [
        {"target_type": "connecteur", "target_ref": "zoho", "config": {},
         "identity_ref": None},
    ])
    monkeypatch.setattr(db, "project_run_stats", lambda pid: {"runs": 0})
    out = project_audit.audit_project(1)
    assert out["unresolvable_connectors"] == []
