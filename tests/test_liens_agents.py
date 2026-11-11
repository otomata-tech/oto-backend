"""Les liens entre agents (docs/runner-et-automatisations.md §Lier des agents).

`starts` : les liens d'un agent BORNENT ce qu'il lance — un run hébergé d'un agent qui
en déclare ne lance que ce qui est relié au sien ; un agent sans lien garde la règle
d'avant. L'agent relié qui porte `oto_trigger` reçoit la consigne de relais ; le lien ne
lui donne jamais l'outil.
`requires` : l'horloge saute l'échéance d'un agent dont un exigé n'a pas réussi
aujourd'hui. Les gardes de la pose (deux droits, pas de boucle, pas plus loin que la
borne) se jugent sur doublures ; le SQL, sur base éphémère (bas du fichier).
"""
from __future__ import annotations

import asyncio
import datetime

import pytest

from oto_mcp import runner_tick, verrou_org
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

# Ce banc ne parle pas de l'existence de la procédure d'un agent (#1499, conftest).
pytestmark = pytest.mark.usefixtures("procedures_connues")


def _ctx(sub="membre", org_id=2):
    return ResolvedCtx(sub=sub, org_id=org_id)


def _appel(ctx, **kw):
    return asyncio.run(RT._triggers(ctx, RT.TriggerInput(**kw)))


@pytest.fixture
def org(monkeypatch):
    """Une org de six agents (1-6) qu'un membre possède, et sa table de liens en mémoire."""
    from oto_mcp.capabilities import _acces_agent
    liens: list[dict] = []
    agents = {i: {"id": i, "sub": "membre", "org_id": 2, "label": f"agent {i}",
                  "procedure": f"p{i}"} for i in range(1, 7)}
    monkeypatch.setattr(_acces_agent, "niveaux",
                        lambda sub, org_id, ts: {int(t["id"]): "owner" for t in ts})
    monkeypatch.setattr(RT.db, "get_trigger", lambda i, o: agents.get(int(i)))
    monkeypatch.setattr(RT.db, "list_triggers", lambda o: list(agents.values()))

    def poser(org_id, de, vers, genre, condition, par):
        for l in liens:
            if (l["from_trigger_id"], l["to_trigger_id"], l["kind"]) == (de, vers, genre):
                l["condition"] = condition
                return l
        l = {"id": len(liens) + 1, "org_id": org_id, "from_trigger_id": de,
             "to_trigger_id": vers, "kind": genre, "condition": condition,
             "created_by": par, "created_at": "2026-10-22 18:00:00"}
        liens.append(l)
        return l

    monkeypatch.setattr(RT.db.runner_links, "poser", poser)
    monkeypatch.setattr(RT.db.runner_links, "de_l_org", lambda o: list(liens))
    monkeypatch.setattr(RT.db.runner_links, "lire",
                        lambda o, i: next((l for l in liens if l["id"] == i), None))
    monkeypatch.setattr(RT.db.runner_links, "retirer",
                        lambda o, i: liens.remove(next(l for l in liens if l["id"] == i)))
    return {"liens": liens, "agents": agents}


def _relier(de, vers, genre="starts", condition=None):
    return _appel(_ctx(), op="link", trigger_id=de, to_trigger_id=vers, link_kind=genre,
                  condition=condition)


# ── poser ─────────────────────────────────────────────────────────────────────

def test_un_lien_se_pose_et_se_relit(org):
    out = _relier(1, 2, condition="quand des comptes sont trouvés")
    assert out["link"]["kind"] == "starts"
    assert out["link"]["condition"] == "quand des comptes sont trouvés"
    assert out["link"]["created_by"] == "membre"
    vus = _appel(_ctx(), op="links")["links"]
    assert [(l["from_trigger_id"], l["to_trigger_id"]) for l in vus] == [(1, 2)]


def test_un_lien_depuis_un_agent_sans_l_outil_le_dit(org):
    """Le lien ne donne pas `oto_trigger` : sans lui, l'agent ne lancera rien. Le lien
    est posé quand même, et la réponse le dit — jamais un état muet."""
    out = _relier(1, 2)
    assert out["ok"] and len(org["liens"]) == 1, "un avertissement, pas un refus"
    (avert,) = out["link_warnings"]
    assert (avert["tool"], avert["issue"], avert["needed_by"]) == (
        "oto_trigger", "not_granted", "link")
    assert "op=update" in avert["detail"]


def test_un_lien_depuis_un_agent_qui_porte_l_outil_ne_dit_rien(org):
    org["agents"][1]["tools"] = ["data_rows", "oto_trigger"]
    assert _relier(1, 2)["link_warnings"] == []
    # `requires` ne lance rien : l'outil ne lui manque jamais.
    assert _relier(3, 4, genre="requires")["link_warnings"] == []


def test_reposer_un_lien_change_sa_phrase_sans_le_doubler(org):
    _relier(1, 2, condition="a")
    _relier(1, 2, condition="b")
    assert [l["condition"] for l in org["liens"]] == ["b"]


def test_un_agent_ne_se_relie_pas_a_lui_meme(org):
    with pytest.raises(AuthzDenied) as e:
        _relier(3, 3)
    assert e.value.code == "link_self"


def test_un_lien_ne_ferme_pas_de_boucle(org):
    _relier(1, 2)
    _relier(2, 3)
    with pytest.raises(AuthzDenied) as e:
        _relier(3, 1)
    assert e.value.code == "link_cycle"
    # Exiger dans le sens du relais est cohérent : 1 avant 3, des deux façons.
    assert _relier(1, 3, genre="requires")["ok"]


def test_une_boucle_melee_des_deux_genres_est_refusee(org):
    """Reprise : `starts` et `requires` disent tous deux « A avant B ». 1 lance 2, 2
    lance 3 ; si 1 exigeait 3, 1 serait sauté chaque jour quand 3 ne part que lancé
    par la chaîne de 1 — une attente sans fin."""
    _relier(1, 2)
    _relier(2, 3)
    with pytest.raises(AuthzDenied) as e:
        _relier(3, 1, genre="requires")
    assert e.value.code == "link_cycle"
    # Et dans l'autre sens : une exigence posée d'abord, un relais qui la referme.
    _relier(4, 5, genre="requires")
    with pytest.raises(AuthzDenied) as e:
        _relier(5, 4)
    assert e.value.code == "link_cycle"
    assert len(org["liens"]) == 3, "rien n'est posé sur un refus"


def test_une_boucle_d_exigences_est_refusee(org):
    """Deux agents qui s'exigent l'un l'autre seraient sautés tous les jours."""
    _relier(1, 2, genre="requires")
    _relier(2, 3, genre="requires")
    with pytest.raises(AuthzDenied) as e:
        _relier(3, 1, genre="requires")
    assert e.value.code == "link_cycle"


def test_une_chaine_declaree_s_arrete_a_la_borne(org, monkeypatch):
    monkeypatch.setattr(RT, "CHAINE_MAX", 3)
    _relier(1, 2)
    _relier(2, 3)
    _relier(3, 4)
    with pytest.raises(AuthzDenied) as e:
        _relier(4, 5)
    assert e.value.code == "chain_too_deep"
    # Par l'amont aussi : relier 5 → 1 ferait quatre passages de main.
    with pytest.raises(AuthzDenied) as e:
        _relier(5, 1)
    assert e.value.code == "chain_too_deep"


def test_un_agent_lance_au_plus_la_borne_d_agents(org, monkeypatch):
    monkeypatch.setattr(RT, "CHAINE_ENFANTS_MAX", 2)
    _relier(1, 2)
    _relier(1, 3)
    with pytest.raises(AuthzDenied) as e:
        _relier(1, 4)
    assert e.value.code == "chain_too_wide"


def test_relier_exige_de_pouvoir_modifier_les_deux(org, monkeypatch):
    from oto_mcp.capabilities import _acces_agent
    monkeypatch.setattr(_acces_agent, "niveaux",
                        lambda sub, org_id, ts: {int(t["id"]): ("owner" if int(t["id"]) == 1
                                                                else "viewer") for t in ts})
    with pytest.raises(AuthzDenied) as e:
        _relier(1, 2)
    assert e.value.status == 403
    assert not org["liens"], "rien n'est posé"


def test_on_ne_relie_pas_l_agent_d_une_autre_org(org):
    """L'agent d'une autre org ne se lit pas dans la mienne (`get_trigger` borné à
    l'org de l'appel) : 404, comme un agent inconnu — et rien n'est posé."""
    with pytest.raises(AuthzDenied) as e:
        _relier(1, 99)
    assert e.value.status == 404
    with pytest.raises(AuthzDenied) as e:
        _relier(99, 1, genre="requires")
    assert e.value.status == 404
    assert not org["liens"]


def test_un_agent_heberge_ne_pose_pas_de_lien(org):
    """Un lien est une permission : un agent qui lit des données tierces se l'accorderait
    sur une injection de prompt."""
    jeton = verrou_org.poser(verrou_org.Verrou(sub="membre", org_id=2, job_id=9))
    try:
        with pytest.raises(AuthzDenied) as e:
            _relier(1, 2)
    finally:
        verrou_org.lever(jeton)
    assert (e.value.status, e.value.code) == (403, "links_human_only")


def test_les_champs_d_un_lien_ne_vont_qu_a_link(org):
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="update", trigger_id=1, link_kind="starts")
    assert e.value.code == "link_fields_link_only"


def test_retirer_un_lien(org):
    lien = _relier(1, 2)["link"]
    out = _appel(_ctx(), op="unlink", link_id=lien["id"])
    assert out["ok"] and not org["liens"]
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="unlink", link_id=lien["id"])
    assert e.value.code == "link_not_found"


def test_on_ne_voit_que_les_liens_dont_on_voit_les_deux_bouts(org, monkeypatch):
    from oto_mcp.capabilities import _acces_agent
    _relier(1, 2)
    _relier(3, 4)
    monkeypatch.setattr(_acces_agent, "niveaux",
                        lambda sub, org_id, ts: {int(t["id"]): (None if int(t["id"]) == 4
                                                                else "owner") for t in ts})
    vus = _appel(_ctx(), op="links")["links"]
    assert [(l["from_trigger_id"], l["to_trigger_id"]) for l in vus] == [(1, 2)]


# ── run_now : les liens d'un agent bornent ce qu'il lance ─────────────────────

def test_un_run_heberge_ne_lance_qu_un_agent_relie(monkeypatch):
    monkeypatch.setattr(RT.db, "charge_du_travail", lambda j: {"trigger_id": 4})
    monkeypatch.setattr(RT.db.runner_links, "depuis_l_agent",
                        lambda o, de, genre: [{"to_trigger_id": 6}]
                        if (de, genre) == (4, "starts") else [])
    jeton = verrou_org.poser(verrou_org.Verrou(sub="membre", org_id=2, job_id=77))
    try:
        with pytest.raises(AuthzDenied) as e:
            RT._maillon_suivant(2, 5, [])
    finally:
        verrou_org.lever(jeton)
    assert e.value.code == "chain_not_linked"


def test_un_agent_relie_lance_sa_cible_sous_les_bornes_de_la_chaine(monkeypatch):
    """Relié, le lancement passe — et les bornes `chain_*` jouent toujours : le budget
    par `root_job` refuse le 21e run, lien ou pas."""
    monkeypatch.setattr(RT.db, "charge_du_travail",
                        lambda j: {"trigger_id": 4, "_chaine": {"root_job": 70, "depth": 1}})
    monkeypatch.setattr(RT.db, "enfants_du_travail", lambda j, o: [])
    monkeypatch.setattr(RT.db.runner_links, "depuis_l_agent",
                        lambda o, de, genre: [{"to_trigger_id": 5}])
    runs = {"n": 3}
    monkeypatch.setattr(RT.db, "runs_de_la_chaine", lambda r, org: runs["n"])
    jeton = verrou_org.poser(verrou_org.Verrou(sub="membre", org_id=2, job_id=77))
    try:
        assert RT._maillon_suivant(2, 5, []) == {"parent_job": 77, "root_job": 70, "depth": 2,
                                             "parent_label": None}
        runs["n"] = RT.CHAINE_BUDGET
        with pytest.raises(AuthzDenied) as e:
            RT._maillon_suivant(2, 5, [])
        assert e.value.code == "chain_budget_exhausted"
    finally:
        verrou_org.lever(jeton)


def test_un_agent_sans_lien_garde_la_regle_d_avant(monkeypatch):
    """Reprise : les chaînes écrites dans les procédures depuis v1.586.0 n'ont aucun
    lien déclaré — elles ne cassent pas parce que les liens existent. Les bornes
    `chain_*` jouent, et l'enfant n'est pas plus outillé que le lanceur
    (`chain_widens_tools`, banc suivant)."""
    monkeypatch.setattr(RT.db, "charge_du_travail", lambda j: {"trigger_id": 4})
    monkeypatch.setattr(RT.db, "enfants_du_travail", lambda j, o: [])
    monkeypatch.setattr(RT.db, "runs_de_la_chaine", lambda r, org: 1)
    monkeypatch.setattr(RT.db.runner_links, "depuis_l_agent", lambda *a: [])
    jeton = verrou_org.poser(verrou_org.Verrou(sub="membre", org_id=2, job_id=77))
    try:
        assert RT._maillon_suivant(2, 5, [])["depth"] == 1
    finally:
        verrou_org.lever(jeton)


def test_un_agent_relie_lance_une_cible_plus_outillee_que_lui(monkeypatch):
    """Le lien `starts`, posé par une personne qui peut modifier les deux agents, est la
    décision qui permet de passer la main à plus outillé que soi."""
    monkeypatch.setattr(RT.db, "charge_du_travail",
                        lambda j: {"trigger_id": 4, "tools": ["fr_siret"]})
    monkeypatch.setattr(RT.db, "enfants_du_travail", lambda j, o: [])
    monkeypatch.setattr(RT.db, "runs_de_la_chaine", lambda r, org: 1)
    monkeypatch.setattr(RT.db.runner_links, "depuis_l_agent",
                        lambda o, de, genre: [{"to_trigger_id": 5}])
    jeton = verrou_org.poser(verrou_org.Verrou(sub="membre", org_id=2, job_id=77))
    try:
        assert RT._maillon_suivant(2, 5, ["fr_siret", "email_send"])["depth"] == 1
    finally:
        verrou_org.lever(jeton)


@pytest.mark.parametrize("charge", [
    {"trigger_id": 4, "tools": ["fr_siret"]},     # un agent sans lien `starts`
    {"procedure": "p", "tools": ["fr_siret"]},    # un travail sans agent
])
def test_sans_lien_un_run_heberge_ne_lance_pas_plus_outille_que_lui(monkeypatch, charge):
    monkeypatch.setattr(RT.db, "charge_du_travail", lambda j: charge)
    monkeypatch.setattr(RT.db, "enfants_du_travail", lambda j, o: [])
    monkeypatch.setattr(RT.db, "runs_de_la_chaine", lambda r, org: 1)
    monkeypatch.setattr(RT.db.runner_links, "depuis_l_agent", lambda *a: [])
    jeton = verrou_org.poser(verrou_org.Verrou(sub="membre", org_id=2, job_id=77))
    try:
        with pytest.raises(AuthzDenied) as e:
            RT._maillon_suivant(2, 5, ["fr_siret", "email_send"])
        assert (e.value.status, e.value.code) == (403, "chain_widens_tools")
        assert "email_send" in e.value.message
        assert RT._maillon_suivant(2, 5, ["fr_siret", "run_finish"])["depth"] == 1
    finally:
        verrou_org.lever(jeton)


def test_une_personne_lance_sans_lien(monkeypatch):
    """Le bouton de l'écran, le MCP d'un humain : aucun travail porteur, aucun lien lu."""
    monkeypatch.setattr(RT.db.runner_links, "depuis_l_agent",
                        lambda *a: pytest.fail("aucun lien à lire pour une personne"))
    assert RT._maillon_suivant(2, 5, []) is None


def test_un_travail_sans_agent_garde_la_regle_d_avant(monkeypatch):
    """Une flotte, un enfilage à la main : pas d'agent lanceur, donc pas de lien à
    montrer — les bornes de profondeur, de largeur et de budget restent les seules."""
    monkeypatch.setattr(RT.db, "charge_du_travail", lambda j: {"procedure": "p"})
    monkeypatch.setattr(RT.db, "enfants_du_travail", lambda j, o: [])
    monkeypatch.setattr(RT.db, "runs_de_la_chaine", lambda r, org: 1)
    monkeypatch.setattr(RT.db.runner_links, "depuis_l_agent",
                        lambda *a: pytest.fail("aucun lien à lire"))
    jeton = verrou_org.poser(verrou_org.Verrou(sub="membre", org_id=2, job_id=77))
    try:
        assert RT._maillon_suivant(2, 5, [])["depth"] == 1
    finally:
        verrou_org.lever(jeton)


# ── la charge : relais et outil ───────────────────────────────────────────────

def _t(**kw):
    base = {"id": 4, "org_id": 2, "procedure": "trouver-acheteurs", "project_id": None,
            "tools": ["data_rows"], "input": None, "label": None, "max_steps": None,
            "tz": "Europe/Paris", "model": "claude-sonnet-5"}
    base.update(kw)
    return base


_RELAIS = [{"to_trigger_id": 5, "to_label": "Ajouter aux campagnes",
            "to_procedure": "ajouter", "condition": "when buyers are verified"}]


def test_un_agent_relie_qui_porte_l_outil_recoit_la_consigne():
    charge = runner_tick.charge_du_declencheur(
        _t(tools=["data_rows", "oto_trigger"]),
        datetime.datetime(2026, 10, 22, 6, 0, tzinfo=datetime.timezone.utc),
        relais=_RELAIS)
    assert charge["tools"] == ["data_rows", "oto_trigger"]
    assert "trigger_id 5 (“Ajouter aux campagnes”): start it when buyers are verified." \
        in charge["input"]
    assert charge["input"].startswith("Read the procedure `trouver-acheteurs`"), \
        "la procédure de l'agent reste en tête"


def test_le_lien_ne_donne_jamais_l_outil(monkeypatch):
    """Reprise : `oto_trigger` modifie, partage, supprime des agents — pas seulement
    `run_now`. Un lien ne l'ajoute pas à la liste qu'une personne a posée ; sans
    l'outil, aucune consigne qui dirait de l'appeler."""
    charge = runner_tick.charge_du_declencheur(
        _t(), datetime.datetime(2026, 10, 22, 6, 0, tzinfo=datetime.timezone.utc),
        relais=_RELAIS)
    assert charge["tools"] == ["data_rows"]
    assert "run_now" not in charge["input"]


def test_sans_relais_la_charge_est_celle_d_avant():
    charge = runner_tick.charge_du_declencheur(
        _t(), datetime.datetime(2026, 10, 22, 6, 0, tzinfo=datetime.timezone.utc))
    assert charge["tools"] == ["data_rows"]
    assert "run_now" not in charge["input"]


# ── l'horloge : une exigence manquée saute l'échéance ─────────────────────────

def _exigences(monkeypatch, derniers):
    """L'agent 4 exige l'agent 9 ; `derniers` = ce que rend la lecture de son dernier run."""
    vu = {}
    monkeypatch.setattr(runner_tick.db.runner_links, "vers_l_agent",
                        lambda o, vers, genre: [{"from_trigger_id": 9, "from_enabled": True}]
                        if (vers, genre) == (4, "requires") else [])

    def dernier(org_id, tid, depuis):
        vu["depuis"] = depuis
        return derniers
    monkeypatch.setattr(runner_tick.db.runner_links, "dernier_run_depuis", dernier)
    return vu


def test_une_exigence_reussie_laisse_partir(monkeypatch):
    _exigences(monkeypatch, {"status": "done"})
    assert runner_tick.exigence_manquee(_t()) is None


@pytest.mark.parametrize("dernier", [None, {"status": "failed"}, {"status": "claimed"}])
def test_une_exigence_manquee_nomme_l_agent(monkeypatch, dernier):
    _exigences(monkeypatch, dernier)
    assert runner_tick.exigence_manquee(_t()) == 9


def test_un_agent_exige_en_pause_n_exige_plus_rien(monkeypatch):
    """La pause est un choix : sans cette règle, l'agent qui l'exige serait sauté
    chaque jour, sans fin."""
    monkeypatch.setattr(runner_tick.db.runner_links, "vers_l_agent",
                        lambda o, vers, genre: [{"from_trigger_id": 9, "from_enabled": False}])
    monkeypatch.setattr(runner_tick.db.runner_links, "dernier_run_depuis",
                        lambda *a: pytest.fail("un agent en pause ne se lit pas"))
    assert runner_tick.exigence_manquee(_t()) is None


def test_le_jour_est_celui_du_fuseau_de_l_agent(monkeypatch):
    vu = _exigences(monkeypatch, {"status": "done"})
    # 23:30 UTC le 21 = 01:30 le 22 à Paris : la journée gardée commence le 22.
    runner_tick.exigence_manquee(
        _t(), maintenant=datetime.datetime(2026, 10, 21, 23, 30, tzinfo=datetime.timezone.utc))
    assert vu["depuis"].isoformat() == "2026-10-22T00:00:00+02:00"


def test_le_tick_saute_et_motive_une_exigence_manquee(monkeypatch):
    sautees, enfiles = [], []
    _exigences(monkeypatch, {"status": "failed"})
    monkeypatch.setattr(runner_tick.db, "due_triggers", lambda limit=50: [_t(
        cron="0 9 * * *", next_due="2026-10-22 07:00:00", sub="membre")])
    monkeypatch.setattr(runner_tick.db, "consume_due", lambda i, vu, prochaine: True)
    monkeypatch.setattr(runner_tick.org_suspension, "etat", lambda o: None)
    monkeypatch.setattr(runner_tick.procedure_retiree, "archivee", lambda *a: None)
    monkeypatch.setattr(runner_tick, "_occurrence_sautee",
                        lambda t, issue, exige=None: sautees.append((t["id"], issue, exige)))
    monkeypatch.setattr(runner_tick.db, "enqueue_job",
                        lambda *a, **k: enfiles.append(k) or {"id": 1})
    assert runner_tick._tick() == 0
    assert sautees == [(4, runner_tick.db.REFUSE_REQUIRES, 9)]
    assert not enfiles


# ── le SQL ────────────────────────────────────────────────────────────────────

ORG = 9551


def _agent(db, nom):
    return db.create_trigger(ORG, "membre", procedure=nom, tz="Europe/Paris", tools=[],
                             cron="0 9 * * *",
                             next_due=datetime.datetime(2026, 10, 23, 7, 0,
                                                        tzinfo=datetime.timezone.utc))["id"]


def _existe(L, org, de, vers):
    return any((l["from_trigger_id"], l["to_trigger_id"], l["kind"]) == (de, vers, L.DEMARRE)
               for l in L.de_l_org(org))


def test_les_liens_en_base(live):
    from oto_mcp import db
    a, b, c = _agent(db, "a"), _agent(db, "b"), _agent(db, "c")
    L = db.runner_links
    l1 = L.poser(ORG, a, b, L.DEMARRE, "quand x", "membre")
    L.poser(ORG, a, b, L.DEMARRE, "quand y", "membre")
    L.poser(ORG, c, b, L.EXIGE, None, "membre")
    assert [l["condition"] for l in L.de_l_org(ORG) if l["kind"] == "starts"] == ["quand y"]
    assert _existe(L, ORG, a, b) and not _existe(L, ORG, b, a)
    assert not _existe(L, ORG + 1, a, b), "org-scopé"
    assert [l["from_trigger_id"] for l in L.vers_l_agent(ORG, b, L.EXIGE)] == [c]
    assert [r["to_procedure"] for r in L.depuis_l_agent(ORG, a, L.DEMARRE)] == ["b"]
    assert L.retirer(ORG, l1["id"])["id"] == l1["id"]
    assert not _existe(L, ORG, a, b)
    # Un agent supprimé emporte ses liens.
    db.delete_trigger(c, ORG)
    assert L.vers_l_agent(ORG, b, L.EXIGE) == []


def test_le_dernier_run_du_jour_et_les_chaines(live):
    from oto_mcp import db
    a, b = _agent(db, "a2"), _agent(db, "b2")
    L = db.runner_links
    minuit = datetime.datetime.now(datetime.timezone.utc).replace(hour=0, minute=0,
                                                                 second=0, microsecond=0)
    assert L.dernier_run_depuis(ORG, a, minuit) is None
    racine = db.enqueue_job(ORG, "start", payload={"procedure": "a2", "trigger_id": a})
    enfant = db.enqueue_job(ORG, "start", payload={"procedure": "b2", "trigger_id": b},
                            chaine={"parent_job": racine["id"], "root_job": racine["id"],
                                    "depth": 1})
    assert L.dernier_run_depuis(ORG, a, minuit)["id"] == racine["id"]
    rangs = L.chaines_recentes(ORG, minuit - datetime.timedelta(days=1))
    assert {r["id"] for r in rangs} == {racine["id"], enfant["id"]}
    assert {r["id"] for r in L.travaux_des_agents(ORG, [a], minuit)} == {racine["id"]}


def test_un_lien_entre_deux_orgs_ne_peut_pas_exister(live):
    from oto_mcp import db
    a = _agent(db, "a3")
    autre = db.create_trigger(ORG + 1, "membre", procedure="x", tz="Europe/Paris", tools=[],
                              cron="0 9 * * *",
                              next_due=datetime.datetime(2026, 10, 23, 7, 0,
                                                         tzinfo=datetime.timezone.utc))["id"]
    with pytest.raises(ValueError):
        db.runner_links.poser(ORG, a, autre, db.runner_links.DEMARRE, None, "membre")
    assert not _existe(db.runner_links, ORG, a, autre)
    assert not _existe(db.runner_links, ORG + 1, a, autre)


def test_l_exigence_en_base_a_la_frontiere_de_minuit(live):
    """Réussi HIER, échoué aujourd'hui (Paris) : manquée. Réussi aujourd'hui : tenue.
    Et le saut se relit dans `sauts_par_exigence`."""
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    exige, garde = _agent(db, "mailbox"), _agent(db, "outreach")
    db.runner_links.poser(ORG, exige, garde, db.runner_links.EXIGE, None, "membre")
    t = {"id": garde, "org_id": ORG, "tz": "Europe/Paris"}
    # 07:30 UTC le 22 = 09:30 Paris ; minuit Paris = 22:00 UTC le 21.
    maintenant = datetime.datetime(2026, 10, 22, 7, 30, tzinfo=datetime.timezone.utc)

    def run(statut, due):
        j = db.enqueue_job(ORG, "start", payload={"procedure": "mailbox", "trigger_id": exige})
        with _connect() as conn:
            conn.execute("UPDATE runner_jobs SET status = %s, due_at = %s WHERE id = %s",
                         (statut, due, j["id"]))

    run("done", datetime.datetime(2026, 10, 21, 21, 30, tzinfo=datetime.timezone.utc))
    run("failed", datetime.datetime(2026, 10, 21, 22, 30, tzinfo=datetime.timezone.utc))
    assert runner_tick.exigence_manquee(t, maintenant=maintenant) == exige
    run("done", datetime.datetime(2026, 10, 22, 5, 30, tzinfo=datetime.timezone.utc))
    assert runner_tick.exigence_manquee(t, maintenant=maintenant) is None

    with _connect() as conn:
        db.enregistrer(conn, garde, ORG, db.REFUSE_REQUIRES, source="schedule")
    sauts = db.runner_links.sauts_par_exigence(
        ORG, datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
    assert [int(r["trigger_id"]) for r in sauts] == [garde]
