"""« Cet agent a-t-il tourné ? », servi avec l'agent (`last_run`, `last_done_at`).

La seule réponse était la liste des runs de l'org (`oto_org_monitoring op=runs`), plus
récent d'abord. Un agent toutes les quinze minutes en remplit la première page en une
dizaine d'heures ; un agent de veille y cherchait le soir le passage du matin d'un
autre agent, ne le trouvait pas, et levait « n'a pas tourné » à tort.

Ce banc tient : la dernière occurrence et le dernier succès de CHAQUE agent, quel que
soit le bruit des autres ; l'org, le genre de travail et la fenêtre bornent la
lecture ; la liste et la fiche servent les deux champs ; `op=runs` de la console dit ce
qu'il n'a pas lu et renvoie à `last_run`.
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities import org_monitoring as OM
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import ResolvedCtx

ORG = 8311


def _job(c, org, tid, statut, il_y_a_h, *, kind="start", fini=True):
    c.execute(
        "INSERT INTO runner_jobs (org_id, kind, status, payload, due_at, finished_at) "
        "VALUES (%s, %s, %s, jsonb_build_object('trigger_id', %s::text), "
        "NOW() - %s * interval '1 hour', "
        "CASE WHEN %s THEN NOW() - %s * interval '1 hour' + interval '10 minutes' END)",
        (org, kind, statut, str(tid), il_y_a_h, fini, il_y_a_h))



def _agent(org, proc):
    from oto_mcp import db
    return db.create_trigger(org, "proprio", procedure=proc, tz="UTC",
                             tools=["data_write"], cron="5 6 * * *",
                             next_due="2030-01-01 00:00:00")["id"]


@pytest.fixture
def agents(live):
    from oto_mcp.db import _conn as dbconn
    matin, frequent, muet, ancien = (_agent(ORG, p) for p in ("matin", "frequent", "muet", "ancien"))
    with dbconn._connect() as c:
        # Le matin : un succès hier, un passage ce matin mort dans la file.
        _job(c, ORG, matin, "done", 26)
        _job(c, ORG, matin, "failed", 14)
        # Une reprise (`continue`) n'est pas une occurrence de l'agent.
        _job(c, ORG, matin, "claimed", 1, kind="continue", fini=False)
        # Le bruit : un agent fréquent, 150 passages depuis.
        for k in range(150):
            _job(c, ORG, frequent, "done", 13 - k * 0.08)
        # Plus vieux que la fenêtre : ne compte pas.
        _job(c, ORG, ancien, "done", 24 * 40)
        # Le même agent vu depuis une autre org : ne compte pas.
        _job(c, ORG + 1, muet, "done", 2)
    return {"matin": matin, "frequent": frequent, "muet": muet, "ancien": ancien}


def test_chaque_agent_a_sa_derniere_occurrence_et_son_dernier_succes(agents):
    from oto_mcp import db
    ids = list(agents.values())
    lu = db.derniers_travaux(ORG, ids)
    assert set(lu) == set(ids), "chaque id demandé est présent"
    matin = lu[agents["matin"]]
    assert matin["last_run"]["status"] == "failed", "la DERNIÈRE occurrence, pas la dernière réussie"
    assert set(matin["last_run"]) == {"status", "due_at", "finished_at"}
    assert matin["last_done_at"] is not None and matin["last_done_at"] < matin["last_run"]["due_at"], (
        "le dernier succès est celui d'hier, avant l'échec de ce matin")
    assert lu[agents["frequent"]]["last_run"]["status"] == "done"
    assert lu[agents["muet"]] == {"last_run": None, "last_done_at": None}, (
        "un travail d'une autre org ne se compte pas")
    assert lu[agents["ancien"]] == {"last_run": None, "last_done_at": None}, (
        "au-delà de la fenêtre, rien")
    assert db.derniers_travaux(ORG, []) == {}


def test_le_passage_du_matin_reste_visible_derriere_un_agent_frequent(agents):
    """Ce que la liste des runs perdait : 150 passages plus récents d'un autre agent
    ne cachent pas l'occurrence du matin."""
    from oto_mcp import db
    lu = db.derniers_travaux(ORG, [agents["matin"]])[agents["matin"]]
    assert lu["last_run"] is not None and lu["last_run"]["due_at"] is not None


def test_la_liste_et_la_fiche_servent_le_dernier_passage(agents, monkeypatch):
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **k: None)
    monkeypatch.setattr(RT.tool_registry, "bound_instance", lambda: None)
    ctx = ResolvedCtx(sub="proprio", org_id=ORG)
    liste = RT._triggers_sync(ctx, RT.TriggerInput(op="list"))
    par_id = {t["id"]: t for t in liste["triggers"]}
    assert par_id[agents["matin"]]["last_run"]["status"] == "failed"
    assert par_id[agents["muet"]]["last_run"] is None
    fiche = RT._triggers_sync(ctx, RT.TriggerInput(op="get", trigger_id=agents["matin"]))
    assert fiche["trigger"]["last_run"] == par_id[agents["matin"]]["last_run"]
    assert fiche["trigger"]["last_done_at"] == par_id[agents["matin"]]["last_done_at"]


def test_op_runs_dit_ce_qu_il_couvre_et_ce_qu_il_n_a_pas_lu():
    rangs = [{"run_id": "b", "started_at": "2026-01-02 10:00:00"},
             {"run_id": "a", "started_at": "2026-01-02 08:00:00"}]
    inp = OM.OrgMonitoringInput(op="runs", org_id=ORG, days=1, limit=900, sub="x",
                                errors=True, cursor="c", since="2026-01-01", tool="t")
    out = OM._avec_portee_des_runs(inp, {"runs": list(rangs)})
    assert out["runs"] == rangs, "la liste elle-même ne change pas"
    assert out["ignored_params"] == ["since", "tool"], (
        "seuls les champs que `runs` ne lit pas sont nommés — `days`, `sub`, `errors`, "
        "`cursor` sont lus")
    assert "last_run" in out["hint"]
    sobre = OM._avec_portee_des_runs(OM.OrgMonitoringInput(op="runs", org_id=ORG),
                                     {"runs": []})
    assert "ignored_params" not in sobre and "last_run" in sobre["hint"]
