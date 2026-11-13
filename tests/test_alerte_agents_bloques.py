"""Prévenir l'admin quand un agent programmé est bloqué (signal 1168) — sans base.

Les propriétés qui font que ce passage répare quelque chose : un courriel PAR ORG, sous
la marque de l'org ; la marque posée APRÈS l'envoi, et seulement s'il est parti ; rien
pour une org d'un tenant tiers (comptée) ; rien aux heures calmes ; rien hors production
(la boucle se déclare `tiers=True`). Sous le réglage des alertes d'agents, le courriel
parle comme Slack : un bloc par problème, le texte du client, jamais le motif brut."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from oto_mcp import alerte_agents_bloques as M

_JOUR = datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)    # 12:00 à Paris
_NUIT = datetime(2026, 10, 23, 21, 0, tzinfo=timezone.utc)    # 23:00 à Paris


def _agent(tid, org):
    return {"trigger_id": tid, "org_id": org, "libelle": f"agent {tid}", "travaux": 3,
            "depuis": "2026-10-20 06:00:00", "dernier": "2026-10-22 06:00:00",
            "motif": "HTTP 403 <Access denied>"}


@pytest.fixture()
def bureau(monkeypatch):
    from oto_mcp import db, email_templates, org_store, tenancy
    from oto_mcp.db import alertes_agents, episodes_agents
    from oto_mcp.db import users as U

    etat = {"lettres": [], "marques": [], "envoi_ok": True,
            "agents": [_agent(1, 7), _agent(2, 7), _agent(3, 9), _agent(4, 11)]}
    monkeypatch.setattr(M, "_ECARTEES_DITES", set())
    monkeypatch.delenv("OTO_ALERTES_SLACK_AGENTS_ENABLED", raising=False)
    etat["soignes"] = set()
    etat["fenetres"] = []

    def _soignes(ids, *, vus_depuis):
        etat["fenetres"].append(vus_depuis)
        return {i for i in ids if i in etat["soignes"]}
    monkeypatch.setattr(episodes_agents, "agents_soignes_par_nous", _soignes)
    monkeypatch.setattr(alertes_agents, "agents_bloques", lambda seuil: list(etat["agents"]))
    monkeypatch.setattr(alertes_agents, "marquer_annonces",
                        lambda ids: etat["marques"].extend(ids) or len(ids))
    # L'org 9 est celle d'un tenant tiers.
    monkeypatch.setattr(db, "org_tenant_slug",
                        lambda o: "partenaire" if int(o) == 9 else tenancy.primary_slug())
    # L'org 11 n'a aucun admin joignable.
    monkeypatch.setattr(org_store, "list_org_members", lambda o: [] if o == 11 else [
        {"sub": "m", "org_role": "member"}, {"sub": "a", "org_role": "org_admin"}])
    monkeypatch.setattr(U, "emails_by_subs", lambda subs: {s: f"{s}@acme.test" for s in subs})
    monkeypatch.setattr(U, "get_user", lambda s: {"locale": "fr"})
    monkeypatch.setattr(org_store, "org_front", lambda o: (None, None))

    def _envoyer(m, to, subject, html):
        etat["lettres"].append((to, subject, html))
        return etat["envoi_ok"]
    monkeypatch.setattr(email_templates, "_envoyer", _envoyer)
    return etat


def test_un_courriel_par_org_a_son_admin_puis_la_marque(bureau):
    bilan = M.balayer(_JOUR)
    assert [(to, s) for to, s, _ in bureau["lettres"]] == [
        ("a@acme.test", "Un agent programmé est bloqué")]
    html = bureau["lettres"][0][2]
    assert "agent 1" in html and "agent 2" in html
    assert "&lt;Access denied&gt;" in html, "le motif est échappé"
    assert bureau["marques"] == [1, 2]
    assert bilan == {"orgs": 3, "envoyes": 1, "marques": 2, "tenant_tiers_ecartes": 1,
                     "sans_destinataire": 1}


def test_un_envoi_rate_ne_marque_rien(bureau):
    bureau["envoi_ok"] = False
    assert M.balayer(_JOUR)["envoyes"] == 0
    assert bureau["marques"] == []


def test_aux_heures_calmes_rien_ne_part(bureau):
    assert M.balayer(_NUIT) == {"heures_calmes": True}
    assert bureau["lettres"] == [] and bureau["marques"] == []


def test_l_org_d_un_tenant_tiers_n_est_jamais_ecrite(bureau):
    bureau["agents"] = [_agent(3, 9)]
    assert M.balayer(_JOUR)["tenant_tiers_ecartes"] == 1
    assert bureau["lettres"] == [] and bureau["marques"] == []


def test_un_tenant_illisible_ferme(bureau, monkeypatch):
    from oto_mcp import db

    def _casse(o):
        raise RuntimeError("base indisponible")
    monkeypatch.setattr(db, "org_tenant_slug", _casse)
    assert M.balayer(_JOUR)["envoyes"] == 0
    assert bureau["lettres"] == []


def test_la_boucle_est_declaree_tierce():
    from oto_mcp import boucles_de_fond
    b = next(b for b in boucles_de_fond.BOUCLES if b.nom == "alerte_agents_bloques")
    assert b.tiers is True
    assert b.fonction() is M.run_alerte_agents_loop


def test_une_panne_de_la_plateforme_ne_s_annonce_pas_au_titulaire(bureau):
    # L'agent 2 a un épisode côté plateforme (`medecin_agents`) : son titulaire n'y peut
    # rien, il n'est ni nommé ni marqué ; l'agent 1, de la même org, part seul.
    bureau["soignes"] = {2}
    M.balayer(_JOUR)
    html = bureau["lettres"][0][2]
    assert "agent 1" in html and "agent 2" not in html
    assert 2 not in bureau["marques"]
    # Seulement tant que la boucle tient l'épisode : la fenêtre est celle du médecin.
    from oto_mcp import medecin_agents
    assert bureau["fenetres"] == [medecin_agents.FRAICHEUR]


def test_des_episodes_illisibles_laissent_partir_le_courriel(bureau, monkeypatch):
    from oto_mcp.db import episodes_agents

    def _casse(ids, *, vus_depuis):
        raise RuntimeError("base indisponible")
    monkeypatch.setattr(episodes_agents, "agents_soignes_par_nous", _casse)
    M.balayer(_JOUR)
    assert "agent 2" in bureau["lettres"][0][2]


# ── Sous le réglage des alertes : le courriel parle comme Slack ─────────────────

_COPIE = {"fr": {"headline": "La clé de l'outil d'emailing a été refusée.",
                 "todo": "Recréez-la et collez-la dans Connecteurs.",
                 "action": "connectors"}}


def _episode(tid, **kw):
    return {"id": 100 + tid, "org_id": 7, "trigger_id": tid, "classe": "cle_refusee",
            "cote": "proprietaire", "statut": "ouvert", "motif": "HTTP 403 <Access denied>",
            "message_client": None, "cause_copie": None, "cause_cote": None, **kw}


@pytest.fixture()
def comme_slack(bureau, monkeypatch):
    from oto_mcp import links
    from oto_mcp.db import sante_agents
    monkeypatch.setenv("OTO_ALERTES_SLACK_AGENTS_ENABLED", "1")
    monkeypatch.setattr(M, "_annonces_sur_slack", lambda ids: set())
    bureau["vivants"] = {7: []}

    def _vivants(org):
        if bureau["vivants"] is None:
            raise RuntimeError("base indisponible")
        return list(bureau["vivants"].get(org, []))
    monkeypatch.setattr(sante_agents, "episodes_vivants", _vivants)
    base = "https://app.acme.test"
    monkeypatch.setattr(links, "link_for", lambda kind, sub=None, **p: (
        base if kind == "home" else f"{base}/{kind}/{p.get('id', '')}".rstrip("/")))
    return bureau


def test_les_agents_d_un_meme_probleme_forment_un_bloc_sans_le_motif(comme_slack):
    comme_slack["vivants"][7] = [
        _episode(1, cause_copie=_COPIE, cause_cote="proprietaire"),
        _episode(2, cause_copie=_COPIE, cause_cote="proprietaire")]
    M.balayer(_JOUR)
    (to, sujet, html), = comme_slack["lettres"]
    assert (to, sujet) == ("a@acme.test", "Un agent programmé est bloqué")
    assert "Access denied" not in html and "HTTP 403" not in html, "jamais le motif brut"
    assert html.count("La clé de l'outil") == 1, "un problème = un bloc, le texte une fois"
    assert "agent 1" in html and "agent 2" in html
    assert 'href="https://app.acme.test/agent/1"' in html, "chaque agent mène à sa page"
    assert "https://app.acme.test/connectors" in html, "le bouton du geste"
    assert comme_slack["marques"] == [1, 2], "marqués après l'envoi, comme avant"


def test_deux_problemes_deux_blocs_et_le_lien_du_medecin_hors_produit_est_omis(comme_slack):
    comme_slack["vivants"][7] = [
        _episode(1, cause_copie=_COPIE, cause_cote="proprietaire"),
        _episode(2, classe="inconnu", message_client={
            "headline": "Le compte de l'agent n'a plus accès au tableur.",
            "todo": "Partagez de nouveau le tableur avec lui.",
            "link": "https://ailleurs.test/piege"})]
    M.balayer(_JOUR)
    html = comme_slack["lettres"][0][2]
    assert "Le compte de l" in html and "Partagez de nouveau" in html
    assert "La clé de l" in html
    assert "ailleurs.test" not in html, "un lien qui mène hors du produit est omis"
    assert "Access denied" not in html


def test_un_agent_sans_episode_lisible_recoit_la_phrase_par_defaut(comme_slack):
    comme_slack["vivants"] = None          # lecture des épisodes en échec : ouvert
    M.balayer(_JOUR)
    html = comme_slack["lettres"][0][2]
    assert "Il s'est arrêté avant la fin de son travail." in html
    assert "Access denied" not in html
    assert comme_slack["marques"] == [1, 2]


def test_le_texte_du_medecin_est_echappe(comme_slack):
    comme_slack["vivants"][7] = [_episode(1, message_client={
        "headline": "<script>x</script> refusé", "todo": None, "link": None})]
    M.balayer(_JOUR)
    html = comme_slack["lettres"][0][2]
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_en_anglais_le_courriel_par_probleme(comme_slack, monkeypatch):
    from oto_mcp.db import users as U
    monkeypatch.setattr(U, "get_user", lambda s: {"locale": "en"})
    M.balayer(_JOUR)
    sujet, html = comme_slack["lettres"][0][1:]
    assert sujet == "A scheduled agent is blocked"
    assert "It stopped before finishing its work." in html


def test_une_panne_pas_encore_comprise_ne_dit_pas_que_c_est_au_client(comme_slack):
    """Côté `inconnu`, sans texte du médecin : la phrase de l'onglet Santé, jamais celle
    d'une classe qui dirait au client de reconnecter quoi que ce soit."""
    comme_slack["vivants"][7] = [_episode(1, classe="cle_refusee", cote="inconnu")]
    M.balayer(_JOUR)
    html = comme_slack["lettres"][0][2]
    assert "Nous cherchons pourquoi il s" in html
    assert "Reconnectez" not in html and "https://app.acme.test/connectors" not in html


def test_le_courriel_par_probleme_dit_depuis_quand(comme_slack):
    comme_slack["vivants"][7] = [_episode(1), _episode(2)]
    M.balayer(_JOUR)
    assert "En échec depuis le 2026-10-20 06:00 UTC" in comme_slack["lettres"][0][2]
