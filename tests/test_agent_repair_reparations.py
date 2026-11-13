"""La réparation du médecin des agents qui se défait (`oto_admin_agent_repair op=add_tool`),
sur une vraie base.

Ce qu'on prouve : un outil n'est donné que s'il est une LECTURE que la procédure (ou une
recette qu'elle lance) appelle déjà ; chaque geste s'écrit sur l'épisode avant d'être
fait ; la phrase pour le client est exigée et servie sur l'agent (`repair`) ; après une
réparation, la relance est permise même du côté du titulaire ; et la porte n'écrit
aucune procédure (`set_procedure` n'existe pas). `op=get` sert la procédure que suit
l'agent, son corps à qui peut déjà la lire, sinon son slug et sa version seuls.
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities import agent_repair as AR
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

ORG = 8430
#: Un appelant membre de l'org de l'agent ; `medecin`, l'appelant par défaut, ne l'est
#: jamais (la base du banc vit le temps du module).
MEMBRE = "medecin-membre"


@pytest.fixture()
def agent(live, monkeypatch):
    from oto_mcp.db import runner_triggers as rt
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("INSERT INTO orgs (id, name) VALUES (%s, 'Acme test') "
                     "ON CONFLICT DO NOTHING", (ORG,))
    t = rt.create_trigger(ORG, "jane", procedure="nouveaux-comptes", tz="UTC",
                          tools=["oto_recipe"], cron="0 6 * * *",
                          next_due="2999-01-01 00:00:00+00", label="Nouveaux comptes")
    # Ce que la procédure et sa recette appellent, hors des outils de l'agent : une
    # lecture et un envoi.
    monkeypatch.setattr(RT, "_outils_non_accordes", lambda org, t: [
        ("serper_search", "recipe `acme-stack`"), ("slack_post_message", "procedure")])
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


def _appel(sub="medecin", **kw):
    ctx = ResolvedCtx(sub=sub)
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
                 client_note="Nous avons ajouté la recherche web à l'agent.")
    assert out.get("refus") is None
    t = rt.get_trigger(agent, ORG)
    assert t["tools"] == ["oto_recipe", "serper_search"]
    assert t["repair"]["what"] == "Nous avons ajouté la recherche web à l'agent."
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


def test_aucune_procedure_ne_s_ecrit_par_cette_porte(agent):
    import pydantic
    ep = _episode(agent)
    with pytest.raises(pydantic.ValidationError):
        AR.AgentRepairInput(op="set_procedure", episode_id=ep["id"], body_md="x")
    assert "set_procedure" not in AR.AgentRepairInput.model_fields["op"].annotation.__args__


def test_le_repair_n_est_servi_que_sept_jours(agent):
    from oto_mcp.db import runner_triggers as rt
    from oto_mcp.db._conn import _connect
    assert rt.noter_reparation_pour_le_client(agent, ORG, "Nous avons ajouté un outil.")
    assert rt.get_trigger(agent, ORG)["repair"]["what"] == "Nous avons ajouté un outil."
    with _connect() as conn:
        conn.execute("UPDATE runner_triggers SET repair_note = jsonb_set(repair_note, "
                     "'{at}', to_jsonb((NOW() - INTERVAL '8 days')::text)) WHERE id = %s",
                     (agent,))
    assert rt.get_trigger(agent, ORG)["repair"] is None


def test_apres_une_reparation_la_relance_passe_meme_cote_titulaire(agent):
    ep = _episode(agent, cote="proprietaire")
    refus = _appel(op="rerun", episode_id=ep["id"])
    assert refus["relance"] is False and "titulaire" in refus["refus"]
    _appel(op="add_tool", episode_id=ep["id"], tool="serper_search",
           client_note="Nous avons ajouté la recherche web.")
    out = _appel(op="rerun", episode_id=ep["id"])
    assert "titulaire" not in (out.get("refus") or "")



def _suit(agent, slug):
    """Pointe l'agent sur un slug propre au test : la base du banc garde les procédures
    d'un test à l'autre."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("UPDATE runner_triggers SET procedure = %s WHERE id = %s",
                     (slug, agent))
    return slug

def test_get_sert_slug_et_version_sans_le_corps_a_qui_n_est_pas_de_l_org(agent):
    from oto_mcp import org_store
    slug = _suit(agent, "medecin-lit-hors-org")
    v = org_store.set_instruction("org", ORG, slug, "Étape 1 : lire.", set_by="jane")
    out = _appel(op="get", episode_id=_episode(agent)["id"])
    assert out["procedure"] == {"slug": slug, "scope": "org", "version": v}


def test_get_sert_le_corps_borne_a_un_membre_de_l_org(agent, monkeypatch):
    from oto_mcp import org_store
    org_store.add_org_member(ORG, MEMBRE)
    slug = _suit(agent, "medecin-lit-membre")
    org_store.set_instruction("org", ORG, slug, "Étape 1 : lire.", set_by="jane")
    out = _appel(MEMBRE, op="get", episode_id=_episode(agent)["id"])
    assert out["procedure"]["body_md"] == "Étape 1 : lire."
    assert out["procedure"]["body_truncated"] is False
    monkeypatch.setattr(AR, "_CORPS_MAX", 5)
    p = _appel(MEMBRE, op="get", episode_id=_episode(agent)["id"])["procedure"]
    assert (p["body_md"], p["body_chars"], p["body_truncated"]) == ("Étape", 15, True)


def test_get_suit_la_copie_personnelle_du_proprietaire_sans_l_ouvrir(agent):
    from oto_mcp import org_store
    org_store.add_org_member(ORG, MEMBRE)
    slug = _suit(agent, "medecin-lit-perso")
    org_store.set_instruction("org", ORG, slug, "org", set_by="jane")
    v = org_store.set_instruction("user", "jane", slug, "perso", set_by="jane")
    p = _appel(MEMBRE, op="get", episode_id=_episode(agent)["id"])["procedure"]
    assert p == {"slug": slug, "scope": "user", "version": v}


def test_get_dit_une_procedure_introuvable(agent):
    slug = _suit(agent, "medecin-lit-introuvable")
    p = _appel(op="get", episode_id=_episode(agent)["id"])["procedure"]
    assert p == {"slug": slug, "scope": None, "version": None}
