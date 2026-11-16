"""Console d'observabilité d'org `oto_org_monitoring` (capabilities/org_monitoring.py).

Ce qu'on garde vert :
  1. le SCOPE — chaque lentille passe `org_id` à sa projection db (une lentille qui
     l'oublie rendrait des chiffres plateforme sous un écran d'org) ;
  2. les GARDES cross-org — `call` (id entier devinable) et `run` renvoient 404, jamais
     le contenu d'une autre org ;
  3. le dispatch (défauts de fenêtre par op, params requis) ;
  4. l'autz déclarée : ORG_ADMIN_OF sur TOUTES les surfaces, sans exception.
"""
import pytest

from oto_mcp.capabilities import org_monitoring as om
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="admin-sub", org_id=35)


def _inp(**kw):
    return om.OrgMonitoringInput(org_id=35, **kw)


# ── 1. scope : chaque lentille borne à l'org ────────────────────────────────

def test_every_lens_scopes_to_the_org(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(om, "_resolve_sub", lambda t: t, raising=False)
    monkeypatch.setattr(om.monitoring, "_resolve_sub", lambda t: t)
    monkeypatch.setattr(om.db, "tool_call_stats",
                        lambda **kw: seen.update(summary=kw) or {})
    monkeypatch.setattr(om.db, "list_tool_calls_page",
                        lambda **kw: seen.update(calls=kw) or {"calls": [], "next": None})
    monkeypatch.setattr(om.db, "count_calls_of_org_runs_elsewhere",
                        lambda oid, **kw: seen.update(hors=oid) or 0)
    monkeypatch.setattr(om.db, "connector_failure_stats",
                        lambda **kw: seen.update(conn=kw) or {})
    monkeypatch.setattr(om.db, "org_adoption",
                        lambda oid, **kw: seen.update(adoption=(oid, kw)) or {})
    monkeypatch.setattr(om.db, "list_runs_page", lambda limit, **kw: seen.update(
        runs=(limit, kw)) or {"runs": [], "next": None, "scanned": 0})
    monkeypatch.setattr(om.db, "aggregate_gaps",
                        lambda days, **kw: seen.update(gaps=(days, kw)) or [])
    monkeypatch.setattr(om.db, "aggregate_tool_feedback",
                        lambda days, **kw: seen.update(quality=(days, kw)) or [])

    for op in ("summary", "calls", "connectors", "adoption", "runs", "gaps", "tool_quality"):
        om._console(CTX, _inp(op=op))

    assert seen["summary"]["org_id"] == 35
    assert seen["calls"]["org_id"] == 35
    assert seen["hors"] == 35                # le plancher (#630) est scopé pareil
    assert seen["conn"]["org_id"] == 35
    assert seen["adoption"] == (35, {"active_window_days": 30})
    assert seen["runs"] == (100, {"org_id": 35, "sub": None, "since_days": None,
                                  "before": None, "issues": None})
    assert seen["gaps"] == (30, {"org_id": 35})
    assert seen["quality"] == (30, {"org_id": 35})


def test_window_defaults_differ_by_lens(monkeypatch):
    """7 j sur les lentilles de trafic, 30 j sur celles qui mesurent une adoption ou
    un signal rare — mêmes défauts que la console plateforme."""
    seen: dict = {}
    monkeypatch.setattr(om.db, "tool_call_stats", lambda **kw: seen.update(s=kw["since_days"]) or {})
    monkeypatch.setattr(om.db, "connector_failure_stats", lambda **kw: seen.update(c=kw["since_days"]) or {})
    monkeypatch.setattr(om.db, "org_adoption", lambda oid, **kw: seen.update(a=kw["active_window_days"]) or {})
    monkeypatch.setattr(om.monitoring, "_resolve_sub", lambda t: None)
    om._console(CTX, _inp(op="summary"))
    om._console(CTX, _inp(op="connectors"))
    om._console(CTX, _inp(op="adoption"))
    assert seen == {"s": 7, "c": 7, "a": 30}


# ── 2. gardes cross-org ─────────────────────────────────────────────────────

def test_call_of_another_org_is_a_404_not_a_leak(monkeypatch):
    """`call_id` est un BIGSERIAL : sans cette garde, un org_admin itère les ids et lit
    le journal de toute la plateforme. Même 404 qu'un id inexistant (ne pas confirmer)."""
    monkeypatch.setattr(om.db, "get_tool_call",
                        lambda cid: {"id": cid, "org_id": 99, "tool": "folk_record"})
    with pytest.raises(AuthzDenied) as e:
        om._console(CTX, _inp(op="call", call_id=1234))
    assert (e.value.status, e.value.code) == (404, "unknown_call")

    monkeypatch.setattr(om.db, "get_tool_call", lambda cid: None)
    with pytest.raises(AuthzDenied) as e:
        om._console(CTX, _inp(op="call", call_id=1234))
    assert e.value.status == 404


def test_call_of_my_org_passes(monkeypatch):
    monkeypatch.setattr(om.db, "get_tool_call", lambda cid: {"id": cid, "org_id": 35})
    out = om._console(CTX, _inp(op="call", call_id=7))
    assert out["call"]["id"] == 7


def test_run_scopes_and_404s_when_empty(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(om.db, "get_run",
                        lambda rid, **kw: seen.update(rid=rid, kw=kw) or [])
    with pytest.raises(AuthzDenied) as e:
        om._console(CTX, _inp(op="run", run_id="r-other-org"))
    assert (e.value.status, e.value.code) == (404, "unknown_run")
    assert seen["kw"] == {"org_id": 35}      # le filtre est bien descendu en SQL

    monkeypatch.setattr(om.db, "get_run", lambda rid, **kw: [{"tool": "run_start"}])
    # #665 : le registre d'archive est lu sous le MÊME scope d'org que la timeline.
    monkeypatch.setattr(om.db, "run_content_archived",
                        lambda rid, **kw: seen.update(archive_kw=kw) and None)
    out = om._console(CTX, _inp(op="run", run_id="r1"))
    assert out["run_id"] == "r1" and out["content_archived"] is None
    assert seen["archive_kw"] == {"org_id": 35}


def test_call_et_run_ne_rendent_jamais_args(monkeypatch):
    """oto-backend#563, décision d'Alexis du 15/09/2026 : les arguments complets d'un
    appel restent à la supervision PLATEFORME seule, jamais à l'org_admin de l'org
    émettrice. La garde RÉELLE est ici — dans ce que le handler renvoie — pas dans le
    modèle `Output=`, qui ne filtre rien à l'exécution (`api/base.py::_json` sert le
    dict du handler tel quel). Source (mockée) porte `args` : si le handler ne le
    retire pas explicitement, ce test le voit ressortir."""
    monkeypatch.setattr(om.db, "get_tool_call", lambda cid: {
        "id": cid, "org_id": 35, "tool": "data_write",
        "args": {"row": {"email": "personne@exemple.test"}}})
    out = om._console(CTX, _inp(op="call", call_id=7))
    assert "args" not in out["call"], out["call"]

    monkeypatch.setattr(om.db, "get_run", lambda rid, **kw: [
        {"id": 1, "tool": "email_send", "args": {"to": "personne@exemple.test"}}])
    monkeypatch.setattr(om.db, "run_content_archived", lambda rid, **kw: None)
    out = om._console(CTX, _inp(op="run", run_id="r1"))
    assert all("args" not in c for c in out["calls"]), out["calls"]


# ── 3. dispatch ─────────────────────────────────────────────────────────────

def test_required_params():
    with pytest.raises(AuthzDenied) as e:
        om._console(CTX, _inp(op="call"))
    assert e.value.code == "missing_call_id"
    with pytest.raises(AuthzDenied) as e:
        om._console(CTX, _inp(op="run"))
    assert e.value.code == "missing_run_id"


def test_export_reuses_the_audit_log_capability(monkeypatch):
    """Le journal d'audit org (#67) existe déjà avec la même autz et le même scope :
    la console le REBRANCHE (un seul contrat), elle n'en écrit pas un second."""
    seen: dict = {}
    monkeypatch.setattr(om.audit_log, "_export",
                        lambda ctx, inp: seen.update(org=inp.org_id, since=inp.since,
                                                     limit=inp.limit) or {"count": 0})
    om._console(CTX, _inp(op="export", since="2026-08-01"))
    assert seen == {"org": 35, "since": "2026-08-01", "limit": 1000}


# ── 4. autz déclarée ────────────────────────────────────────────────────────

def test_all_surfaces_are_org_admin_scoped():
    """Un oubli d'autz sur UNE lentille ouvrirait le journal nominatif à tout membre.
    La règle est la même instance partagée — on vérifie qu'aucune capacité n'y échappe."""
    from oto_mcp.capabilities.registry import CAPABILITIES
    caps = [c for c in CAPABILITIES if c.key.startswith("org.monitoring.")]
    assert len(caps) == 12                       # 11 lentilles REST + la console MCP
    assert all(c.authz is om._ADMIN_OF for c in caps)


def test_rest_paths_are_org_scoped_in_the_path():
    """Le scope doit se LIRE dans le chemin (règle des jetons portés, cf. CLAUDE.md
    §REST) : chaque route porte `/api/orgs/{id}/`, jamais l'org dans le corps."""
    from oto_mcp.capabilities.registry import CAPABILITIES
    paths = {b.path for c in CAPABILITIES if c.key.startswith("org.monitoring.")
             for b in c.rest_bindings()}
    assert paths == {
        "/api/orgs/{id}/monitoring/summary",
        "/api/orgs/{id}/monitoring/view-as",
        "/api/orgs/{id}/monitoring/calls",
        "/api/orgs/{id}/monitoring/calls/{call_id}",
        "/api/orgs/{id}/monitoring/connectors",
        "/api/orgs/{id}/monitoring/adoption",
        "/api/orgs/{id}/monitoring/runs",
        "/api/orgs/{id}/monitoring/runs/{run_id}",
        "/api/orgs/{id}/monitoring/gaps",
        "/api/orgs/{id}/monitoring/tool-quality",
        "/api/orgs/{id}/monitoring/signals",
    }
    assert all(p.startswith("/api/orgs/{id}/") for p in paths)
