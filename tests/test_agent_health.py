"""L'onglet Santé d'une org (`capabilities/agent_health.py`), sur une vraie base.

Ce qu'on prouve : un problème par cause qui attend le client, avec les agents qu'il
arrête ; une panne de notre côté ne se dit qu'après six heures (D3), à part, sans geste,
et son agent compte d'ici là comme fonctionnant ;
un membre ne voit que les agents qu'il voit ; ce que Tulina a réparé se lit avec sa
phrase ; « laisser Tulina réparer mes agents » naît allumé, s'éteint, et la boucle comme
l'agent médecin s'y tiennent ; un ajout d'outil du médecin se défait par qui peut modifier
l'agent, et le médecin ne le redonne plus.
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
        "Nous avons ajouté la recherche web.", False), "un geste sans outil ne se défait pas"
    assert fixed["Relances"]["undo"] is False, "une relance ne se défait pas"
    assert fixed["Relances"]["undone"] is False


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


# ── Défaire la réparation du médecin (`POST …/agent-health/undo`) ───────────────

def _repare(agent, outil="serper_search"):
    """Un épisode guéri après un ajout d'outil du médecin, comme `add_tool` l'écrit."""
    from oto_mcp.db import episodes_agents, runner_triggers as rt
    ep = _panne(agent, cote="inconnu", classe="inconnu")
    episodes_agents.noter_reparation(ep["id"], action="ajout_outil", acteur="medecin",
                                     detail=f"+ {outil}", outil=outil, maximum=5)
    t = rt.get_trigger(agent, ORG)
    rt.update_trigger(agent, ORG, {"tools": list(t["tools"]) + [outil]}, par="medecin")
    rt.noter_reparation_pour_le_client(agent, ORG, "Nous avons ajouté la recherche web.")
    episodes_agents.poser(int(ep["id"]), statut="clos", resolution="gueri")
    return ep


def _defaire(episode_id, sub="jane"):
    return H._defaire(ResolvedCtx(sub=sub, org_id=ORG),
                      H.AgentHealthUndoInput(org_id=ORG, episode_id=episode_id))


def test_defaire_retire_l_outil_et_le_medecin_ne_le_redonne_plus(live, monkeypatch):
    from oto_mcp.db import episodes_agents, runner_triggers as rt
    a = _agent("Veille")
    ep = _repare(a)
    (f,) = _lire()["fixed"]
    assert (f["episode_id"], f["undo"], f["undone"]) == (ep["id"], True, False)

    out = _defaire(ep["id"])
    t = rt.get_trigger(a, ORG)
    assert t["tools"] == ["a"], "l'outil ajouté sort des outils de l'agent"
    assert t["repair"] is None, "l'agent ne se dit plus « réparé par Tulina »"
    assert rt.outils_annules(a, ORG) == {"serper_search"}, "la marque durable"
    (f,) = out["fixed"]
    assert (f["undo"], f["undone"]) == (False, True)
    assert "annulé" in f["what"]
    lu = episodes_agents.lire(ep["id"])
    assert lu["statut"] == "clos", "défaire ne rouvre pas l'épisode"
    assert lu["reparations"][-1]["action"] == "annulation"
    assert lu["reparations"][-1]["outils"] == ["serper_search"]

    with pytest.raises(AuthzDenied) as e:
        _defaire(ep["id"])
    assert (e.value.status, e.value.code) == (404, "repair_not_undoable"), "une fois"

    # Le médecin, sur une panne suivante du même agent, ne redonne pas cet outil.
    from oto_mcp import tool_registry
    from oto_mcp.capabilities import agent_repair as AR, runner_triggers as RT
    monkeypatch.setattr(RT, "_outils_non_accordes",
                        lambda org, t: [("serper_search", "procedure")])
    monkeypatch.setitem(tool_registry._EFFETS, "serper_search", "read")
    suivant = _panne(a, cote="inconnu", classe="inconnu")
    get = AR._agent_repair(ResolvedCtx(sub="medecin"),
                           AR.AgentRepairInput(op="get", episode_id=suivant["id"]))
    assert get["missing_tools"][0]["undone"] is True
    with pytest.raises(AuthzDenied) as e:
        AR._agent_repair(ResolvedCtx(sub="medecin"), AR.AgentRepairInput(
            op="add_tool", episode_id=suivant["id"], tool="serper_search",
            client_note="Nous avons ajouté la recherche web."))
    assert (e.value.status, e.value.code) == (409, "repair_undone")
    assert rt.get_trigger(a, ORG)["tools"] == ["a"]


def test_un_outil_deja_retire_a_la_main_se_defait_quand_meme(live):
    from oto_mcp.db import runner_triggers as rt
    a = _agent("Veille")
    ep = _repare(a)
    rt.update_trigger(a, ORG, {"tools": ["a"]}, par="jane")
    _defaire(ep["id"])
    assert rt.outils_annules(a, ORG) == {"serper_search"}


def test_sans_droit_de_modifier_on_ne_defait_pas(live):
    from oto_mcp import ownership
    from oto_mcp.db import runner_triggers as rt
    lu, cache = _agent("Partagé", sub="bob"), _agent("Caché", sub="bob")
    ep_lu, ep_cache = _repare(lu), _repare(cache)
    ownership.grant("runner_trigger", str(lu), "user", "jane", "read", granted_by="bob")
    (f,) = _lire()["fixed"]
    assert (f["agent"]["id"], f["undo"]) == (lu, False), "un lecteur voit, sans défaire"
    with pytest.raises(AuthzDenied) as e:
        _defaire(ep_lu["id"])
    assert (e.value.status, e.value.code) == (403, "trigger_edit_forbidden")
    with pytest.raises(AuthzDenied) as e:
        _defaire(ep_cache["id"])
    assert (e.value.status, e.value.code) == (404, "trigger_not_found")
    assert "serper_search" in rt.get_trigger(lu, ORG)["tools"]
    assert rt.outils_annules(lu, ORG) == set()


def test_un_episode_d_une_autre_org_ou_une_relance_ne_se_defont_pas(live):
    from oto_mcp.db import episodes_agents
    ep = _panne(_agent("Relances"), cote="plateforme", classe="refus_passager")
    episodes_agents.noter_reparation(ep["id"], action="relance", acteur="medecin", job_id=1)
    episodes_agents.poser(int(ep["id"]), statut="clos", resolution="gueri")
    with pytest.raises(AuthzDenied) as e:
        _defaire(ep["id"])
    assert e.value.code == "repair_not_undoable"
    with pytest.raises(AuthzDenied) as e:
        H._defaire(ResolvedCtx(sub="jane", org_id=ORG + 1),
                   H.AgentHealthUndoInput(org_id=ORG + 1, episode_id=ep["id"]))
    assert (e.value.status, e.value.code) == (404, "episode_not_found")


def test_un_geste_d_avant_se_lit_par_son_detail():
    from oto_mcp.db import episodes_agents as E
    reps = [{"action": "relance"}, {"action": "ajout_outil", "detail": "+ serper_search"},
            {"action": "ajout_outil", "outil": "web_read", "detail": "+ web_read"}]
    assert E.outils_ajoutes(reps) == ["serper_search", "web_read"]
    reps.append({"action": "annulation", "outils": ["serper_search"]})
    assert E.outils_a_defaire(reps) == ["web_read"]
