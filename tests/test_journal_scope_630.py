"""#630 — la vue filtrée du journal omet un appel que le déroulé du travail montre.

Mesuré en production le 29/08/2026 : un `data_write` refusé « namespace inconnu » à
21:11:23, visible dans `op=run` (les 17 appels du run), absent de `op=calls org_id=226`
interrogé trois fois avec des motifs que son texte contient. Rejoué contre la base :
avec `org_id=226` la vue rend 1 refus portant le nom du tableau ; sans, 40. La condition
qui l'élimine est le SCOPE de la vue — `tool_calls.org_id = 226` — parce que l'appel a
été RÉSOLU sous l'org maison de l'appelant (2), l'axe `_org` étant absent (#631).

La vue est exacte dans son périmètre ; ce qui manquait est que le lecteur ne sache pas
ce que ce périmètre laisse dehors. Un compte tiré d'une vue filtrée était donc un
PLANCHER, et un « zéro » y était muet. Ce qu'on garde vert :

  1. le store compte, avec LES MÊMES filtres que la page, les appels des runs de l'org
     résolus sous une autre org — la seule façon d'avoir un plancher comparable ;
  2. les deux consoles (org, plateforme avec `org_id`) rendent ce compte à côté des
     lignes, même quand il vaut 0 — et disent où voir ces appels quand il y en a ;
  3. sans scope d'org, rien n'est laissé dehors : pas de champ.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from oto_mcp.capabilities import monitoring as mon
from oto_mcp.capabilities import org_monitoring as om
from oto_mcp.capabilities._types import ResolvedCtx

CTX = ResolvedCtx(sub="admin-sub", org_id=35)


# ── 2/3. les consoles rendent le plancher, ou rien quand il n'y a pas de scope ──

def _stub(monkeypatch, *, lignes, hors):
    seen: dict = {}
    monkeypatch.setattr(om.monitoring, "_resolve_sub", lambda t: t)
    monkeypatch.setattr(mon.db, "list_tool_calls_page",
                        lambda **kw: seen.update(page=kw)
                        or {"calls": list(lignes), "next": None})
    monkeypatch.setattr(mon.db, "count_calls_of_org_runs_elsewhere",
                        lambda org_id, **kw: seen.update(hors=(org_id, kw)) or hors)
    return seen


def test_la_console_d_org_dit_ce_que_son_scope_laisse_dehors(monkeypatch):
    seen = _stub(monkeypatch, lignes=[{"id": 1, "org_id": 35}], hors=3)
    out = om._console(CTX, om.OrgMonitoringInput(org_id=35, op="calls",
                                                 error_contains="inconnu", errors=True))
    assert out["calls"] == [{"id": 1, "org_id": 35}]
    assert out["hors_scope"] == 3
    assert "3" in out["hors_scope_hint"] and "op=run" in out["hors_scope_hint"], out
    assert "org 35" in out["scope"], out
    # Le plancher se compte avec LES MÊMES filtres que la page — sinon il ne dit rien.
    org, kw = seen["hors"]
    assert org == 35
    assert kw["error_contains"] == "inconnu" and kw["errors_only"] is True, kw
    assert kw["since"] is not None, "sans borne, le compte parcourrait tout le journal"


def test_zero_n_est_pas_muet(monkeypatch):
    """Le champ est là même à 0 : c'est la preuve que la vue a regardé dehors."""
    _stub(monkeypatch, lignes=[], hors=0)
    out = om._console(CTX, om.OrgMonitoringInput(org_id=35, op="calls"))
    assert out["calls"] == [] and out["hors_scope"] == 0, out
    assert out.get("hors_scope_hint") is None, out
    assert "org 35" in out["scope"], out


def test_la_console_plateforme_scopee_par_org_fait_pareil(monkeypatch):
    seen = _stub(monkeypatch, lignes=[], hors=1)
    monkeypatch.setattr(mon, "_resolve_sub", lambda t: t)
    out = mon._monitoring(CTX, mon.MonitoringInput(op="calls", org_id=226, days=1))
    assert out["hors_scope"] == 1 and "1" in out["hors_scope_hint"], out
    assert seen["hors"][0] == 226


def test_sans_scope_d_org_rien_n_est_dehors(monkeypatch):
    """La console plateforme sans `org_id` voit tout le journal : aucun plancher à
    déclarer, donc aucun champ — un compte à 0 ici serait une phrase qui meuble."""
    seen = _stub(monkeypatch, lignes=[{"id": 1}], hors=99)
    monkeypatch.setattr(mon, "_resolve_sub", lambda t: t)
    out = mon._monitoring(CTX, mon.MonitoringInput(op="calls"))
    assert out == {"calls": [{"id": 1}]}, out
    assert "hors" not in seen


def test_la_fenetre_du_plancher_suit_la_page(monkeypatch):
    """`days` donné → depuis `days` ; page PLEINE sans `days` → depuis l'appel le plus
    ancien de la page (le plancher est comparable à ce que la page montre) ; page
    incomplète sans `days` → une fenêtre bornée, DITE dans l'indice."""
    ancien = datetime(2026, 8, 29, 20, 0, tzinfo=timezone.utc)
    page = [{"id": i, "called_at": ancien + timedelta(minutes=i)} for i in range(3)]

    seen = _stub(monkeypatch, lignes=page, hors=2)
    om._console(CTX, om.OrgMonitoringInput(org_id=35, op="calls", limit=3))
    assert seen["hors"][1]["since"] == ancien, "page pleine : l'horizon est celui de la page"

    seen = _stub(monkeypatch, lignes=page, hors=2)
    om._console(CTX, om.OrgMonitoringInput(org_id=35, op="calls", limit=50, days=2))
    since = seen["hors"][1]["since"]
    assert timedelta(days=1, hours=23) < datetime.now(timezone.utc) - since < timedelta(days=2, minutes=1)

    seen = _stub(monkeypatch, lignes=page, hors=2)
    out = om._console(CTX, om.OrgMonitoringInput(org_id=35, op="calls", limit=50))
    since = seen["hors"][1]["since"]
    assert timedelta(days=29) < datetime.now(timezone.utc) - since < timedelta(days=30, minutes=1)
    assert "30 j" in out["hors_scope_hint"], out


# ── 1. le store : même filtres que la page, vrai PostgreSQL ────────────────────


@pytest.fixture
def journal(live):
    """Le soir du 29/08 en miniature : un run de l'org TRAVAIL dont un appel a été
    résolu sous l'org MAISON de l'appelant ; un run d'une autre org ; un appel hors run."""
    from oto_mcp import db, org_store
    sub = "sub-630-" + uuid.uuid4().hex[:6]
    travail = org_store.create_org("Travail 630", created_by=sub)
    maison = org_store.create_org("Maison 630", created_by=sub)
    autre = org_store.create_org("Autre 630", created_by=sub)
    run, run_autre = uuid.uuid4().hex, uuid.uuid4().hex
    db.insert_run(run, sub=sub, org_id=travail, label="campagne")
    db.insert_run(run_autre, sub=sub, org_id=autre, label="ailleurs")
    refus = "Error calling tool 'data_write': namespace `copie-eval` inconnu"

    def _appel(**kw):
        db.insert_tool_call({"sub": sub, "kind": "mcp", "ok": False, "error": refus,
                             "tool": "data_write", "args": {"namespace": "copie-eval"},
                             **kw})
    _appel(org_id=travail, run_id=run)                       # dans le scope
    _appel(org_id=maison, run_id=run)                        # le 21:11:23 : dehors
    _appel(org_id=maison, run_id=run, tool="data_rows")      # dehors, autre outil
    _appel(org_id=maison, run_id=run, ok=True, error=None)   # dehors, mais pas un refus
    _appel(org_id=autre, run_id=run_autre)                   # run d'une autre org
    _appel(org_id=maison, run_id=None)                       # hors de tout run
    return {"sub": sub, "travail": travail, "maison": maison, "run": run}


def test_le_store_compte_ce_que_la_page_ne_montre_pas_avec_ses_filtres(journal):
    from oto_mcp import db
    depuis = datetime.now(timezone.utc) - timedelta(hours=1)
    page = db.list_tool_calls(org_id=journal["travail"], errors_only=True,
                              error_contains="copie-eval")
    assert len(page) == 1 and page[0]["org_id"] == journal["travail"], page

    n = db.count_calls_of_org_runs_elsewhere(
        journal["travail"], since=depuis, errors_only=True, error_contains="copie-eval")
    assert n == 2, "les deux refus de ce run résolus sous l'org maison"
    assert db.count_calls_of_org_runs_elsewhere(
        journal["travail"], since=depuis, tool_name="data_rows") == 1
    assert db.count_calls_of_org_runs_elsewhere(
        journal["travail"], since=depuis, errors_only=False) == 3
    assert db.count_calls_of_org_runs_elsewhere(
        journal["travail"], since=depuis, run_id=journal["run"], errors_only=True) == 2
    assert db.count_calls_of_org_runs_elsewhere(
        journal["travail"], since=depuis, sub="quelqu-un-d-autre") == 0
    assert db.count_calls_of_org_runs_elsewhere(
        journal["travail"], since=datetime.now(timezone.utc) + timedelta(minutes=1)) == 0
    # L'org maison, elle, n'a aucun run : rien n'est « dehors » de son point de vue.
    assert db.count_calls_of_org_runs_elsewhere(journal["maison"], since=depuis) == 0


def test_la_page_et_le_plancher_lisent_les_memes_clauses(journal):
    """La page (`list_tool_calls`) et le plancher partagent leur construction de
    filtres : un filtre honoré d'un côté l'est de l'autre, par construction."""
    from oto_mcp import db
    clauses, params = db.call_filter_clauses(
        sub="s", tool_name="t", errors_only=True, since_days=3, run_id="r",
        session_id="x", min_duration_ms=10, error_contains="e")
    assert len(clauses) == 8 and len(params) == 7      # `l.ok = FALSE` n'a pas de paramètre
    assert all(c.startswith("l.") for c in clauses), clauses
