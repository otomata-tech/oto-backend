"""L'issue que l'agent conclut de son travail (`db/issues_des_travaux.py`), en base.

Ce qu'une doublure ne peut pas prouver : un `done` que l'agent a conclu `blocked` est un
travail MORT pour les trois relevés (épisodes, guérison, alerte au titulaire), avec sa
note comme motif ; la plus grave des issues déclarées sous un travail gagne ; un jeton
d'autrui n'écrit rien ; et la liste des travaux sert l'issue et la note. Cas réel du
09/11/2026 : un agent arrêté sur un outil absent, trois jours « verts ».

Tout cela sous le réglage d'instance `OTO_RUNS_BLOQUES_COMPTES` : allumé par défaut dans
ce fichier (`allume`), et les cas éteints prouvent que rien ne change alors par rapport à
avant — un travail n'est mort que `failed`, rien n'est écrit ni servi.
"""
from __future__ import annotations

import asyncio
import json

import pytest

ORG = 8420


def _travail(tid, status, il_y_a_h, erreur=None, issue=None, note=None, sub="jane"):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute(
            "INSERT INTO runner_jobs (org_id, kind, payload, status, last_error, sub, "
            "finished_at, run_outcome, run_note) VALUES (%s, 'start', %s::jsonb, %s, %s, "
            "%s, NOW() - make_interval(hours => %s), %s, %s) RETURNING id",
            (ORG, json.dumps({"trigger_id": tid}), status, erreur, sub, il_y_a_h, issue,
             note)).fetchone()["id"]


def _issue(job_id):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute("SELECT run_outcome, run_note FROM runner_jobs WHERE id = %s",
                         (job_id,)).fetchone()
    return (r["run_outcome"], r["run_note"])


@pytest.fixture(autouse=True)
def allume(monkeypatch):
    monkeypatch.setenv("OTO_RUNS_BLOQUES_COMPTES", "1")
    return monkeypatch


@pytest.fixture()
def agent(live):
    from oto_mcp.db import runner_triggers as rt
    t = rt.create_trigger(ORG, "jane", procedure="veille", tz="UTC", tools=["a"],
                          cron="0 6 * * *", next_due="2999-01-01 00:00:00+00",
                          label="Nouveaux comptes")
    return t["id"]


def test_la_plus_grave_des_issues_gagne_avec_sa_note(agent):
    from oto_mcp.db.issues_des_travaux import noter_issue
    j = _travail(agent, "claimed", 0)
    assert noter_issue(j, "jane", "blocked", "serper_search absent")
    assert noter_issue(j, "jane", "done", "enveloppe close")
    assert _issue(j) == ("blocked", "serper_search absent")
    assert noter_issue(j, "jane", "failed", "plus grave")
    assert _issue(j) == ("failed", "plus grave")


def test_un_jeton_d_autrui_n_ecrit_rien(agent):
    from oto_mcp.db.issues_des_travaux import noter_issue
    j = _travail(agent, "claimed", 0)
    assert not noter_issue(j, "mallory", "blocked", "x")
    assert _issue(j) == (None, None)


def test_une_issue_inconnue_n_est_pas_posee(agent):
    from oto_mcp.db.issues_des_travaux import noter_issue
    j = _travail(agent, "claimed", 0)
    assert not noter_issue(j, "jane", "peut-etre", "x")


def test_un_done_bloque_ouvre_la_panne_avec_la_note_pour_motif(agent):
    from oto_mcp.db import episodes_agents
    _travail(agent, "done", 30)
    dernier = _travail(agent, "done", 2, issue="blocked",
                       note="Étape 2 : serper_search absent (tool_not_in_job)")
    p = {x["trigger_id"]: x for x in episodes_agents.pannes_en_cours()}[agent]
    assert (p["travaux"], p["job_id"]) == (1, dernier)
    assert "serper_search" in p["motif"]


def test_un_done_partiel_est_un_succes_et_guerit(agent):
    from oto_mcp.db import episodes_agents
    j = _travail(agent, "done", 10, issue="blocked", note="clé retirée")
    episodes_agents.ouvrir_ou_rafraichir(org_id=ORG, trigger_id=agent, classe="inconnu",
                                         cote="inconnu", motif="clé retirée", job_id=j,
                                         travaux=1)
    _travail(agent, "done", -1, issue="partial", note="fait à moitié")
    assert agent not in {x["trigger_id"] for x in episodes_agents.pannes_en_cours()}
    clos = {e["trigger_id"]: e for e in episodes_agents.clore_les_gueris()}
    assert clos[agent]["resolution"] == "gueri"


def test_un_bloque_ne_guerit_pas_l_episode(agent):
    from oto_mcp.db import episodes_agents
    j = _travail(agent, "failed", 10, erreur="HTTP 401")
    episodes_agents.ouvrir_ou_rafraichir(org_id=ORG, trigger_id=agent, classe="cle_refusee",
                                         cote="proprietaire", motif="HTTP 401", job_id=j,
                                         travaux=1)
    _travail(agent, "done", -1, issue="blocked", note="toujours la clé")
    assert agent not in {e["trigger_id"] for e in episodes_agents.clore_les_gueris()}


def test_l_alerte_au_titulaire_compte_les_bloques(agent):
    from oto_mcp.db import alertes_agents
    for h in (30, 20, 10):
        _travail(agent, "done", h, issue="blocked", note=f"lemlist 401 ({h})")
    b = {x["trigger_id"]: x for x in alertes_agents.agents_bloques(3)}[agent]
    assert (b["travaux"], b["motif"]) == (3, "lemlist 401 (10)")


def test_la_liste_des_travaux_sert_l_issue_et_la_note(agent):
    from oto_mcp.db import runner_jobs
    j = _travail(agent, "done", 1, issue="blocked", note="canal archivé")
    rows = {r["id"]: r for r in runner_jobs.list_jobs(ORG, trigger_id=agent)}
    assert (rows[j]["run_outcome"], rows[j]["run_note"]) == ("blocked", "canal archivé")
    assert runner_jobs.get_job(j, ORG)["run_outcome"] == "blocked"


def test_run_finish_pose_l_issue_sur_le_travail_du_jeton(agent):
    from oto_mcp import verrou_org
    from oto_mcp.tools import guide_run
    j = _travail(agent, "claimed", 0)
    jeton = verrou_org.poser(verrou_org.Verrou(sub="jane", org_id=ORG, job_id=j))
    try:
        asyncio.run(guide_run._noter_sur_le_travail("blocked", "canal archivé"))
    finally:
        verrou_org.lever(jeton)
    assert _issue(j) == ("blocked", "canal archivé")


def test_run_finish_hors_travail_n_ecrit_rien(agent):
    from oto_mcp.tools import guide_run
    j = _travail(agent, "claimed", 0)
    asyncio.run(guide_run._noter_sur_le_travail("blocked", "x"))
    assert _issue(j) == (None, None)


# --- Réglage éteint : le comportement d'avant ------------------------------------------

@pytest.mark.parametrize("valeur", ["", "0", "no", "off", "false"])
def test_eteint_un_done_bloque_n_est_pas_mort(agent, allume, valeur):
    from oto_mcp.db import alertes_agents, episodes_agents
    allume.setenv("OTO_RUNS_BLOQUES_COMPTES", valeur)
    for h in (30, 20, 10):
        _travail(agent, "done", h, issue="blocked", note="outil absent")
    assert agent not in {x["trigger_id"] for x in episodes_agents.pannes_en_cours()}
    assert agent not in {x["trigger_id"] for x in alertes_agents.agents_bloques(3)}


def test_absent_vaut_eteint(agent, allume):
    from oto_mcp.db import episodes_agents
    allume.delenv("OTO_RUNS_BLOQUES_COMPTES")
    _travail(agent, "done", 2, issue="blocked", note="outil absent")
    assert agent not in {x["trigger_id"] for x in episodes_agents.pannes_en_cours()}


def test_eteint_le_motif_est_l_erreur_du_runner(agent, allume):
    from oto_mcp.db import episodes_agents
    allume.setenv("OTO_RUNS_BLOQUES_COMPTES", "0")
    _travail(agent, "failed", 2, erreur="", note="la note de l'agent")
    p = {x["trigger_id"]: x for x in episodes_agents.pannes_en_cours()}[agent]
    assert p["motif"] == ""


def test_eteint_un_bloque_guerit_comme_avant(agent, allume):
    from oto_mcp.db import episodes_agents
    allume.setenv("OTO_RUNS_BLOQUES_COMPTES", "0")
    j = _travail(agent, "failed", 10, erreur="HTTP 401")
    episodes_agents.ouvrir_ou_rafraichir(org_id=ORG, trigger_id=agent, classe="cle_refusee",
                                         cote="proprietaire", motif="HTTP 401", job_id=j,
                                         travaux=1)
    _travail(agent, "done", -1, issue="blocked", note="toujours la clé")
    assert agent in {e["trigger_id"] for e in episodes_agents.clore_les_gueris()}


def test_eteint_la_liste_ne_sert_pas_l_issue(agent, allume):
    from oto_mcp.db import runner_jobs
    allume.setenv("OTO_RUNS_BLOQUES_COMPTES", "0")
    j = _travail(agent, "done", 1, issue="blocked", note="canal archivé")
    rows = {r["id"]: r for r in runner_jobs.list_jobs(ORG, trigger_id=agent)}
    assert "run_outcome" not in rows[j] and "run_note" not in rows[j]
    assert "run_outcome" not in runner_jobs.get_job(j, ORG)


def test_eteint_run_finish_n_ecrit_rien(agent, allume):
    from oto_mcp import verrou_org
    from oto_mcp.tools import guide_run
    allume.setenv("OTO_RUNS_BLOQUES_COMPTES", "0")
    j = _travail(agent, "claimed", 0)
    jeton = verrou_org.poser(verrou_org.Verrou(sub="jane", org_id=ORG, job_id=j))
    try:
        asyncio.run(guide_run._noter_sur_le_travail("blocked", "canal archivé"))
    finally:
        verrou_org.lever(jeton)
    assert _issue(j) == (None, None)


def test_une_valeur_illisible_leve(allume):
    from oto_mcp import interrupteurs
    from oto_mcp.db import issues_des_travaux
    allume.setenv("OTO_RUNS_BLOQUES_COMPTES", "peut-etre")
    with pytest.raises(interrupteurs.InterrupteurIllisible):
        issues_des_travaux.mort()
