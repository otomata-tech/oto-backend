"""Un agent hébergé déclare son modèle — ou il ne se pose pas, et ne part pas.

Décidé le 24/09/2026, avec l'ouverture des agents hébergés à toutes les orgs :
chaque org paie son modèle. Or la garde d'argent (`_cle_exigee`) ne juge que la
famille DÉCLARÉE. Un agent sans modèle n'exigeait aucune clé et tournait sur le
modèle du worker — c'est-à-dire sur notre clé, sans que rien ne le dise. Mesuré en
production le même jour : deux déclencheurs actifs dans ce cas.

Deux moments, comme la clé exigée : la POSE refuse lisiblement (`model_required`,
`_modele.exige_un_modele`) ; la RÉSERVATION arrête ce qui a été posé avant, raison
écrite (`runner_jobs._avec_cle`). La création, la retouche qui retirerait le modèle
et l'armement d'un passage sans modèle ont leur banc dans `test_modele_de_l_agent.py`.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp.capabilities import runner_fleets as RF
from oto_mcp.capabilities import runner_jobs as RJ
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

ORG = 2



@pytest.fixture(autouse=True)
def _l_appelant_possede_l_agent(monkeypatch):
    """Ce fichier ne parle pas du PARTAGE d'agents — il a son banc
    (`test_partage_agents.py`). L'appelant y est lu propriétaire de tout agent
    qu'on lui présente ; sans cette doublure, la lecture de rôle irait chercher la
    vraie base."""
    from oto_mcp.capabilities import _acces_agent
    monkeypatch.setattr(_acces_agent, "niveaux",
                        lambda sub, org_id, agents: {int(t["id"]): "owner" for t in agents})

@pytest.fixture(autouse=True)
def _cle_de_modele_non_exigee(monkeypatch):
    """Le réglage de clé exigée est lu ÉTEINT : ce banc parle du MODÈLE, pas de la
    clé — sans la doublure, la lecture irait chercher la vraie base."""
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **k: None)


def _ctx():
    return ResolvedCtx(sub="alexis", org_id=ORG)


# ── la POSE : le rallumage d'un agent posé avant la règle ─────────────────────

def test_rallumer_un_agent_pose_SANS_modele_est_refuse(monkeypatch):
    """Éteint, un agent d'avant la règle se range et se lit ; il ne se rallume
    qu'en déclarant un modèle, dans le même appel ou avant."""
    monkeypatch.setattr(RT.db, "runner_arme", lambda org: {
        "armed": True, "workers": 1, "last_seen": None, "families": ["anthropic"]})
    monkeypatch.setattr(RT.db, "get_trigger", lambda i, o: {
        "id": 3, "enabled": False, "kind": "schedule", "cron": "5 6 * * *",
        "tz": "UTC", "model": None})
    ecrit = {}
    monkeypatch.setattr(RT.db, "update_trigger",
                        lambda i, o, champs, **k: ecrit.update(champs) or {"id": i, **champs})
    with pytest.raises(AuthzDenied) as e:
        asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(op="update", trigger_id=3,
                                                         enabled=True)))
    assert (e.value.status, e.value.code) == (400, "model_required")
    assert not ecrit, "un refus n'écrit rien"

    # Le même rallumage, qui déclare son modèle, passe.
    asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(op="update", trigger_id=3,
                                                     enabled=True,
                                                     model="claude-sonnet-5")))
    assert ecrit["enabled"] is True and ecrit["model"] == "claude-sonnet-5"


def test_lancer_un_passage_INCONNU_rend_toujours_404(monkeypatch):
    """La garde du modèle ne masque pas « automatisation inconnue » : sans passage
    lu, il n'y a aucun modèle à juger."""
    from oto_mcp import roles
    monkeypatch.setattr(roles, "is_org_admin", lambda *a, **k: True)
    monkeypatch.setattr(RF, "_run_courant", lambda: None)
    monkeypatch.setattr(RF.db, "get_fleet", lambda *a, **k: None)
    monkeypatch.setattr(RF.db, "runner_arme", lambda org: {
        "armed": True, "workers": 1, "last_seen": None, "families": ["anthropic"]})
    monkeypatch.setattr(RF.db, "armer", lambda *a, **k: None)
    with pytest.raises(AuthzDenied) as e:
        RF._fleets(_ctx(), RF.FleetInput(op="launch", fleet_id=1))
    assert (e.value.status, e.value.code) == (404, "fleet_not_found")


# ── la RÉSERVATION : ce qui a été posé avant la règle ne part plus ────────────

@pytest.fixture
def arret(monkeypatch):
    vu = {}
    monkeypatch.setattr(RJ.db, "arreter_definitivement",
                        lambda job_id, sub, raison: vu.update(job=job_id, raison=raison) or True)
    return vu


def _travail(**kw):
    return {"id": 9, "org_id": ORG, "sub": "alexis", "delegated_token": "otd_x", **kw}


@pytest.mark.parametrize("depot", ["anthropic", "", None])
def test_un_travail_SANS_famille_reserve_par_un_worker_est_ARRETE(arret, depot):
    """Servi à un worker de plateforme, il tournait sur le modèle de SON
    environnement. Arrêté pour de bon, raison écrite — quel que soit le dépôt que
    le worker nomme — et sans le jeton délégué émis juste avant."""
    rendu = RJ._avec_cle(_travail(payload={"procedure": "p"}), depot, "worker:banc",
                         worker=True)
    assert rendu["delegation_refusee"] == RJ._SANS_MODELE
    assert "déclare aucun modèle" in rendu["delegation_refusee"]
    assert "model_key" not in rendu and not rendu.get("delegated_token")
    assert arret == {"job": 9, "raison": RJ._SANS_MODELE}


def test_un_travail_AVEC_famille_n_est_pas_arrete_par_cette_garde(arret):
    rendu = RJ._avec_cle(_travail(payload={"model_family": "anthropic"}), "anthropic",
                         "worker:banc", worker=True)
    assert "delegation_refusee" not in rendu and not arret


def test_un_MEMBRE_qui_reserve_un_travail_sans_famille_n_est_pas_arrete(arret):
    """La garde vise ce qui retombe sur NOTRE clé : un worker de plateforme. Un
    membre qui réserve tourne sur ce qu'il a — même règle que la clé exigée."""
    rendu = RJ._avec_cle(_travail(payload={}), "anthropic", "alexis", worker=False)
    assert "delegation_refusee" not in rendu and not arret
