"""Console d'investigation `oto_admin_monitoring` (capabilities/monitoring.py).

Logique pure : dispatch op→handler (stubs db), défauts de fenêtre par op,
paramètres requis (`call_id`, `run_id`), résolution email→sub du filtre appelant,
et fiche d'appel introuvable → AuthzDenied 404 (jamais un 500).
"""
import pytest

from oto_mcp.capabilities import monitoring, usage
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="admin-sub")


def test_summary_defaults_and_sub_resolution(monkeypatch):
    seen = {}
    monkeypatch.setattr(monitoring.db, "tool_call_stats",
                        lambda since_days, org_id, sub: seen.update(
                            days=since_days, org_id=org_id, sub=sub) or {"ok": 1})
    monkeypatch.setattr(monitoring, "_resolve_sub", lambda t: "sub-jane" if t else None)
    out = monitoring._monitoring(CTX, monitoring.MonitoringInput(
        op="summary", sub="jane@example.com"))
    assert out == {"ok": 1}
    assert seen == {"days": 7, "org_id": None, "sub": "sub-jane"}


def test_calls_passes_investigation_filters(monkeypatch):
    seen = {}
    monkeypatch.setattr(monitoring.db, "list_tool_calls",
                        lambda **kw: seen.update(kw) or [])
    monitoring._monitoring(CTX, monitoring.MonitoringInput(
        op="calls", run_id="r1", session_id="s1", min_duration_ms=5000,
        error_contains="timeout", errors=True, tool="folk_record"))
    assert seen["run_id"] == "r1"
    assert seen["session_id"] == "s1"
    assert seen["min_duration_ms"] == 5000
    assert seen["error_contains"] == "timeout"
    assert seen["errors_only"] is True
    assert seen["tool_name"] == "folk_record"
    assert seen["limit"] == 200          # défaut console


def test_call_requires_id_and_404s_on_unknown(monkeypatch):
    with pytest.raises(AuthzDenied) as e:
        monitoring._monitoring(CTX, monitoring.MonitoringInput(op="call"))
    assert e.value.code == "missing_call_id"
    monkeypatch.setattr(monitoring.db, "get_tool_call", lambda cid: None)
    with pytest.raises(AuthzDenied) as e:
        monitoring._monitoring(CTX, monitoring.MonitoringInput(op="call", call_id=999))
    assert e.value.status == 404


def test_run_requires_run_id():
    with pytest.raises(AuthzDenied) as e:
        monitoring._monitoring(CTX, monitoring.MonitoringInput(op="run"))
    assert e.value.code == "missing_run_id"


def test_rest_paths_are_unchanged_for_the_dashboard():
    """Les lentilles ont quitté `api.routes` pour la couche capacité : les CHEMINS
    doivent rester identiques (le dashboard tape ces URLs, cf. api/console.ts) —
    une migration interne ne doit jamais casser une surface consommée."""
    from oto_mcp.capabilities.registry import CAPABILITIES
    paths = {b.path for c in CAPABILITIES if c.key.startswith("monitoring.")
             for b in c.rest_bindings()}
    assert paths == {
        # Un chemin qui s'AJOUTE ne casse rien ; l'égalité stricte est là pour qu'il
        # apparaisse dans le diff, donc qu'il soit ajouté délibérément. Un chemin qui
        # DISPARAÎT de cette liste est, lui, une surface consommée qu'on retire.
        "/api/admin/monitoring/transport",
        "/api/admin/monitoring/summary",
        "/api/admin/monitoring/rest",
        "/api/admin/monitoring/rest-calls",
        "/api/admin/monitoring/connectors",
        "/api/admin/monitoring/funnel",
        "/api/admin/monitoring/calls",
        "/api/admin/monitoring/calls/{call_id}",
    }


def test_usage_ops_reuse_adr0017_handlers(monkeypatch):
    seen = {}
    monkeypatch.setattr(usage.db, "aggregate_gaps", lambda days: seen.update(gaps=days) or [])
    monkeypatch.setattr(usage.db, "list_runs", lambda limit: seen.update(runs=limit) or [])
    monitoring._monitoring(CTX, monitoring.MonitoringInput(op="gaps"))
    monitoring._monitoring(CTX, monitoring.MonitoringInput(op="runs"))
    assert seen == {"gaps": 30, "runs": 100}   # défauts par op (gaps 30j, runs 100)


# ── #451 : ce que la console acceptait puis jetait, et ce qu'elle ne disait pas ──

def test_rest_honore_le_filtre_appelant(monkeypatch):
    """Signalement #451 : on enquête sur UN compte, on passe `sub`, et la console
    construisait un `WindowInput(days=…)` — le filtre n'atteignait jamais le SQL.
    La réponse décrivait toute la plateforme et rien ne distinguait les deux
    lectures : l'enquêteur a conclu sur l'activité d'un compte qu'il n'avait pas
    mesurée."""
    seen = {}
    monkeypatch.setattr(monitoring.db, "rest_call_stats",
                        lambda **kw: seen.update(kw) or {"total_calls": 3})
    monkeypatch.setattr(monitoring, "_resolve_sub", lambda t: "sub-jane" if t else None)

    out = monitoring._monitoring(CTX, monitoring.MonitoringInput(
        op="rest", days=30, sub="jane@example.com", org_id=42))

    assert out == {"total_calls": 3}
    assert seen == {"since_days": 30, "sub": "sub-jane", "org_id": 42, "route": None}


def test_rest_honore_le_filtre_route(monkeypatch):
    """oto-dashboard#125 : mesurer une route précise (préfixe), pas seulement un
    compte — `by_route` est plafonné à 100, une route à faible volume peut y être
    invisible sans que rien ne le dise."""
    seen = {}
    monkeypatch.setattr(monitoring.db, "rest_call_stats",
                        lambda **kw: seen.update(kw) or {"total_calls": 0})
    monitoring._monitoring(CTX, monitoring.MonitoringInput(
        op="rest", route="/api/atlassian/oauth/start"))
    assert seen["route"] == "/api/atlassian/oauth/start"


def test_connectors_honore_le_scope_d_org(monkeypatch):
    """Même défaut, même console : `connector_failure_stats` sait scoper par org
    depuis toujours, la console jetait quand même `org_id`."""
    seen = {}
    monkeypatch.setattr(monitoring.db, "connector_failure_stats",
                        lambda **kw: seen.update(kw) or {})
    monitoring._monitoring(CTX, monitoring.MonitoringInput(op="connectors", org_id=42))
    assert seen == {"since_days": 7, "org_id": 42}


def test_un_filtre_que_l_op_ne_lit_pas_est_refuse_et_nomme():
    """La troisième option — accepter et ignorer — était celle en place. On honore
    là où la donnée existe, on REFUSE ailleurs, et le refus nomme l'op qui lit ce
    filtre : sans ça l'appelant ne sait pas où porter son enquête."""
    with pytest.raises(AuthzDenied) as e:
        monitoring._monitoring(CTX, monitoring.MonitoringInput(op="funnel", sub="jane@x"))
    assert e.value.status == 400 and e.value.code == "param_not_read_by_op"
    assert "`sub`" in e.value.message and "summary" in e.value.message

    # `tool` n'est lu que par `calls` : sur `rest`, il serait tombé dans le vide.
    with pytest.raises(AuthzDenied) as e:
        monitoring._monitoring(CTX, monitoring.MonitoringInput(op="rest", tool="fr_get"))
    assert e.value.code == "param_not_read_by_op"


def test_le_defaut_d_un_champ_ne_declenche_pas_le_refus(monkeypatch):
    """⚠️ Sur la face MCP, fastmcp remplit TOUS les défauts avant d'appeler le
    handler : `model_fields_set` porte les dix champs même sur `{"op":"funnel"}`.
    Un garde qui lirait « champ fourni » refuserait donc tous les appels."""
    monkeypatch.setattr(monitoring.db, "activation_funnel", lambda **kw: {})
    champs = {n: f.default for n, f in monitoring.MonitoringInput.model_fields.items()
              if n != "op"}
    assert monitoring._monitoring(CTX, monitoring.MonitoringInput(op="funnel", **champs)) == {}


def test_les_champs_lus_collent_au_dispatch():
    """La table `_CHAMPS_LUS` est ce qui refuse : si elle diverge du `Literal` des
    ops, un op inconnu d'elle lèverait un KeyError au lieu de répondre."""
    ops = set(monitoring.MonitoringInput.model_fields["op"].annotation.__args__)
    assert set(monitoring._CHAMPS_LUS) == ops
    connus = set(monitoring.MonitoringInput.model_fields) - {"op"}
    for op, champs in monitoring._CHAMPS_LUS.items():
        assert champs <= connus, f"{op} déclare un champ absent de l'Input : {champs - connus}"


def test_rest_calls_sert_view_as_sub_quand_pose(monkeypatch):
    """oto-backend#962 : la lentille plateforme ligne-par-ligne existe et sert bien
    `view_as_sub` — ni `op=calls` (MCP only) ni `op=rest` (agrégats) ne peuvent le
    montrer."""
    seen = {}
    monkeypatch.setattr(monitoring.db, "list_rest_calls",
                        lambda **kw: seen.update(kw) or [
                            {"id": 1, "route": "GET /api/orgs/1", "view_as_sub": "sub-jane"},
                            {"id": 2, "route": "GET /api/me", "view_as_sub": None},
                        ])
    out = monitoring._monitoring(CTX, monitoring.MonitoringInput(
        op="rest_calls", org_id=7, route="/api/orgs"))
    assert seen["org_id"] == 7
    assert seen["route"] == "/api/orgs"
    assert seen["limit"] == 200
    assert out["calls"][0]["view_as_sub"] == "sub-jane"
    assert out["calls"][1]["view_as_sub"] is None    # jamais devinée, jamais omise


def test_rest_calls_refuse_les_champs_dune_autre_op():
    with pytest.raises(AuthzDenied) as e:
        monitoring._monitoring(CTX, monitoring.MonitoringInput(
            op="rest_calls", run_id="r1"))
    assert e.value.code == "param_not_read_by_op"


def test_la_console_dit_que_les_gestes_du_dashboard_ne_sont_pas_dans_calls():
    """Second piège du #451 : `calls`/`summary` ne portent que les appels d'AGENT
    (kind='mcp'). Un compte qui n'utilise que le tableau de bord y est à zéro — et
    « zéro appel » se lit spontanément « compte inactif ». La description est
    l'instruction relue à CHAQUE appel : c'est elle qui doit le dire."""
    cap = next(c for c in monitoring.CAPABILITIES if c.key == "admin.monitoring")
    d = cap.description
    assert "op=rest" in d and "zero calls" in d
    assert "param_not_read_by_op" in d          # le refus est annoncé, pas découvert
