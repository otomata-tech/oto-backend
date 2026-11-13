"""Les ÉPISODES de panne d'un agent, en base (`db/episodes_agents.py`).

Ce qu'une doublure ne peut pas prouver : le relevé dès le PREMIER travail mort depuis le
dernier succès ; un seul épisode vivant par agent (l'index unique partiel, rafraîchi au
lieu d'être doublé) ; une classe posée à la main qui survit aux passages ; la garde d'une
réparation (plafond par action, un geste par travail mort même sous deux processus, rien
sur un épisode clos) ; la guérison au succès suivant ; et le silence de l'alerte borné au
côté plateforme ET aux épisodes que la boucle voit encore. Joué sur une vraie base.
"""
from __future__ import annotations

import json
import threading

import pytest

from oto_mcp.medecin_agents import FRAICHEUR

ORG = 8410


def _travail(tid, status, il_y_a_h, erreur=None, org=ORG):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute(
            "INSERT INTO runner_jobs (org_id, kind, payload, status, last_error, sub, "
            "finished_at) VALUES (%s, 'start', %s::jsonb, %s, %s, 'jane', "
            "NOW() - make_interval(hours => %s)) RETURNING id",
            (org, json.dumps({"trigger_id": tid}), status, erreur, il_y_a_h)
        ).fetchone()["id"]


def _pannes():
    from oto_mcp.db import episodes_agents
    return {p["trigger_id"]: p for p in episodes_agents.pannes_en_cours()}


@pytest.fixture()
def agent(live):
    from oto_mcp.db import runner_triggers as rt
    t = rt.create_trigger(ORG, "jane", procedure="veille", tz="UTC", tools=["a"],
                          cron="0 6 * * *", next_due="2999-01-01 00:00:00+00",
                          label="Veille des leads")
    return t["id"]


def _ouvrir(agent, job_id, classe="refus_passager", cote="plateforme"):
    from oto_mcp.db import episodes_agents
    return episodes_agents.ouvrir_ou_rafraichir(
        org_id=ORG, trigger_id=agent, classe=classe, cote=cote, motif="HTTP 529",
        job_id=job_id, travaux=1)


def test_un_seul_travail_mort_suffit_et_le_dernier_est_rendu(agent):
    _travail(agent, "done", 30)
    _travail(agent, "failed", 20, "ancien")
    dernier = _travail(agent, "failed", 10, "HTTP 529 overloaded")
    p = _pannes()[agent]
    assert (p["travaux"], p["job_id"], p["motif"], p["sub"]) == (
        2, dernier, "HTTP 529 overloaded", "jane")
    assert p["payload"]["trigger_id"] == agent


def test_un_agent_eteint_ou_gueri_n_est_pas_en_panne(agent):
    from oto_mcp.db import runner_triggers as rt
    _travail(agent, "failed", 20)
    _travail(agent, "done", 10)
    assert agent not in _pannes()
    _travail(agent, "failed", 5)
    rt.update_trigger(agent, ORG, {"enabled": False})
    assert agent not in _pannes()


def test_un_seul_episode_vivant_par_agent_rafraichi(agent):
    a = _ouvrir(agent, 1)
    b = _ouvrir(agent, 2, classe="famille_non_servie")
    assert a["id"] == b["id"]
    assert (b["dernier_job_id"], b["classe"], b["statut"]) == (2, "famille_non_servie",
                                                               "ouvert")


def test_une_classe_posee_a_la_main_survit_aux_passages(agent):
    """`diagnose` (agent médecin ou opérateur) corrige la classe : le passage suivant de
    la boucle, qui reclasse par regex, ne la défait pas — mais rafraîchit le reste."""
    from oto_mcp.db import episodes_agents
    ep = _ouvrir(agent, 1, classe="inconnu", cote="inconnu")
    assert ep["classe_manuelle"] is False
    pose = episodes_agents.poser(ep["id"], classe="cle_refusee", cote="proprietaire",
                                 diagnostic="clé révoquée chez le fournisseur")
    assert pose["classe_manuelle"] is True
    apres = _ouvrir(agent, 2, classe="inconnu", cote="inconnu")
    assert (apres["id"], apres["classe"], apres["cote"], apres["dernier_job_id"]) == (
        ep["id"], "cle_refusee", "proprietaire", 2)
    encore = _ouvrir(agent, 3, classe="refus_passager", cote="plateforme")
    assert (encore["classe"], encore["cote"], encore["classe_manuelle"]) == (
        "cle_refusee", "proprietaire", True), "la marque reste, passage après passage"


def test_un_diagnostic_seul_laisse_la_boucle_reclasser(agent):
    from oto_mcp.db import episodes_agents
    ep = _ouvrir(agent, 1, classe="inconnu", cote="inconnu")
    assert episodes_agents.poser(ep["id"], diagnostic="à suivre")["classe_manuelle"] is False
    assert _ouvrir(agent, 2)["classe"] == "refus_passager"


def test_un_travail_mort_ne_recoit_qu_une_relance(agent):
    from oto_mcp.db import episodes_agents
    ep = _ouvrir(agent, 1)
    assert episodes_agents.noter_reparation(ep["id"], action="relance", acteur="medecin",
                                            job_id=7, maximum=2) is not None
    assert episodes_agents.noter_reparation(ep["id"], action="relance", acteur="x",
                                            job_id=7, maximum=2) is None, "même travail"
    assert episodes_agents.noter_reparation(ep["id"], action="note", acteur="x",
                                            job_id=7) is not None, "autre action, permise"


def test_deux_processus_sur_le_meme_travail_un_seul_relance(agent):
    """La garde tient sous concurrence réelle : l'UPDATE qui perd attend le verrou de
    ligne, relit `reparations` écrit par le gagnant, et refuse. Plafond large : seule la
    garde par `job_id` peut départager."""
    from oto_mcp.db import episodes_agents
    ep = _ouvrir(agent, 1)
    passes, depart = [], threading.Barrier(4)

    def _un(i):
        depart.wait()
        if episodes_agents.noter_reparation(ep["id"], action="relance", acteur=f"p{i}",
                                            job_id=900, maximum=50) is not None:
            passes.append(i)

    fils = [threading.Thread(target=_un, args=(i,)) for i in range(4)]
    [f.start() for f in fils]
    [f.join() for f in fils]
    assert len(passes) == 1, f"{len(passes)} relances du même travail : {passes}"
    gestes = episodes_agents.lire(ep["id"])["reparations"]
    assert [(g["action"], g["job_id"]) for g in gestes] == [("relance", 900)]


def test_la_garde_d_une_reparation(agent):
    from oto_mcp.db import episodes_agents
    ep = _ouvrir(agent, 1)
    for job in (1, 2):
        r = episodes_agents.noter_reparation(ep["id"], action="relance", acteur="medecin",
                                             job_id=job, maximum=2)
        assert r is not None and r["statut"] == "en_soin"
    assert episodes_agents.noter_reparation(ep["id"], action="relance", acteur="medecin",
                                            job_id=3, maximum=2) is None, "plafond"
    gestes = episodes_agents.lire(ep["id"])["reparations"]
    assert [(g["action"], g["job_id"]) for g in gestes] == [("relance", 1), ("relance", 2)]
    assert all(g["at"] for g in gestes)
    episodes_agents.poser(ep["id"], statut="clos")
    assert episodes_agents.noter_reparation(ep["id"], action="note", acteur="x") is None


def test_le_succes_suivant_clot_et_un_nouvel_episode_peut_s_ouvrir(agent):
    from oto_mcp.db import episodes_agents
    from oto_mcp.db._conn import _connect
    ep = _ouvrir(agent, 1)
    with _connect() as conn:
        conn.execute("UPDATE agent_episodes SET ouvert_at = NOW() - interval '2 hours' "
                     "WHERE id = %s", (ep["id"],))
    assert [e["id"] for e in episodes_agents.clore_les_gueris()] == []
    _travail(agent, "done", 1)
    clos = episodes_agents.clore_les_gueris()
    assert [(e["id"], e["resolution"]) for e in clos] == [(ep["id"], "gueri")]
    assert _ouvrir(agent, 9)["id"] != ep["id"]


def test_un_agent_eteint_clot_son_episode(agent):
    from oto_mcp.db import episodes_agents
    from oto_mcp.db import runner_triggers as rt
    ep = _ouvrir(agent, 1)
    rt.update_trigger(agent, ORG, {"enabled": False})
    assert [(e["id"], e["resolution"]) for e in episodes_agents.clore_les_gueris()] == [
        (ep["id"], "agent_eteint")]


def test_l_alerte_ne_se_tait_que_pour_le_cote_plateforme(agent, live):
    from oto_mcp.db import episodes_agents
    from oto_mcp.db import runner_triggers as rt
    autre = rt.create_trigger(ORG, "jane", procedure="veille", tz="UTC", tools=["a"],
                              cron="0 6 * * *", next_due="2999-01-01 00:00:00+00")["id"]
    ep = _ouvrir(agent, 1)
    _ouvrir(autre, 2, classe="cle_refusee", cote="proprietaire")
    assert episodes_agents.agents_soignes_par_nous(
        [agent, autre], vus_depuis=FRAICHEUR) == {agent}
    episodes_agents.poser(ep["id"], statut="escalade")
    assert episodes_agents.agents_soignes_par_nous([agent], vus_depuis=FRAICHEUR) == \
        {agent}, "escaladé chez nous : toujours pas au titulaire"
    episodes_agents.poser(ep["id"], statut="clos")
    assert episodes_agents.agents_soignes_par_nous([agent], vus_depuis=FRAICHEUR) == set()


def test_une_boucle_eteinte_ne_fait_plus_taire_l_alerte(agent):
    """La boucle a tourné, ouvert un épisode plateforme, puis s'est arrêtée : plus
    personne ne le rafraîchit. Passé `FRAICHEUR`, l'alerte au titulaire reprend."""
    from oto_mcp.db import episodes_agents
    from oto_mcp.db._conn import _connect
    ep = _ouvrir(agent, 1)
    assert episodes_agents.agents_soignes_par_nous([agent], vus_depuis=FRAICHEUR) == {agent}
    with _connect() as conn:
        conn.execute("UPDATE agent_episodes SET vu_at = NOW() - %s - interval '1 minute' "
                     "WHERE id = %s", (FRAICHEUR, ep["id"]))
    assert episodes_agents.agents_soignes_par_nous([agent], vus_depuis=FRAICHEUR) == set()
    # La boucle repasse : l'épisode est de nouveau tenu.
    _ouvrir(agent, 2)
    assert episodes_agents.agents_soignes_par_nous([agent], vus_depuis=FRAICHEUR) == {agent}


# --- Le catalogue des causes (`db/causes_agents.py`), sur une vraie base. --------------

def test_l_empreinte_suit_le_motif_de_l_episode(agent):
    from oto_mcp.db import episodes_agents
    j = _travail(agent, "failed", 1, "HTTP 529")
    ep = episodes_agents.ouvrir_ou_rafraichir(
        org_id=ORG, trigger_id=agent, classe="refus_passager", cote="plateforme",
        motif="HTTP 529", job_id=j, travaux=1, empreinte="aaaa")
    assert ep["empreinte"] == "aaaa"
    ep2 = episodes_agents.ouvrir_ou_rafraichir(
        org_id=ORG, trigger_id=agent, classe="cle_refusee", cote="proprietaire",
        motif="HTTP 401", job_id=j, travaux=2, empreinte="bbbb")
    assert (ep2["id"], ep2["empreinte"]) == (ep["id"], "bbbb")


def test_une_empreinte_s_insere_une_fois_et_ne_se_reecrit_pas(live):
    from oto_mcp.db import causes_agents
    un = causes_agents.relever([("c1", "http <n>", "refus_passager", "plateforme")])
    assert un["c1"]["neuve"] and un["c1"]["statut"] == "proposee"
    causes_agents.decider("c1", acteur="ops", statut="approuvee", classe="surcharge")
    deux = causes_agents.relever([("c1", "http <n>", "inconnu", "inconnu"),
                                  ("c2", "autre", "inconnu", "inconnu")])
    assert not deux["c1"]["neuve"] and deux["c2"]["neuve"]
    assert (deux["c1"]["statut"], deux["c1"]["classe"]) == ("approuvee", "surcharge")
    assert causes_agents.relever([]) == {}


def test_une_cause_dit_ce_qu_elle_couvre_et_qui_l_a_decidee(agent):
    from oto_mcp.db import causes_agents, episodes_agents
    causes_agents.relever([("c9", "http <n>", "refus_passager", "plateforme")])
    j = _travail(agent, "failed", 1, "HTTP 529")
    episodes_agents.ouvrir_ou_rafraichir(
        org_id=ORG, trigger_id=agent, classe="refus_passager", cote="plateforme",
        motif="HTTP 529", job_id=j, travaux=1, empreinte="c9")
    c = causes_agents.decider("c9", acteur="ops", statut="approuvee",
                              copie={"fr": {"headline": "Panne passagère."}})
    assert c["copie"] == {"fr": {"headline": "Panne passagère."}}
    assert [(g["acteur"], g["statut"]) for g in c["gestes"]] == [("ops", "approuvee")]
    ligne = {x["cle"]: x for x in causes_agents.lister()}["c9"]
    assert (ligne["episodes"], ligne["vivants"], ligne["orgs"]) == (1, 1, 1)
    assert ligne["classes_vues"] == ["refus_passager"]
    assert [x["cle"] for x in causes_agents.lister(statut="refusee")] == []
    assert causes_agents.decider("absente", acteur="ops", statut="refusee") is None
