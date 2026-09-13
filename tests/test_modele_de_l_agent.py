"""Le modèle se déclare sur l'AGENT et part avec le TRAVAIL (oto#81).

Avant ce lot, le modèle d'un agent hébergé était une variable d'environnement du
worker : un déclencheur ne pouvait pas en nommer un, et une flotte en stockait un
que rien ne lisait — un champ qui PROMETTAIT une attribution qui n'arrivait pas.

Trois familles de bancs ici, sans base :

1. **le catalogue** — un nom inconnu est refusé à l'écriture, et un travail sans
   modèle reste le travail d'avant, à l'octet ;
2. **la promesse** — un agent ne se pose (ni ne se rallume, ni ne s'arme) sur un
   modèle qu'aucun worker vivant ne sert : son travail attendrait pour rien ;
3. **le transport** — le modèle et sa famille partent avec le travail, depuis le
   tick comme depuis une campagne, et un `continue` garde celui de son run.

Le filtre du claim et la présence par famille vivent en SQL : ils sont éprouvés en
base, dans `test_modele_de_l_agent_db.py`.
"""
from __future__ import annotations

import pytest

from oto_mcp import runner_models, runner_tick
from oto_mcp.capabilities import _modele
from oto_mcp.capabilities import runner_fleets as RF
from oto_mcp.capabilities import runner_jobs as RJ
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


@pytest.fixture(autouse=True)
def _cle_de_modele_non_exigee(monkeypatch):
    """Ce fichier ne parle pas de la garde de clé de modèle — elle a son propre banc
    (`test_cle_de_modele_exigee.py`). Le réglage est lu ÉTEINT, comme sur toute
    plateforme qui ne l'a pas allumé : sans cette doublure, la lecture irait
    chercher la vraie base et chaque banc tomberait sur une raison qui n'est pas
    la sienne."""
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **k: None)


def _ctx(sub="alexis", org_id=2):
    return ResolvedCtx(sub=sub, org_id=org_id)


def _declencher(**kw):
    return RT._triggers(_ctx(), RT.TriggerInput(**kw))


def _runner(monkeypatch, familles=("anthropic",), armed=True):
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: {"armed": armed, "workers": int(armed),
                                     "last_seen": "2026-09-12 07:00:00",
                                     "families": list(familles)})


# ── 1. le catalogue ───────────────────────────────────────────────────────────

def test_la_famille_se_deduit_du_modele_et_l_inconnu_n_en_a_pas():
    assert runner_models.famille("claude-opus-5") == "anthropic"
    assert runner_models.famille("mistral-large-2512") == "mistral"
    assert runner_models.famille("gpt-9") is None
    assert runner_models.famille(None) is None


def test_un_modele_inconnu_ne_part_PAS_avec_le_travail():
    """Sans famille, rien ne sait le router : l'envoyer le ferait appeler par un
    worker quelconque chez un fournisseur qui ne le sert peut-être pas."""
    assert runner_models.charge("claude-haiku-4-5") == {
        "model": "claude-haiku-4-5", "model_family": "anthropic"}
    assert runner_models.charge("un-modele-libre") == {}
    assert runner_models.charge(None) == {}


@pytest.mark.parametrize("familles, attendu", [
    ((), None),
    (("mistral",), "mistral-large-2512"),
    (("anthropic",), "claude-sonnet-5"),
    (("anthropic", "mistral"), "claude-sonnet-5"),
])
def test_le_defaut_propose_est_le_premier_modele_SERVI_et_aucun_sans_famille(
        familles, attendu):
    """Le défaut se DÉRIVE de ce qui est servi. Une marque en dur proposait
    `claude-sonnet-5` alors que les workers de production ne servent que `mistral` :
    un agent qui la suivait se faisait refuser `model_not_served`."""
    modeles = runner_models.catalogue(familles)
    defauts = [m for m in modeles if m["default"]]
    assert len(defauts) <= 1, "au plus un défaut"
    assert all(m["served"] for m in defauts), "un défaut non servi serait refusé"
    if not familles:
        assert defauts == [], "rien de servi, rien à proposer — pas de repli"
    # L'ordre du catalogue est la préférence : le défaut est le PREMIER servi.
    premier_servi = next((m["id"] for m in modeles if m["served"]), None)
    assert (defauts[0]["id"] if defauts else None) == premier_servi == attendu


def test_le_catalogue_servi_marque_ce_qu_un_worker_vivant_sert():
    etat = _modele.etat_servi({"armed": True, "workers": 1, "last_seen": None,
                               "families": ["mistral"]})
    servis = {m["id"]: m["served"] for m in etat["models"]}
    assert servis["mistral-large-2512"] is True
    assert servis["claude-sonnet-5"] is False


def test_un_etat_SANS_familles_se_sert_avec_une_liste_vide():
    """Un worker au jeton d'org, ou un état lu avant ce lot : `families` manque.
    Servi `[]`, jamais absent — un écran lirait l'absence comme « inconnu »."""
    etat = _modele.etat_servi({"armed": True, "workers": 1, "last_seen": None})
    assert etat["families"] == []
    assert not any(m["served"] for m in etat["models"])


# ── 2. la promesse — déclencheurs ─────────────────────────────────────────────

@pytest.fixture
def pose(monkeypatch):
    vu = {}
    monkeypatch.setattr(RT.db, "triggers_for_procedure", lambda o, p: [])
    monkeypatch.setattr(RT.db, "create_trigger",
                        lambda org, sub, **kw: vu.update(kw) or {"id": 1, **kw})
    return vu


def test_un_agent_pose_AVEC_un_modele_l_ecrit(monkeypatch, pose):
    _runner(monkeypatch, familles=("anthropic",))
    _declencher(op="create", procedure="veille", cron="5 6 * * *", tools=["a"],
                model="claude-opus-5")
    assert pose["model"] == "claude-opus-5"


def test_un_agent_pose_SANS_modele_ecrit_NULL_et_non_le_defaut(monkeypatch, pose):
    """⚠️ NULL veut dire « n'importe quel worker, sur le sien ». Écrire le défaut du
    catalogue ferait refuser la création dans une org servie par une autre famille
    — et changerait le modèle de tout agent posé comme avant.

    La doublure ne porte AUCUNE famille, comme un état d'avant ce lot : un agent
    sans modèle ne doit pas les lire."""
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: {"armed": True, "workers": 1, "last_seen": None})
    _declencher(op="create", procedure="veille", cron="5 6 * * *", tools=["a"])
    assert pose["model"] is None


def test_un_modele_hors_catalogue_est_refuse_et_nomme(monkeypatch, pose):
    _runner(monkeypatch)
    with pytest.raises(AuthzDenied) as e:
        _declencher(op="create", procedure="veille", cron="5 6 * * *", tools=["a"],
                    model="gpt-9")
    assert (e.value.status, e.value.code) == (400, "invalid_model")
    assert "gpt-9" in e.value.message and "claude-sonnet-5" in e.value.message
    assert "model" not in pose, "un refus n'écrit rien"


def test_le_modele_inconnu_se_juge_AVANT_la_presence_du_runner(monkeypatch, pose):
    """Même ordre que le cron : ce qui se corrige dans l'appel se dit d'abord."""
    _runner(monkeypatch, familles=(), armed=False)
    with pytest.raises(AuthzDenied) as e:
        _declencher(op="create", procedure="veille", cron="5 6 * * *", tools=["a"],
                    model="gpt-9")
    assert e.value.code == "invalid_model"


def test_un_modele_qu_aucun_worker_ne_sert_n_est_PAS_promis(monkeypatch, pose):
    """LE banc du lot côté promesse. Un runner armé, mais d'une autre famille : le
    travail attendrait un worker Mistral qui n'existe pas, puis périmerait — et le
    déclencheur aurait l'air de marcher."""
    _runner(monkeypatch, familles=("anthropic",))
    with pytest.raises(AuthzDenied) as e:
        _declencher(op="create", procedure="veille", cron="5 6 * * *", tools=["a"],
                    model="mistral-large-2512")
    assert (e.value.status, e.value.code) == (400, "model_not_served")
    assert "anthropic" in e.value.message, "le refus dit ce qui EST servi"
    assert "model" not in pose


def test_sans_runner_du_tout_le_refus_reste_no_runner_armed(monkeypatch, pose):
    """Deux causes, deux gestes : monter un runner n'est pas choisir un modèle."""
    _runner(monkeypatch, familles=(), armed=False)
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: {"armed": False, "workers": 0, "last_seen": None,
                                     "families": []})
    with pytest.raises(AuthzDenied) as e:
        _declencher(op="create", procedure="veille", cron="5 6 * * *", tools=["a"],
                    model="claude-opus-5")
    assert e.value.code == "no_runner_armed"


@pytest.fixture
def retouche(monkeypatch):
    vu = {"stocke": {"id": 3, "enabled": True, "cron": "5 6 * * *", "tz": "UTC",
                     "model": None}}
    monkeypatch.setattr(RT.db, "get_trigger", lambda i, o: dict(vu["stocke"]))
    monkeypatch.setattr(RT.db, "update_trigger",
                        lambda i, o, champs: vu.update(champs=champs) or {"id": i, **champs})
    return vu


def test_changer_le_modele_d_un_agent_ALLUME_vers_un_non_servi_est_refuse(
        monkeypatch, retouche):
    _runner(monkeypatch, familles=("anthropic",))
    with pytest.raises(AuthzDenied) as e:
        _declencher(op="update", trigger_id=3, model="mistral-large-2512")
    assert e.value.code == "model_not_served"
    assert "champs" not in retouche


def test_changer_le_modele_d_un_agent_ETEINT_ne_promet_rien_et_passe(
        monkeypatch, retouche):
    """Même doctrine que le cron : corriger un agent éteint reste ouvert, sinon un
    agent mort deviendrait impossible à ranger."""
    retouche["stocke"]["enabled"] = False
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: pytest.fail("un agent éteint ne promet rien"))
    _declencher(op="update", trigger_id=3, model="mistral-large-2512")
    assert retouche["champs"]["model"] == "mistral-large-2512"


def test_rallumer_un_agent_dont_le_modele_STOCKE_n_est_plus_servi_est_refuse(
        monkeypatch, retouche):
    """Une famille peut disparaître pendant qu'un agent dort. Le rallumer promet à
    nouveau ce modèle-là, même si l'appel ne le nomme pas."""
    retouche["stocke"].update(enabled=False, model="mistral-large-2512")
    _runner(monkeypatch, familles=("anthropic",))
    with pytest.raises(AuthzDenied) as e:
        _declencher(op="update", trigger_id=3, enabled=True)
    assert e.value.code == "model_not_served"


def test_rallumer_en_CHANGEANT_de_modele_juge_le_nouveau(monkeypatch, retouche):
    retouche["stocke"].update(enabled=False, model="mistral-large-2512")
    _runner(monkeypatch, familles=("anthropic",))
    _declencher(op="update", trigger_id=3, enabled=True, model="claude-sonnet-5")
    assert retouche["champs"]["model"] == "claude-sonnet-5"


def test_une_chaine_VIDE_rend_l_agent_au_modele_du_worker(monkeypatch, retouche):
    """Le geste « modèle du worker » d'un écran : NULL écrit, rien à promettre."""
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: pytest.fail("aucun modèle, aucune promesse"))
    _declencher(op="update", trigger_id=3, model="")
    assert retouche["champs"] == {"model": None}


def test_list_sert_le_catalogue_marque(monkeypatch):
    _runner(monkeypatch, familles=("anthropic",))
    monkeypatch.setattr(RT.db, "list_triggers", lambda org: [])
    runner = _declencher(op="list")["runner"]
    assert runner["families"] == ["anthropic"]
    servis = {m["id"]: m["served"] for m in runner["models"]}
    assert servis == {"claude-sonnet-5": True, "claude-opus-5": True,
                      "claude-haiku-4-5": True, "mistral-large-2512": False}
    assert [m["id"] for m in runner["models"] if m["default"]] == ["claude-sonnet-5"]
    # Servi par la seule famille des workers de production : le défaut la suit.
    _runner(monkeypatch, familles=("mistral",))
    runner = _declencher(op="list")["runner"]
    assert [m["id"] for m in runner["models"] if m["default"]] == ["mistral-large-2512"]
    # Aucune famille : aucun défaut, et `families: []` le dit.
    _runner(monkeypatch, familles=())
    runner = _declencher(op="list")["runner"]
    assert runner["families"] == []
    assert not any(m["default"] for m in runner["models"])
    # Et la sortie typée l'accepte : un champ servi hors modèle n'est dans aucun
    # schéma, donc aucun front ne sait qu'il peut le lire.
    RT.TriggerOut(**{"triggers": [], "runner": runner})


# ── 3. le transport — le tick ─────────────────────────────────────────────────

def _tick_avec(monkeypatch, **declencheur):
    enfile = {}
    t = {"id": 5, "org_id": 2, "cron": "5 6 * * *", "tz": "Europe/Paris",
         "next_due": "2026-09-12 04:05:00", "procedure": "veille",
         "project_id": None, "tools": ["data_write"], "input": None,
         "label": None, "max_steps": None, **declencheur}
    monkeypatch.setattr(runner_tick.db, "due_triggers", lambda limit=50: [t])
    monkeypatch.setattr(runner_tick.db, "consume_due", lambda i, vu, p: True)
    monkeypatch.setattr(runner_tick.db, "perimer_travaux_du_declencheur",
                        lambda t, o: 0)
    monkeypatch.setattr(runner_tick.db, "enqueue_job",
                        lambda org, kind, payload=None, **_: enfile.update(payload))
    assert runner_tick._tick() == 1
    return enfile


def test_le_tick_emporte_le_modele_ET_sa_famille(monkeypatch):
    charge = _tick_avec(monkeypatch, model="claude-opus-5")
    assert charge["model"] == "claude-opus-5"
    assert charge["model_family"] == "anthropic", "sans famille, rien ne route"


def test_un_agent_sans_modele_enfile_le_travail_d_AVANT(monkeypatch):
    charge = _tick_avec(monkeypatch, model=None)
    assert "model" not in charge and "model_family" not in charge


# ── 3. le transport — une file enfilée à la main ──────────────────────────────

@pytest.fixture
def file(monkeypatch):
    vu = {}
    monkeypatch.setattr(RJ.db, "enqueue_job",
                        lambda org_id, kind, payload=None, **_: vu.update(payload=payload)
                        or {"id": 7, "status": "pending", "due_at": "2026-09-12"})
    monkeypatch.setattr(RJ.db, "get_run_head",
                        lambda run_id: {"sub": "alexis", "org_id": 2})
    monkeypatch.setattr(RJ.db, "modele_du_run", lambda run_id, org_id: {})
    return vu


def _enfiler(**kw):
    return RJ._jobs(_ctx(), RJ.JobsInput(op="enqueue", **kw))


def test_un_continue_GARDE_le_modele_de_son_run(monkeypatch, file):
    """Un fil ouvert sur une voie ne se poursuit pas sur une autre. Le modèle passé
    à l'appel est ignoré au profit de celui du run."""
    monkeypatch.setattr(RJ.db, "modele_du_run", lambda run_id, org_id: {
        "model": "claude-opus-5", "model_family": "anthropic"})
    _enfiler(kind="continue", run_id="run-X",
             payload={"model": "mistral-large-2512", "model_family": "mistral"})
    assert file["payload"] == {"model": "claude-opus-5", "model_family": "anthropic"}


def test_un_continue_d_un_run_sans_modele_n_invente_rien(file):
    """La charge d'avant, à l'octet : aucune, si l'appel n'en passait pas."""
    _enfiler(kind="continue", run_id="run-X")
    assert file["payload"] is None


def test_la_famille_ne_se_DECLARE_pas_elle_se_deduit(file):
    """Une famille posée par l'appelant enverrait un modèle Anthropic à un worker
    Mistral, qui le recevrait comme une commande valide."""
    _enfiler(kind="start", payload={"procedure": "p", "model": "claude-sonnet-5",
                                    "model_family": "mistral"})
    assert file["payload"]["model_family"] == "anthropic"
    _enfiler(kind="start", payload={"procedure": "p", "model_family": "mistral"})
    assert "model_family" not in file["payload"]


def test_un_start_au_modele_inconnu_est_refuse(file):
    with pytest.raises(AuthzDenied) as e:
        _enfiler(kind="start", payload={"procedure": "p", "model": "gpt-9"})
    assert e.value.code == "invalid_model"


# ── flottes : déclarer, armer ─────────────────────────────────────────────────

@pytest.fixture
def flotte(monkeypatch):
    monkeypatch.setattr(RF.access, "has_option", lambda sub, option, *, org=None: True)
    vu = {}
    monkeypatch.setattr(RF.db, "create_fleet",
                        lambda *a, **k: vu.update(k) or {"id": 1})
    return vu


def _flotte(**kw):
    return RF._fleets(_ctx(), RF.FleetInput(op="create", label="l", procedure="p",
                                            tools=["data_rows"], **kw))


def test_une_flotte_declare_un_modele_du_catalogue_et_le_fournisseur_s_en_deduit(flotte):
    _flotte(model="mistral-large-2512")
    assert (flotte["model"], flotte["provider"]) == ("mistral-large-2512", "mistral")


def test_une_flotte_sans_modele_n_en_ecrit_aucun(flotte):
    _flotte()
    assert (flotte["model"], flotte["provider"]) == (None, None)


@pytest.mark.parametrize("champs", [
    {"model": "un-modele-libre"},
    {"model": "claude-sonnet-5", "provider": "openai"},
    {"provider": "anthropic"},
])
def test_une_flotte_refuse_un_contexte_qui_ne_route_pas(flotte, champs):
    """Un modèle libre, un fournisseur qui contredit le modèle, un fournisseur seul :
    trois champs posés qui ne s'appliqueraient pas."""
    with pytest.raises(AuthzDenied) as e:
        _flotte(**champs)
    assert e.value.code == "invalid_model"
    assert not flotte, "un refus n'écrit rien"


@pytest.fixture
def armement(monkeypatch):
    from oto_mcp import roles
    monkeypatch.setattr(RF.access, "has_option", lambda sub, option, *, org=None: True)
    monkeypatch.setattr(roles, "is_org_admin", lambda *a, **k: True)
    monkeypatch.setattr(RF, "_run_courant", lambda: None)
    monkeypatch.setattr(RF, "_lignes_visees", lambda *a, **k: None)
    vu = {"stockee": {"id": 1, "status": "draft", "procedure": "p", "input": "x",
                      "model": "mistral-large-2512"}}
    monkeypatch.setattr(RF.db, "get_fleet", lambda *a, **k: dict(vu["stockee"]))
    monkeypatch.setattr(RF.db, "armer",
                        lambda *a, **k: vu.update(armee=True) or {"id": 1, "status": "armed"})
    return vu


def test_armer_un_passage_au_modele_NON_servi_est_refuse_et_n_arme_rien(
        monkeypatch, armement):
    """Armé, il passerait `running` au premier travail produit, que le claim
    filtre ; et `campagne_a_servir` n'en produit plus tant qu'il attend. Un passage
    `running` pour toujours, sans une erreur."""
    monkeypatch.setattr(RF.db, "runner_arme",
                        lambda org: {"armed": True, "workers": 1, "last_seen": None,
                                     "families": ["anthropic"]})
    with pytest.raises(AuthzDenied) as e:
        RF._fleets(_ctx(), RF.FleetInput(op="launch", fleet_id=1))
    assert e.value.code == "model_not_served"
    assert "armee" not in armement


def test_armer_un_passage_au_modele_servi_arme(monkeypatch, armement):
    monkeypatch.setattr(RF.db, "runner_arme",
                        lambda org: {"armed": True, "workers": 1, "last_seen": None,
                                     "families": ["mistral"]})
    RF._fleets(_ctx(), RF.FleetInput(op="launch", fleet_id=1))
    assert armement["armee"]


def test_armer_un_passage_SANS_modele_ne_lit_pas_les_familles(monkeypatch, armement):
    """Les passages d'avant ce lot n'ont pas de modèle : aucune garde neuve ne doit
    les toucher."""
    armement["stockee"]["model"] = None
    monkeypatch.setattr(RF.db, "runner_arme",
                        lambda org: pytest.fail("un passage sans modèle ne lit rien"))
    RF._fleets(_ctx(), RF.FleetInput(op="launch", fleet_id=1))
    assert armement["armee"]
