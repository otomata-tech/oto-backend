"""Les alertes d'agents sur le Slack d'une org (`alertes_slack_agents.py`), sur une vraie
base, Slack doublé.

Ce qu'on prouve, réglage d'instance allumé : le réglage naît allumé à la connexion de
Slack, en message privé à qui l'a connecté, sans défaire celui d'un admin ; un épisode du
côté du client part UNE fois, avec le texte de l'agent médecin et ses boutons, sous la
marque du tenant ; sans ce texte, il attend ; une panne de notre côté ne part jamais ; une
réparation et un retour s'annoncent ; un envoi raté n'est pas marqué ; le courriel se tait
pour un épisode annoncé sur Slack ; la file des travaux sert `problem` sur le dernier
travail mort ; un lien qui ne mène pas au tableau de bord de l'instance est refusé ou omis.

Et, réglage éteint (absent, vide, non) : rien de tout cela — routes en 404, aucune
écriture à la connexion de Slack, courriel inchangé, file inchangée, `client_*` refusés,
boucle qui refuse de passer. Une valeur illisible lève.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from oto_mcp import alertes_slack_agents as A

ORG = 8440
BASE = "https://app.example.test"
CHEMINS = json.dumps({"agent": "/org/{org}/agents?agent={id}",
                      "agent_repair": "/org/{org}/agents?agent={id}&fix=1"})


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
                return [{"id": "C1", "name": "acme-alertes"},
                        {"id": "C2", "name": "vieux", "is_archived": True}]
        return _Client()


@pytest.fixture(autouse=True)
def allume(live, monkeypatch):
    """La base est celle du MODULE : chaque cas part d'une org sans réglage ni épisode.
    Le réglage d'instance est allumé ; les cas éteints le retirent."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("DELETE FROM agent_alert_settings WHERE org_id = %s", (ORG,))
        conn.execute("DELETE FROM agent_episodes WHERE org_id = %s", (ORG,))
        conn.execute("DELETE FROM agent_causes WHERE cle LIKE 'test-%%'")
    monkeypatch.setenv("OTO_ALERTES_SLACK_AGENTS_ENABLED", "1")
    monkeypatch.setenv("OTO_APP_URL", BASE)
    monkeypatch.setenv("OTO_DASHBOARD_LINK_PATHS", CHEMINS)
    return monkeypatch


@pytest.fixture()
def slack(live, monkeypatch):
    from oto.tools.slack import client as slack_client
    from oto_mcp import alerte_agents_bloques
    s = _Slack()
    monkeypatch.setattr(slack_client, "SlackClient", lambda bot_token: s(bot_token))
    monkeypatch.setattr(A, "bot_de_l_org", lambda org: ("xoxb-test", "Acme"))
    monkeypatch.setattr(A, "langue", lambda org: "fr")
    monkeypatch.setattr(A, "marque_de_l_org",
                        lambda org: SimpleNamespace(nom="Acme", langue=""))
    monkeypatch.setattr(alerte_agents_bloques, "org_a_nous", lambda org, qui: True)
    monkeypatch.setattr(alerte_agents_bloques, "_heures_calmes", lambda m: False)
    return s


@pytest.fixture()
def agent(live):
    from oto_mcp.db import runner_triggers as rt
    t = rt.create_trigger(ORG, "jane", procedure="leads-push", tz="UTC", tools=["a"],
                          cron="0 6 * * *", next_due="2999-01-01 00:00:00+00",
                          label="Leads vers l'emailing")
    return t["id"]


def _episode(agent, cote="proprietaire", il_y_a_min=5, classe="inconnu", job_id=None,
             empreinte=None):
    from oto_mcp.db import episodes_agents
    from oto_mcp.db._conn import _connect
    ep = episodes_agents.ouvrir_ou_rafraichir(
        org_id=ORG, trigger_id=agent, classe=classe, cote=cote, motif="HTTP 401",
        job_id=job_id, travaux=1, empreinte=empreinte)
    with _connect() as conn:
        conn.execute("UPDATE agent_episodes SET ouvert_at = NOW() - make_interval(mins => %s) "
                     "WHERE id = %s", (il_y_a_min, ep["id"]))
    return ep


_MESSAGE = {
    "headline": "Depuis le 7/11, l'agent ne peut plus ajouter de leads.",
    "todo": "Créez une nouvelle clé API et collez-la dans Connecteurs.",
    "link": f"/org/{ORG}/connectors/emailing", "action": "connectors"}


def _dire(ep, **autre):
    from oto_mcp.db import alertes_slack
    alertes_slack.poser_message_client(ep["id"], {**_MESSAGE, **autre})


def _allumer():
    from oto_mcp.db import alertes_slack
    alertes_slack.activer_a_la_connexion(ORG, dm_user="U0JANE", dm_name="Jane")


def _diagnose(ep, **kw):
    from oto_mcp.capabilities import agent_repair as AR
    from oto_mcp.capabilities._types import ResolvedCtx
    return AR._agent_repair(ResolvedCtx(sub="medecin"),
                            AR.AgentRepairInput(op="diagnose", episode_id=ep["id"], **kw))


# --- Réglage allumé ----------------------------------------------------------------------

def test_la_connexion_allume_en_prive_sans_defaire_un_admin(live):
    from oto_mcp.db import alertes_slack
    _allumer()
    r = alertes_slack.reglage(ORG)
    assert (r["enabled"], r["channel_id"], r["dm_user"]) == (True, None, "U0JANE")
    alertes_slack.poser(ORG, enabled=False, channel_id="C1", channel_name="x")
    alertes_slack.activer_a_la_connexion(ORG, dm_user="U0AUTRE", dm_name="Autre")
    r = alertes_slack.reglage(ORG)
    assert (r["enabled"], r["channel_id"], r["dm_user"]) == (False, "C1", "U0JANE")


def test_un_episode_du_client_part_une_fois_avec_ses_boutons(slack, agent):
    _allumer()
    ep = _episode(agent)
    _dire(ep)
    assert A.balayer()["envoyes"] == 1
    assert A.balayer()["envoyes"] == 0
    (post,) = slack.posts
    assert post["channel"] == "U0JANE"
    assert "Leads vers l'emailing" in post["text"] and "ne fonctionne plus" in post["text"]
    assert "nouvelle clé API" in post["text"]
    boutons = post["blocks"][1]["elements"]
    assert boutons[0]["url"] == f"{BASE}/org/{ORG}/connectors/emailing"
    assert boutons[1]["url"] == f"{BASE}/org/{ORG}/agents?agent={agent}"


def test_sans_texte_du_medecin_l_alerte_attend_puis_part_avec_la_classe(slack, agent):
    _allumer()
    ep = _episode(agent, il_y_a_min=5, classe="cle_refusee")
    assert A.balayer()["envoyes"] == 0
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("UPDATE agent_episodes SET ouvert_at = NOW() - INTERVAL '3 hours' "
                     "WHERE id = %s", (ep["id"],))
    assert A.balayer()["envoyes"] == 1
    assert "branché sur Acme a été refusé" in slack.posts[0]["text"]
    assert slack.posts[0]["blocks"][1]["elements"][0]["url"] == f"{BASE}/connectors"


def test_sans_lien_le_bouton_ouvre_la_reparation_sous_la_marque(slack, agent):
    from oto_mcp.db import alertes_slack
    _allumer()
    ep = _episode(agent)
    alertes_slack.poser_message_client(ep["id"], {"headline": "Il s'est arrêté."})
    A.balayer()
    premier = slack.posts[0]["blocks"][1]["elements"][0]
    assert premier["url"] == f"{BASE}/org/{ORG}/agents?agent={agent}&fix=1"
    assert premier["text"]["text"] == "Réparer avec Acme"


def test_sans_page_d_agent_declaree_aucun_bouton_invente(slack, agent, allume):
    allume.delenv("OTO_DASHBOARD_LINK_PATHS")
    from oto_mcp.db import alertes_slack
    _allumer()
    ep = _episode(agent)
    alertes_slack.poser_message_client(ep["id"], {"headline": "Il s'est arrêté."})
    A.balayer()
    assert len(slack.posts[0]["blocks"]) == 1, "ni réparation ni page d'agent déclarées"


def test_un_lien_hors_de_l_instance_est_omis_du_message(slack, agent):
    _allumer()
    ep = _episode(agent)
    _dire(ep, link="https://ailleurs.example.test/piege")
    A.balayer()
    urls = [b["url"] for b in slack.posts[0]["blocks"][1]["elements"]]
    assert not any("ailleurs" in u for u in urls)
    assert urls[0] == f"{BASE}/org/{ORG}/agents?agent={agent}&fix=1"


def test_diagnose_refuse_un_lien_hors_de_l_instance(agent):
    from oto_mcp.capabilities._types import AuthzDenied
    ep = _episode(agent)
    for lien in ("https://ailleurs.example.test/x", "//ailleurs.example.test/x",
                 "javascript:alert(1)", "https://app.example.test.ailleurs.test/x"):
        with pytest.raises(AuthzDenied) as e:
            _diagnose(ep, client_headline="Il s'est arrêté.", client_link=lien)
        assert e.value.code == "client_link_off_instance", lien


def test_diagnose_garde_un_chemin_ou_une_adresse_de_l_instance(agent):
    from oto_mcp.db import alertes_slack
    ep = _episode(agent)
    _diagnose(ep, client_headline="Il s'est arrêté.", client_link="/org/1/connectors")
    _diagnose(ep, client_headline="Il s'est arrêté.", client_link=f"{BASE}/org/1/x")
    vivant = alertes_slack.problemes_vivants(ORG, [agent])[agent]
    assert vivant["message_client"]["link"] == f"{BASE}/org/1/x"


def test_diagnose_exige_le_titre_avec_les_autres_champs(agent):
    from oto_mcp.capabilities._types import AuthzDenied
    ep = _episode(agent)
    with pytest.raises(AuthzDenied) as e:
        _diagnose(ep, client_todo="Faites ceci.")
    assert e.value.code == "client_headline_required"


def test_une_panne_de_notre_cote_ne_part_jamais(slack, agent):
    _allumer()
    _episode(agent, cote="plateforme", il_y_a_min=600)
    assert A.balayer()["envoyes"] == 0


def test_la_reparation_puis_rien_d_autre(slack, agent):
    from oto_mcp.db import episodes_agents, runner_triggers as rt
    _allumer()
    ep = _episode(agent, cote="inconnu")
    rt.noter_reparation_pour_le_client(agent, ORG, "Nous avons ajouté la recherche web.")
    assert A.balayer()["envoyes"] == 1
    assert "réparé par Acme" in slack.posts[0]["text"]
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
    assert slack.posts[-1]["text"] == "✅ *Leads vers l'emailing* refonctionne."


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
                   "slack_workspace": "Acme", "dm_to": {"name": "Jane"}}
    assert R._lister_canaux(ctx, R.AgentAlertsInput(org_id=ORG)) == {
        "channels": [{"id": "C1", "name": "acme-alertes"}]}
    with pytest.raises(AuthzDenied):
        R._poser(ctx, R.AgentAlertsSaveInput(org_id=ORG, enabled=True, channel_id="C2"))
    vue = R._poser(ctx, R.AgentAlertsSaveInput(org_id=ORG, enabled=True, channel_id="C1"))
    assert vue["channel"] == {"id": "C1", "name": "acme-alertes"}
    assert R._tester(ctx, R.AgentAlertsInput(org_id=ORG)) == {
        "ok": True, "channel_name": "acme-alertes"}
    assert slack.posts[-1]["channel"] == "C1"
    assert "Acme vous préviendra" in slack.posts[-1]["text"]


def _travail_mort(agent):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute(
            "INSERT INTO runner_jobs (org_id, kind, payload, status, sub, finished_at, "
            "last_error) VALUES (%s, 'start', %s::jsonb, 'failed', 'jane', NOW(), "
            "'HTTP 401') RETURNING id",
            (ORG, json.dumps({"trigger_id": agent}))).fetchone()["id"]


def test_la_file_sert_le_probleme_sur_le_dernier_travail_mort(slack, agent):
    from oto_mcp.capabilities import runner_jobs as RJ
    from oto_mcp.capabilities._types import ResolvedCtx
    j = _travail_mort(agent)
    ep = _episode(agent, classe="cle_refusee", job_id=j)
    _dire(ep)
    out = RJ._avec_le_probleme(ResolvedCtx(sub="jane", org_id=ORG),
                               [{"id": j, "payload": {"trigger_id": agent}},
                                {"id": j - 1, "payload": {"trigger_id": agent}}])
    assert out[0]["problem"] == {**_MESSAGE, "link": f"{BASE}/org/{ORG}/connectors/emailing",
                                 "side": "client"}
    assert "problem" not in out[1]


def test_le_cote_inconnu_se_dit_sans_nom(slack, agent):
    from oto_mcp.capabilities import runner_jobs as RJ
    from oto_mcp.capabilities._types import ResolvedCtx
    j = _travail_mort(agent)
    _episode(agent, cote="inconnu", job_id=j)
    out = RJ._avec_le_probleme(ResolvedCtx(sub="jane", org_id=ORG),
                               [{"id": j, "payload": {"trigger_id": agent}}])
    assert out[0]["problem"]["side"] == "operator"
    assert "Acme vous dit quoi faire" in out[0]["problem"]["todo"]


def test_connecter_slack_pour_l_org_allume_les_alertes(live):
    from oto_mcp.auth import slack as auth_slack
    from oto_mcp.db import alertes_slack
    auth_slack._allumer_les_alertes(ORG, {"authed_user": {"id": "U0JOHN"}}, "john")
    r = alertes_slack.reglage(ORG)
    assert (r["enabled"], r["dm_user"], r["channel_id"]) == (True, "U0JOHN", None)


def test_la_marque_vient_du_tenant_de_l_org(live, allume):
    allume.setenv("OTO_BRAND_NAME", "Acme")
    allume.setenv("OTO_BRAND_SITE", "acme.example.test")
    assert A.marque_de_l_org(ORG).nom == "Acme"


# --- Réglage éteint : le comportement d'avant ------------------------------------------

@pytest.fixture(params=["absent", "", "0", "no", "off", "false"])
def eteint(request, allume):
    if request.param == "absent":
        allume.delenv("OTO_ALERTES_SLACK_AGENTS_ENABLED")
    else:
        allume.setenv("OTO_ALERTES_SLACK_AGENTS_ENABLED", request.param)
    return allume


def test_eteint_les_routes_repondent_404(eteint, slack):
    from oto_mcp.capabilities import agent_alerts as R
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    ctx = ResolvedCtx(sub="admin", org_id=ORG)
    for appel in (lambda: R._lire(ctx, R.AgentAlertsInput(org_id=ORG)),
                  lambda: R._lister_canaux(ctx, R.AgentAlertsInput(org_id=ORG)),
                  lambda: R._tester(ctx, R.AgentAlertsInput(org_id=ORG)),
                  lambda: R._poser(ctx, R.AgentAlertsSaveInput(org_id=ORG, enabled=True))):
        with pytest.raises(AuthzDenied) as e:
            appel()
        assert (e.value.status, e.value.code) == (404, "agent_alerts_off")
    assert slack.posts == []


def test_eteint_connecter_slack_n_ecrit_rien(eteint):
    from oto_mcp.auth import slack as auth_slack
    from oto_mcp.db import alertes_slack
    auth_slack._allumer_les_alertes(ORG, {"authed_user": {"id": "U0JOHN"}}, "john")
    assert alertes_slack.reglage(ORG) is None


def test_eteint_la_boucle_n_est_pas_armee_et_ne_passe_pas(eteint):
    from oto_mcp import boucles_de_fond
    assert boucles_de_fond._alertes_slack_agents_armee() is False
    with pytest.raises(A.AlertesEteintes):
        A.balayer()


def test_eteint_le_courriel_ne_consulte_pas_slack(eteint, monkeypatch):
    from oto_mcp import alerte_agents_bloques
    from oto_mcp.db import alertes_agents

    def _interdit(ids):
        raise AssertionError("Slack consulté alors que les alertes sont éteintes")
    monkeypatch.setattr(alerte_agents_bloques, "_annonces_sur_slack", _interdit)
    monkeypatch.setattr(alerte_agents_bloques, "_heures_calmes", lambda m: False)
    monkeypatch.setattr(alertes_agents, "agents_bloques", lambda seuil: [])
    alerte_agents_bloques.balayer()


def test_eteint_la_file_ne_sert_pas_de_probleme(eteint, slack, agent):
    from oto_mcp.capabilities import runner_jobs as RJ
    from oto_mcp.capabilities._types import ResolvedCtx
    j = _travail_mort(agent)
    _episode(agent, job_id=j)
    jobs = [{"id": j, "payload": {"trigger_id": agent}}]
    assert RJ._avec_le_probleme(ResolvedCtx(sub="jane", org_id=ORG), jobs) is jobs


def test_eteint_diagnose_refuse_les_champs_client(eteint, agent):
    from oto_mcp.capabilities._types import AuthzDenied
    ep = _episode(agent)
    with pytest.raises(AuthzDenied) as e:
        _diagnose(ep, client_headline="Il s'est arrêté.")
    assert e.value.code == "agent_alerts_off"
    assert _diagnose(ep, diagnostic="clé refusée")["episode"]["diagnostic"] == "clé refusée"


def test_une_valeur_illisible_leve(allume):
    from oto_mcp import interrupteurs
    allume.setenv("OTO_ALERTES_SLACK_AGENTS_ENABLED", "peut-etre")
    with pytest.raises(interrupteurs.InterrupteurIllisible):
        A.actives()


# --- La cause approuvée, et un message par problème (13/11/2026) ------------------------

def _autre_agent(label):
    from oto_mcp.db import runner_triggers as rt
    return rt.create_trigger(ORG, "jane", procedure="leads-push", tz="UTC", tools=["a"],
                             cron="0 6 * * *", next_due="2999-01-01 00:00:00+00",
                             label=label)["id"]


def _cause(cle, cote="proprietaire", copie=None, statut="approuvee"):
    from oto_mcp.db import causes_agents
    causes_agents.relever([(cle, "x", "cle_refusee", cote)])
    causes_agents.decider(cle, acteur="ops", statut=statut, copie=copie or {
        "fr": {"headline": "Le compte de paiement de {marque} est vide.",
               "todo": "Ajoutez des crédits.", "action": "credits"}})


def test_la_copie_de_la_cause_approuvee_part_tout_de_suite_et_l_emporte(slack, agent):
    _allumer()
    _cause("test-c1")
    ep = _episode(agent, empreinte="test-c1")
    _dire(ep)
    assert A.balayer()["envoyes"] == 1
    texte = slack.posts[0]["text"]
    assert "Le compte de paiement de Acme est vide." in texte
    assert "Ajoutez des crédits." in texte and "nouvelle clé API" not in texte
    assert slack.posts[0]["blocks"][1]["elements"][0]["url"] == f"{BASE}/org/billing"


def test_une_cause_proposee_ou_d_un_autre_cote_ne_parle_pas(slack, agent):
    _allumer()
    _cause("test-c2", statut="proposee")
    ep = _episode(agent, empreinte="test-c2")
    assert A.balayer()["envoyes"] == 0, "proposée : l'attente de la classe tient"
    _cause("test-c3", cote="plateforme")
    from oto_mcp.db import episodes_agents
    episodes_agents.ouvrir_ou_rafraichir(
        org_id=ORG, trigger_id=agent, classe="inconnu", cote="proprietaire",
        motif="HTTP 401", job_id=None, travaux=1, empreinte="test-c3")
    _dire(ep)
    A.balayer()
    assert "nouvelle clé API" in slack.posts[0]["text"]


def test_trois_agents_arretes_par_la_meme_cause_font_un_message(slack, agent):
    _allumer()
    _cause("test-c4")
    autres = [_autre_agent("Relances"), _autre_agent("Veille")]
    for t in [agent, *autres]:
        _episode(t, empreinte="test-c4")
    bilan = A.balayer()
    assert (bilan["envoyes"], bilan["messages"]) == (3, 1)
    (post,) = slack.posts
    assert post["text"].startswith("⚠️ 3 agents ne fonctionnent plus depuis")
    for nom in ("Leads vers l'emailing", "Relances", "Veille"):
        assert f"*{nom}*" in post["text"]
    assert post["text"].count("Ajoutez des crédits.") == 1
    assert A.balayer()["envoyes"] == 0, "les trois sont marqués"


def test_deux_problemes_differents_font_deux_messages(slack, agent):
    _allumer()
    autre = _autre_agent("Relances")
    _dire(_episode(agent))
    _dire(_episode(autre), headline="Le compte au nom duquel l'agent tourne a été retiré.")
    assert A.balayer()["messages"] == 2


def test_au_dela_de_cinq_agents_les_autres_se_comptent(slack, agent):
    _allumer()
    _cause("test-c5")
    for t in [agent, *[_autre_agent(f"Agent {i}") for i in range(6)]]:
        _episode(t, empreinte="test-c5")
    A.balayer()
    assert "et 2 autres" in slack.posts[0]["text"]


def test_les_retours_d_un_passage_partent_ensemble(slack, agent):
    from oto_mcp.db import episodes_agents
    _allumer()
    _cause("test-c6")
    autre = _autre_agent("Relances")
    eps = [_episode(t, empreinte="test-c6") for t in (agent, autre)]
    A.balayer()
    for ep in eps:
        episodes_agents.poser(int(ep["id"]), statut="clos", resolution="gueri")
    bilan = A.balayer()
    assert (bilan["envoyes"], bilan["messages"]) == (2, 1)
    assert slack.posts[-1]["text"] == "✅ *Leads vers l'emailing*, *Relances* refonctionnent."


def test_la_file_sert_la_copie_de_la_cause(slack, agent):
    from oto_mcp.capabilities import runner_jobs as RJ
    from oto_mcp.capabilities._types import ResolvedCtx
    _cause("test-c7")
    j = _travail_mort(agent)
    _dire(_episode(agent, classe="cle_refusee", job_id=j, empreinte="test-c7"))
    out = RJ._avec_le_probleme(ResolvedCtx(sub="jane", org_id=ORG),
                               [{"id": j, "payload": {"trigger_id": agent}}])
    assert out[0]["problem"] == {
        "headline": "Le compte de paiement de Acme est vide.", "todo": "Ajoutez des crédits.",
        "action": "credits", "link": f"{BASE}/org/billing", "side": "client"}


# --- D5 : le texte du médecin part, et devient la copie proposée de sa cause -------------

def test_diagnose_refuse_un_texte_que_le_client_ne_peut_pas_lire(agent):
    from oto_mcp.capabilities._types import AuthzDenied
    ep = _episode(agent)
    for kw in ({"client_headline": "La clé de lemlist_act est refusée."},
               {"client_headline": "Il s'est arrêté.", "client_todo": "Voir l'erreur 4011."},
               {"client_headline": "Il s'est arrêté.", "client_todo": "Lisez `run_note`."}):
        with pytest.raises(AuthzDenied) as e:
            _diagnose(ep, **kw)
        assert e.value.code == "client_copy_refused", kw


def test_le_texte_du_medecin_devient_la_copie_proposee_de_sa_cause(slack, agent):
    from oto_mcp.db import causes_agents
    _allumer()
    causes_agents.relever([("test-d5", "x", "inconnu", "proprietaire")])
    ep = _episode(agent, empreinte="test-d5")
    _diagnose(ep, client_headline="La clé de l'outil d'emailing est refusée.",
              client_todo="Collez une nouvelle clé dans Connecteurs.",
              client_link="/org/1/connectors", client_action="connectors")
    assert A.balayer()["envoyes"] == 1, "le texte part tout de suite, sans attendre"
    cause = causes_agents.lire("test-d5")
    assert cause["statut"] == "proposee", "il attend l'approbation d'un opérateur"
    assert cause["copie"] == {"fr": {
        "headline": "La clé de l'outil d'emailing est refusée.",
        "todo": "Collez une nouvelle clé dans Connecteurs.", "action": "connectors"}}
    _diagnose(ep, client_headline="Un autre texte.")
    assert causes_agents.lire("test-d5")["copie"]["fr"]["headline"].startswith("La clé"), \
        "la première copie proposée tient"


def test_le_medecin_ne_touche_pas_la_copie_d_une_cause_approuvee(agent):
    from oto_mcp.db import causes_agents
    _cause("test-d5b")
    ep = _episode(agent, empreinte="test-d5b")
    _diagnose(ep, client_headline="Un autre texte.")
    assert causes_agents.lire("test-d5b")["copie"]["fr"]["headline"].startswith(
        "Le compte de paiement")
