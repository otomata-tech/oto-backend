"""Export du journal d'audit org-scopé (oto-backend#67).

Vérifie le routage du handler (org → membres → appels, namespace dérivé) et le
câblage de la capacité (REST-only, gatée ORG_ADMIN_OF).
"""
from oto_mcp.capabilities import audit_log as al
from oto_mcp.capabilities import registry
from oto_mcp.capabilities._types import ResolvedCtx

CTX = ResolvedCtx(sub="admin", org_id=7)


def test_export_scopes_to_call_org_and_derives_namespace(monkeypatch):
    captured = {}

    def fake_read(org_id, since=None, until=None, limit=1000):
        captured.update(org_id=org_id, since=since, until=until, limit=limit)
        return [{"tool": "fr_get", "ok": True}, {"tool": "oto_admin_org", "ok": True}]

    monkeypatch.setattr(al.db, "list_tool_calls_for_org", fake_read)
    out = al._export(CTX, al.AuditExportInput(org_id=7, since="2026-06-01", limit=500))

    # filtre EXACT par org de l'appel (pas l'appartenance des membres)
    assert captured["org_id"] == 7
    assert captured["since"] == "2026-06-01" and captured["limit"] == 500
    assert out["org_id"] == 7 and out["count"] == 2
    assert [c["namespace"] for c in out["calls"]] == ["fr", "oto"]


def test_empty_yields_zero(monkeypatch):
    monkeypatch.setattr(al.db, "list_tool_calls_for_org", lambda org_id, **k: [])
    out = al._export(CTX, al.AuditExportInput(org_id=7))
    assert out["count"] == 0 and out["calls"] == []


def test_capability_is_rest_only_org_admin_gated():
    cap = next(c for c in registry.CAPABILITIES if c.key == "org.audit_log.export")
    assert cap.mcp is None                                   # REST-only
    b = cap.rest_bindings()[0]
    assert b.verb == "GET" and b.path == "/api/orgs/{id}/audit-log/export"
    # ORG_ADMIN_OF est une règle paramétrée (closure) — présence + appelable.
    assert callable(cap.authz)
