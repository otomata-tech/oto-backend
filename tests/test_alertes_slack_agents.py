"""Les alertes d'agents sur le Slack d'une org (`alertes_slack_agents.py`, 09/11/2026), sur
une vraie base, Slack doublé.

Ce qu'on prouve : le réglage naît allumé à la connexion de Slack, en message privé à qui
l'a connecté, sans défaire celui d'un admin ; un épisode du côté du client part UNE fois,
avec le texte de l'agent médecin et son bouton, dans la langue de l'espace ; sans ce
texte, il attend ; une panne de notre côté ne part jamais ; une réparation et un retour
s'annoncent ; un envoi raté n'est pas marqué ; le courriel se tait pour un épisode annoncé
sur Slack ; et la file des travaux sert `problem` sur le dernier travail mort.
"""
from __future__ import annotations

import json

import pytest

from oto_mcp import alertes_slack_agents as A

ORG = 8440


class _Slack:
    def __init__(self, refuse=False):
        self.posts, self.refuse = [], refuse

    def __call__(self, bot_token):
        slack = self

        class _Client:
            def post_message(self, channel, text, blocks=None):
                if slack.refuse:
                    raise RuntimeError("channel_not_found")
                slack.posts.append({"channel": channel, "text": text, "blocks": blocks})
                return {"ok": True}

            def list_channels(self, types="public_channel"):
                return [{"id": "C1", "name": "tulina-alertes"},
                        {"id": "C2", "name": "vieux", "is_archived": True}]
        return _Client()


@pytest.fixture(autouse=True)
def _propre(live):
    """La base est celle du MODULE : chaque cas part d'une org sans réglage ni épisode."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("DELETE FROM agent_alert_settings WHERE org_id = %s", (ORG,))
        conn.execute("DELETE FROM agent_episodes WHERE org_id = %s", (ORG,))


@pytest.fixture()
def slack(live, monkeypatch):
    from oto.tools.slack import client as slack_client
    from oto_mcp import alerte_agents_bloques
    s = _Slack()
    monkeypatch.setattr(slack_client, "SlackClient", lambda bot_token: s(bot_token))
    monkeypatch.setattr(A, "bot_de_l_org", lambda org: ("xoxb-test", "Klark"))
    monkeypatch.setattr(A, "_langue", lambda org: "fr")
    monkeypatch.setattr(A, "_base", lambda org, sub: "https://app.tulina.ai")
    monkeypatch.setattr(alerte_agents_bloques, "org_a_nous", lambda org, qui: True)
    monkeypatch.setattr(alerte_agents_bloques, "_heures_calmes", lambda m: False)
    return s


@pytest.fixture()
def agent(live):
    from oto_mcp.db import runner_triggers as rt
    t = rt.create_trigger(ORG, "jane", procedure="lemlist-push", tz="UTC", tools=["a"],
                          cron="0 6 * * *", next_due="2999-01-01 00:00:00+00",
                          label="Leads vers lemlist")
    return t["id"]


def _episode(agent, cote="proprietaire", il_y_a_min=5, classe="inconnu"):
    from oto_mcp.db import episodes_agents
    from oto_mcp.db._conn import _connect
    ep = episodes_agents.ouvrir_ou_rafraichir(
        org_id=ORG, trigger_id=agent, classe=classe, cote=cote, motif="lemlist 401",
        job_id=None, travaux=1)
    with _connect() as conn:
        conn.execute("UPDATE agent_episodes SET ouvert_at = NOW() - make_interval(mins => %s) "
                     "WHERE id = %s", (il_y_a_min, ep["id"]))
    return ep


def _dire(ep):
    from oto_mcp.db import alertes_slack
    alertes_slack.poser_message_client(ep["id"], {
        "headline": "Depuis le 7/11, l'agent ne peut plus ajouter de leads dans lemlist.",
        "todo": "Créez une nouvelle clé API dans lemlist et collez-la dans Tulina.",
        "link": f"/org/{ORG}/connectors/lemlist", "action": "connectors"})


def _allumer():
    from oto_mcp.db import alertes_slack
    alertes_slack.activer_a_la_connexion(ORG, dm_user="U0MATHILDE", dm_name="Mathilde")


def test_la_connexion_allume_en_prive_sans_defaire_un_admin(live):
    from oto_mcp.db import alertes_slack
    _allumer()
    r = alertes_slack.reglage(ORG)
    assert (r["enabled"], r["channel_id"], r["dm_user"]) == (True, None, "U0MATHILDE")
    alertes_slack.poser(ORG, enabled=False, channel_id="C1", channel_name="x")
    alertes_slack.activer_a_la_connexion(ORG, dm_user="U0AUTRE", dm_name="Autre")
    r = alertes_slack.reglage(ORG)
    assert (r["enabled"], r["channel_id"], r["dm_user"]) == (False, "C1", "U0MATHILDE")


def test_un_episode_du_client_part_une_fois_avec_son_bouton(slack, agent):
    _allumer()
    ep = _episode(agent)
    _dire(ep)
    assert A.balayer()["envoyes"] == 1
    assert A.balayer()["envoyes"] == 0
    (post,) = slack.posts
    assert post["channel"] == "U0MATHILDE"
    assert "Leads vers lemlist" in post["text"] and "ne fonctionne plus" in post["text"]
    assert "nouvelle clé API" in post["text"]
    boutons = post["blocks"][1]["elements"]
    assert boutons[0]["url"] == f"https://app.tulina.ai/org/{ORG}/connectors/lemlist"
    assert boutons[1]["url"] == f"https://app.tulina.ai/org/{ORG}/agents?agent={agent}"


def test_sans_texte_du_medecin_l_alerte_attend_puis_part_avec_la_classe(slack, agent):
    _allumer()
    ep = _episode(agent, il_y_a_min=5, classe="cle_refusee")
    assert A.balayer()["envoyes"] == 0
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("UPDATE agent_episodes SET ouvert_at = NOW() - INTERVAL '3 hours' "
                     "WHERE id = %s", (ep["id"],))
    assert A.balayer()["envoyes"] == 1
    assert "clé ou un compte" in slack.posts[0]["text"]


def test_sans_lien_le_bouton_ouvre_la_reparation_avec_tulina(slack, agent):
    from oto_mcp.db import alertes_slack
    _allumer()
    ep = _episode(agent)
    alertes_slack.poser_message_client(ep["id"], {"headline": "Il s'est arrêté."})
    A.balayer()
    premier = slack.posts[0]["blocks"][1]["elements"][0]
    assert premier["url"].endswith(f"agents?agent={agent}&fix=1")
    assert premier["text"]["text"] == "Réparer avec Tulina"


def test_une_panne_de_notre_cote_ne_part_jamais(slack, agent):
    _allumer()
    _episode(agent, cote="plateforme", il_y_a_min=600)
    assert A.balayer()["envoyes"] == 0


def test_la_reparation_puis_rien_d_autre(slack, agent):
    from oto_mcp.db import episodes_agents, runner_triggers as rt
    _allumer()
    ep = _episode(agent, cote="inconnu")
    rt.noter_reparation_pour_le_client(agent, ORG, "Tulina a ajouté la recherche web à l'agent.")
    assert A.balayer()["envoyes"] == 1
    assert "réparé par Tulina" in slack.posts[0]["text"]
    episodes_agents.poser(int(ep["id"]), statut="clos", resolution="gueri")
    assert A.balayer()["envoyes"] == 0


def test_le_retour_s_annonce_apres_une_alerte(slack, agent):
    from oto_mcp.db import episodes_agents
    _allumer()
    ep = _episode(agent)
    _dire(ep)
    A.balayer()
    episodes_agents.poser(int(ep["id"]), statut="clos", resolution="gueri")
    assert A.balayer()["envoyes"] == 1
    assert slack.posts[-1]["text"] == "✅ *Leads vers lemlist* refonctionne."


def test_un_envoi_rate_n_est_pas_marque(slack, agent):
    _allumer()
    ep = _episode(agent)
    _dire(ep)
    slack.refuse = True
    assert A.balayer()["rates"] == 1
    slack.refuse = False
    assert A.balayer()["envoyes"] == 1


def test_le_courriel_se_tait_pour_un_episode_annonce_sur_slack(slack, agent):
    from oto_mcp import alerte_agents_bloques
    _allumer()
    ep = _episode(agent)
    assert alerte_agents_bloques._annonces_sur_slack([agent]) == set()
    _dire(ep)
    A.balayer()
    assert alerte_agents_bloques._annonces_sur_slack([agent]) == {agent}


def test_les_routes_lisent_posent_et_testent(slack, live):
    from oto_mcp.capabilities import agent_alerts as R
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    ctx = ResolvedCtx(sub="admin", org_id=ORG)
    _allumer()
    vue = R._lire(ctx, R.AgentAlertsInput(org_id=ORG))
    assert vue == {"enabled": True, "channel": None, "slack_connected": True,
                   "slack_workspace": "Klark", "dm_to": {"name": "Mathilde"}}
    assert R._lister_canaux(ctx, R.AgentAlertsInput(org_id=ORG)) == {
        "channels": [{"id": "C1", "name": "tulina-alertes"}]}
    with pytest.raises(AuthzDenied):
        R._poser(ctx, R.AgentAlertsSaveInput(org_id=ORG, enabled=True, channel_id="C2"))
    vue = R._poser(ctx, R.AgentAlertsSaveInput(org_id=ORG, enabled=True, channel_id="C1"))
    assert vue["channel"] == {"id": "C1", "name": "tulina-alertes"}
    assert R._tester(ctx, R.AgentAlertsInput(org_id=ORG)) == {
        "ok": True, "channel_name": "tulina-alertes"}
    assert slack.posts[-1]["channel"] == "C1"


def test_la_file_sert_le_probleme_sur_le_dernier_travail_mort(slack, agent):
    from oto_mcp.capabilities import runner_jobs as RJ
    from oto_mcp.db._conn import _connect
    from oto_mcp.db import episodes_agents
    with _connect() as conn:
        j = conn.execute(
            "INSERT INTO runner_jobs (org_id, kind, payload, status, sub, finished_at, "
            "run_outcome, run_note) VALUES (%s, 'start', %s::jsonb, 'done', 'jane', NOW(), "
            "'blocked', 'lemlist 401') RETURNING id",
            (ORG, json.dumps({"trigger_id": agent}))).fetchone()["id"]
    ep = episodes_agents.ouvrir_ou_rafraichir(
        org_id=ORG, trigger_id=agent, classe="cle_refusee", cote="proprietaire",
        motif="lemlist 401", job_id=j, travaux=1)
    _dire(ep)
    out = RJ._avec_le_probleme(ORG, [{"id": j, "payload": {"trigger_id": agent}},
                                     {"id": j - 1, "payload": {"trigger_id": agent}}])
    assert out[0]["problem"] == {
        "headline": "Depuis le 7/11, l'agent ne peut plus ajouter de leads dans lemlist.",
        "todo": "Créez une nouvelle clé API dans lemlist et collez-la dans Tulina.",
        "action": "connectors", "link": f"/org/{ORG}/connectors/lemlist", "side": "client"}
    assert "problem" not in out[1]


def test_connecter_slack_pour_l_org_allume_les_alertes(live):
    from oto_mcp.auth import slack as auth_slack
    from oto_mcp.db import alertes_slack
    auth_slack._allumer_les_alertes(ORG, {"authed_user": {"id": "U0JULIEN"}}, "julien")
    r = alertes_slack.reglage(ORG)
    assert (r["enabled"], r["dm_user"], r["channel_id"]) == (True, "U0JULIEN", None)
