"""Audit LÉGER (oto/#6 A7) : sur `oto_project op=list`, l'audit ne fait que les
checks EN MÉMOIRE (liens morts détectables sans requête) — zéro requête par
projet/par-lien, pour tuer le N+1 (timeout 180 s). L'audit complet reste sur op=get."""
from oto_mcp import project_audit
from oto_mcp import db


def test_light_audit_skips_expensive_calls(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("appel coûteux interdit en mode light")
    monkeypatch.setattr(db, "get_project_by_id", _boom)
    monkeypatch.setattr(db, "project_run_stats", _boom)
    monkeypatch.setattr(project_audit, "_resolved_procedure", _boom)
    monkeypatch.setattr(project_audit, "_unresolvable_connector_why", _boom)
    links = [
        {"target_type": "tableau", "target_ref": "9", "namespace": None},        # mort (cheap)
        {"target_type": "connecteur", "target_ref": "connecteur_inexistant"},    # mort (cheap)
        {"target_type": "procedure", "target_ref": "5"},                          # ignoré en light
        {"target_type": "connecteur", "target_ref": "zoho"},                      # OK, pas de résolvabilité
    ]
    out = project_audit.audit_project(7, links, light=True)
    refs = {d["target_ref"] for d in out["dead_links"]}
    assert refs == {"9", "connecteur_inexistant"}          # seuls les checks en mémoire
    assert out["unbound_slots"] == [] and out["inert_procedures"] == []
