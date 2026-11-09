"""Les deux réparations du médecin des agents qui se défont (`oto_admin_agent_repair`,
09/11/2026) : `add_tool` et `set_procedure`, sur une vraie base.

Ce qu'on prouve : un outil n'est donné que s'il est une LECTURE que la procédure (ou une
recette qu'elle lance) appelle déjà ; chaque geste s'écrit sur l'épisode avant d'être
fait ; la phrase pour le client est exigée et servie sur l'agent (`repair`) ; la
procédure réécrite garde sa version précédente ; une lecture périmée est refusée ; et
après une réparation, la relance est permise même du côté du titulaire.
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities import agent_repair as AR
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

ORG = 8430


@pytest.fixture()
def agent(live, monkeypatch):
    from oto_mcp.db import runner_triggers as rt
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("INSERT INTO orgs (id, name) VALUES (%s, 'Klark test') "
                     "ON CONFLICT DO NOTHING", (ORG,))
    t = rt.create_trigger(ORG, "jane", procedure="nouveaux-comptes", tz="UTC",
                          tools=["oto_recipe"], cron="0 6 * * *",
                          next_due="2999-01-01 00:00:00+00", label="Nouveaux comptes")
    # Ce que la procédure et sa recette appellent, hors des outils de l'agent : une
    # lecture (Klark, 09/11) et un envoi.
    monkeypatch.setattr(RT, "_outils_non_accordes", lambda org, t: [
        ("serper_search", "recipe `cs-stack`"), ("slack_post_message", "procedure")])
    # L'effet déclaré de chaque outil — le registre se réchauffe au démarrage du serveur,
    # pas dans un banc.
    from oto_mcp import tool_registry
    monkeypatch.setitem(tool_registry._EFFETS, "serper_search", "read")
    monkeypatch.setitem(tool_registry._EFFETS, "slack_post_message", "act")
    return t["id"]


def _episode(agent, cote="inconnu"):
    from oto_mcp.db import episodes_agents
    return episodes_agents.ouvrir_ou_rafraichir(
        org_id=ORG, trigger_id=agent, classe="inconnu", cote=cote,
        motif="serper_search absent", job_id=None, travaux=1)


def _appel(**kw):
    ctx = ResolvedCtx(sub="medecin")
    return AR._agent_repair(ctx, AR.AgentRepairInput(**kw))


def test_get_sert_les_outils_manquants(agent):
    ep = _episode(agent)
    out = _appel(op="get", episode_id=ep["id"])
    assert {m["tool"]: m["read"] for m in out["missing_tools"]} == {
        "serper_search": True, "slack_post_message": False}


def test_add_tool_donne_une_lecture_appelee_et_le_dit_au_client(agent):
    from oto_mcp.db import runner_triggers as rt
    ep = _episode(agent)
    out = _appel(op="add_tool", episode_id=ep["id"], tool="serper_search",
                 client_note="Tulina a ajouté la recherche web à l'agent.")
    assert out.get("refus") is None
    t = rt.get_trigger(agent, ORG)
    assert t["tools"] == ["oto_recipe", "serper_search"]
    assert t["repair"]["what"] == "Tulina a ajouté la recherche web à l'agent."
    assert [r["action"] for r in out["episode"]["reparations"]] == ["ajout_outil"]


def test_add_tool_refuse_un_outil_qui_envoie(agent):
    from oto_mcp.db import runner_triggers as rt
    ep = _episode(agent)
    out = _appel(op="add_tool", episode_id=ep["id"], tool="slack_post_message",
                 client_note="x")
    assert "lecture" in out["refus"]
    assert rt.get_trigger(agent, ORG)["tools"] == ["oto_recipe"]


def test_add_tool_refuse_un_outil_que_rien_n_appelle(agent):
    ep = _episode(agent)
    out = _appel(op="add_tool", episode_id=ep["id"], tool="apollo_search_people",
                 client_note="x")
    assert "n'est appelé" in out["refus"]


def test_la_phrase_pour_le_client_est_exigee(agent):
    ep = _episode(agent)
    with pytest.raises(AuthzDenied) as e:
        _appel(op="add_tool", episode_id=ep["id"], tool="serper_search")
    assert e.value.code == "client_note_required"


def test_set_procedure_ecrit_une_version_et_garde_la_precedente(agent):
    from oto_mcp.org_store import instructions
    v1 = instructions.set_instruction("org", ORG, "nouveaux-comptes", "DM à D0C7K5E97RN",
                                      set_by="jane")
    ep = _episode(agent, cote="proprietaire")
    out = _appel(op="set_procedure", episode_id=ep["id"], body_md="DM à U0A4GU1LYDA",
                 expected_version=v1, client_note="Tulina a corrigé le destinataire.")
    assert out["procedure"] == {"slug": "nouveaux-comptes", "version": v1 + 1}
    assert instructions.get_instruction("org", ORG, "nouveaux-comptes")["body_md"] == \
        "DM à U0A4GU1LYDA"
    assert instructions.get_instruction("org", ORG, "nouveaux-comptes",
                                        version=v1)["body_md"] == "DM à D0C7K5E97RN"


def test_set_procedure_refuse_une_lecture_perimee(agent):
    from oto_mcp.org_store import instructions
    v1 = instructions.set_instruction("org", ORG, "nouveaux-comptes", "a", set_by="jane")
    instructions.set_instruction("org", ORG, "nouveaux-comptes", "b", set_by="jane")
    ep = _episode(agent)
    out = _appel(op="set_procedure", episode_id=ep["id"], body_md="c",
                 expected_version=v1, client_note="x")
    assert "a changé" in out["refus"]


def test_apres_une_reparation_la_relance_passe_meme_cote_titulaire(agent):
    ep = _episode(agent, cote="proprietaire")
    refus = _appel(op="rerun", episode_id=ep["id"])
    assert refus["relance"] is False and "titulaire" in refus["refus"]
    _appel(op="add_tool", episode_id=ep["id"], tool="serper_search",
           client_note="Tulina a ajouté la recherche web.")
    out = _appel(op="rerun", episode_id=ep["id"])
    assert "titulaire" not in (out.get("refus") or "")
