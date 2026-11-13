"""L'onglet Santé d'une org (`capabilities/agent_health.py`), sur une vraie base.

Ce qu'on prouve : un problème par cause qui attend le client, avec les agents qu'il
arrête ; une panne de notre côté ne se dit qu'après six heures (D3), à part, sans geste,
et son agent compte d'ici là comme fonctionnant ;
un membre ne voit que les agents qu'il voit ; ce que Tulina a réparé se lit avec sa
phrase ; « laisser Tulina réparer mes agents » naît allumé, s'éteint, et la boucle comme
l'agent médecin s'y tiennent.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from oto_mcp import alertes_slack_agents as A
from oto_mcp.capabilities import agent_health as H
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

ORG = 8450


@pytest.fixture(autouse=True)
def base(live, monkeypatch):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("DELETE FROM agent_episodes WHERE org_id = %s", (ORG,))
        conn.execute("DELETE FROM agent_health_settings WHERE org_id = %s", (ORG,))
        conn.execute("DELETE FROM runner_triggers WHERE org_id = %s", (ORG,))
    monkeypatch.setenv("OTO_ALERTES_SLACK_AGENTS_ENABLED", "1")
    from oto_mcp import alerte_agents_bloques
    monkeypatch.setattr(alerte_agents_bloques, "org_a_nous", lambda org, qui: org == ORG)
    monkeypatch.setattr(A, "langue", lambda org: "fr")
    monkeypatch.setattr(A, "marque_de_l_org", lambda org: SimpleNamespace(nom="Acme", langue=""))


def _agent(label, sub="jane", enabled=True):
    from oto_mcp.db import runner_triggers as rt
    t = rt.create_trigger(ORG, sub, procedure="p", tz="UTC", tools=["a"], cron="0 6 * * *",
                          next_due="2999-01-01 00:00:00+00", label=label)
    if not enabled:
        from oto_mcp.db._conn import _connect
        with _connect() as conn:
            conn.execute("UPDATE runner_triggers SET enabled = false WHERE id = %s", (t["id"],))
    return t["id"]


def _panne(agent, cote="proprietaire", classe="cle_refusee", empreinte=None):
    from oto_mcp.db import episodes_agents
    return episodes_agents.ouvrir_ou_rafraichir(
        org_id=ORG, trigger_id=agent, classe=classe, cote=cote, motif="HTTP 401",
        job_id=None, travaux=1, empreinte=empreinte)


def _lire(sub="jane"):
    return H._lire(ResolvedCtx(sub=sub), H.AgentHealthInput(org_id=ORG))


def test_un_probleme_par_cause_avec_les_agents_qu_il_arrete(live):
    a, b, c = _agent("Relances"), _agent("Veille"), _agent("Leads")
    _panne(a), _panne(b)
    _panne(c, cote="plateforme", classe="refus_passager")
    out = _lire()
    assert (out["watched"], out["working"]) == (3, 1), "notre panne se tait six heures"
    (pb,) = out["needs_you"]
    assert "a été refusé" in pb["headline"] and pb["action"] == "connectors"
    assert [x["label"] for x in pb["agents"]] == ["Relances", "Veille"]
    assert pb["since"] and out["auto_repair"] is True and out["covered"] is True
    assert out["on_us"] == [], "comme sur la carte de l'agent : rien avant six heures"


def test_un_membre_ne_voit_pas_l_agent_d_un_autre(live):
    mien, sien = _agent("Le mien"), _agent("Le sien", sub="bob")
    _panne(sien)
    out = _lire()
    assert out["watched"] == 1 and out["needs_you"] == []


def test_un_agent_eteint_ne_compte_pas(live):
    _panne(_agent("Éteint", enabled=False))
    assert (_lire()["watched"], _lire()["needs_you"]) == (0, [])


def test_ce_que_tulina_a_repare_se_lit_avec_sa_phrase(live):
    from oto_mcp.db import episodes_agents, runner_triggers as rt
    a, b = _agent("Relances"), _agent("Veille")
    ea, eb = _panne(a, cote="plateforme", classe="refus_passager"), _panne(b, cote="inconnu")
    episodes_agents.noter_reparation(ea["id"], action="relance", acteur="medecin", job_id=1)
    episodes_agents.noter_reparation(eb["id"], action="add_tool", acteur="medecin")
    rt.noter_reparation_pour_le_client(b, ORG, "Nous avons ajouté la recherche web.")
    for e in (ea, eb):
        episodes_agents.poser(int(e["id"]), statut="clos", resolution="gueri")
    fixed = {f["agent"]["label"]: f for f in _lire()["fixed"]}
    assert fixed["Relances"]["what"].startswith("Il s'est arrêté sur une panne passagère")
    assert (fixed["Veille"]["what"], fixed["Veille"]["undo"]) == (
        "Nous avons ajouté la recherche web.", False), "aucune route ne défait encore"


def test_reparer_mes_agents_nait_allume_et_s_eteint(live):
    out = H._poser(ResolvedCtx(sub="jane"), H.AgentHealthSaveInput(org_id=ORG, auto_repair=False))
    assert out["auto_repair"] is False
    from oto_mcp.db import sante_agents
    assert sante_agents.orgs_sans_auto_repair([ORG, ORG + 1]) == {ORG}


def test_eteint_l_agent_medecin_ne_relance_ni_ne_repare(live, monkeypatch):
    from oto_mcp.capabilities import _outils_plateforme, agent_repair as AR
    from oto_mcp.db import sante_agents
    ep = _panne(_agent("Relances"), cote="plateforme", classe="refus_passager")
    sante_agents.poser_auto_repair(ORG, False)
    monkeypatch.setattr(_outils_plateforme, "_depuis_un_agent_heberge", lambda: True)
    for op in ("rerun", "add_tool"):
        with pytest.raises(AuthzDenied) as e:
            AR._agent_repair(ResolvedCtx(sub="medecin"),
                             AR.AgentRepairInput(op=op, episode_id=ep["id"], tool="x"))
        assert e.value.code == "auto_repair_off"


def test_une_panne_pas_encore_comprise_est_a_nous(live):
    _panne(_agent("Relances"), cote="inconnu", classe="inconnu")
    out = _lire()
    assert out["needs_you"] == [] and out["working"] == 0
    assert out["on_us"][0]["side"] == "operator"


def test_eteint_la_route_repond_404(live, monkeypatch):
    monkeypatch.delenv("OTO_ALERTES_SLACK_AGENTS_ENABLED")
    with pytest.raises(AuthzDenied) as e:
        _lire()
    assert (e.value.status, e.value.code) == (404, "agent_alerts_off")


def test_une_org_d_un_tenant_tiers_n_est_pas_suivie(live, monkeypatch):
    from oto_mcp import alerte_agents_bloques
    monkeypatch.setattr(alerte_agents_bloques, "org_a_nous", lambda org, qui: False)
    assert _lire()["covered"] is False


def test_une_panne_de_notre_cote_qui_dure_six_heures_se_dit_autrement(live):
    from oto_mcp.db._conn import _connect
    vieux, jeune, inconnu = _agent("Vieux"), _agent("Jeune"), _agent("Inconnu")
    ep = _panne(vieux, cote="plateforme", classe="refus_passager")
    _panne(jeune, cote="plateforme", classe="refus_passager")
    ei = _panne(inconnu, cote="inconnu", classe="inconnu")
    with _connect() as conn:
        conn.execute("UPDATE agent_episodes SET ouvert_at = NOW() - interval '7 hours' "
                     "WHERE id = ANY(%s)", ([ep["id"], ei["id"]],))
    operateur, plateforme = sorted(_lire()["on_us"], key=lambda p: p["side"])
    assert plateforme["lasting"] is True, "une ligne par incident, depuis le plus ancien"
    assert "plus de six heures" in plateforme["headline"]
    assert [a["label"] for a in plateforme["agents"]] == ["Vieux"], "la jeune se tait"
    assert "rien à faire du vôtre" in plateforme["headline"]
    assert operateur["lasting"] is False, "D3 : la plateforme seulement"
    assert "six heures" not in operateur["headline"]
