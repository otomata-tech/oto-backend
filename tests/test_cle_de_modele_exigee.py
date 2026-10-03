"""Un agent hébergé tourne sur la clé de modèle de SON org — ou ne tourne pas.

Sans cette garde, une org qui n'a pas déposé de clé n'est jamais refusée : le worker
retombe sur la clé de son propre environnement, c'est-à-dire la NÔTRE. « Chaque client
paie avec sa propre clé » est une politique que le système n'appliquait pas, et le
défaut est silencieux par construction — les runs aboutissent, seule la facture change
de destinataire.

Ce que ces bancs tiennent :

1. **Éteint par défaut** — rien ne change tant que personne n'a posé le réglage, sinon
   le déploiement arrêterait tous les agents des orgs sans clé, les nôtres comprises ;
2. **l'org l'emporte sur la plateforme** — une exemption, ou une exigence ciblée ;
3. **à la réservation, l'arrêt est DÉFINITIF** et lisible par le worker déjà déployé ;
4. **un worker qui ne nomme aucun dépôt ne sert pas une org qui exige sa clé** — il
   tournerait sur la sienne, quoi que l'org ait déposé ;
5. **à la pose, le refus est lisible** — au moment où l'on peut encore déposer la clé.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp.capabilities import _cle_exigee as CE
from oto_mcp.capabilities import runner_fleets as RF
from oto_mcp.capabilities import runner_jobs as RJ
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

ORG = 42



@pytest.fixture(autouse=True)
def _l_appelant_possede_l_agent(monkeypatch):
    """Ce fichier ne parle pas du PARTAGE d'agents — il a son banc
    (`test_partage_agents.py`). L'appelant y est lu propriétaire de tout agent
    qu'on lui présente ; sans cette doublure, la lecture de rôle irait chercher la
    vraie base."""
    from oto_mcp.capabilities import _acces_agent
    monkeypatch.setattr(_acces_agent, "niveaux",
                        lambda sub, org_id, agents: {int(t["id"]): "owner" for t in agents})

def _reglages(monkeypatch, poses=None):
    """`poses` : {(portee, fournisseur): 'true'|'false'} — portee = 'org' | 'platform'."""
    poses = poses or {}
    def lire(portee, ident, fournisseur, cle):
        assert cle == CE.CLE_REGLAGE
        if portee == "org" and ident != str(ORG):
            return None
        return poses.get((portee, fournisseur))
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting", lire)


def _poses(**kw):
    """`platform__anthropic="true"` → {("platform", "anthropic"): "true"}."""
    return {tuple(k.split("__")): v for k, v in kw.items()}


# ── 1–2. le réglage ──────────────────────────────────────────────────────────

def test_rien_n_est_exige_tant_que_personne_n_a_rien_pose(monkeypatch):
    _reglages(monkeypatch)
    assert CE.cle_exigee(ORG, "anthropic") is False


def test_la_plateforme_exige(monkeypatch):
    _reglages(monkeypatch, _poses(platform__anthropic="true"))
    assert CE.cle_exigee(ORG, "anthropic") is True
    assert CE.cle_exigee(ORG, "mistral") is False, "par fournisseur, pas en bloc"


def test_l_org_l_emporte_sur_la_plateforme(monkeypatch):
    """L'exemption : nos propres orgs pendant la bascule, ou un client qu'on sert
    encore sur notre clé le temps qu'il dépose la sienne."""
    _reglages(monkeypatch, _poses(platform__anthropic="true", org__anthropic="false"))
    assert CE.cle_exigee(ORG, "anthropic") is False


def test_les_fournisseurs_de_modele_se_derivent_du_registre():
    assert {"anthropic", "mistral"} <= set(CE.fournisseurs_de_modele())
    assert "folk" not in CE.fournisseurs_de_modele(), "un connecteur ordinaire n'en est pas"


# ── 3–4. à la réservation ────────────────────────────────────────────────────

@pytest.fixture
def arret(monkeypatch):
    vu = {}
    monkeypatch.setattr(RJ.db, "arreter_definitivement",
                        lambda job_id, sub, raison: vu.update(job=job_id, raison=raison) or True)
    return vu


def _travail(**kw):
    # Un travail porte la famille de son modèle déclaré : sans elle, il est arrêté
    # avant toute question de clé (24/09/2026, banc `test_agent_heberge_modele_
    # obligatoire.py`) — ce fichier-ci parle de la clé.
    return {"id": 9, "org_id": ORG, "sub": "alexis", "delegated_token": "otd_x",
            "payload": {"model_family": "anthropic"}, **kw}


def _reserver(depot, *, worker=True):
    return RJ._avec_cle(_travail(), depot, "worker:banc", worker=worker)


def test_exigee_et_absente_le_travail_est_ARRETE_pour_de_bon(monkeypatch, arret):
    """⚠️ LE banc du lot. Sans lui, le travail partirait sans clé et le worker
    tournerait sur la nôtre."""
    _reglages(monkeypatch, _poses(platform__anthropic="true"))
    monkeypatch.setattr(RJ, "_cle_de_modele", lambda org, depot: (None, None))
    rendu = _reserver("anthropic")
    assert arret["job"] == 9, "marqué failed en base, pas relâché dans la file"
    assert rendu["delegation_refusee"] and "anthropic" in rendu["delegation_refusee"]
    assert "model_key" not in rendu
    assert rendu["delegated_token"] is None, "un travail qui ne tournera pas n'a pas de pouvoir"


def test_exigee_et_deposee_la_cle_part_avec_le_travail(monkeypatch, arret):
    _reglages(monkeypatch, _poses(platform__anthropic="true"))
    monkeypatch.setattr(RJ, "_cle_de_modele", lambda org, depot: ("sk-de-l-org", None))
    rendu = _reserver("anthropic")
    assert rendu["model_key"] == "sk-de-l-org"
    assert "delegation_refusee" not in rendu and not arret


def test_un_coffre_qui_ne_rend_pas_la_cle_ARRETE_aussi(monkeypatch, arret):
    """La présence du dépôt ne suffit pas : c'est la LECTURE qui dit si la clé part.
    Un coffre muet laisserait sinon filer un travail sur notre clé."""
    _reglages(monkeypatch, _poses(platform__anthropic="true"))
    monkeypatch.setattr(RJ, "_cle_de_modele", lambda org, depot: (None, None))
    monkeypatch.setattr(CE, "cle_deposee", lambda org, f: True)
    assert _reserver("anthropic")["delegation_refusee"]


def test_eteint_rien_ne_change(monkeypatch, arret):
    """Le comportement d'avant, à l'octet : sans clé, le travail part sans clé."""
    _reglages(monkeypatch)
    monkeypatch.setattr(RJ, "_cle_de_modele", lambda org, depot: (None, None))
    rendu = _reserver("anthropic")
    assert "delegation_refusee" not in rendu and "model_key" not in rendu and not arret
    assert rendu["delegated_token"] == "otd_x"


def test_un_worker_SANS_depot_ne_sert_pas_une_org_qui_exige_sa_cle(monkeypatch, arret):
    """Il tournerait sur la clé de son environnement quoi que l'org ait déposé — la
    présence du dépôt n'y change rien, il ne le consommera pas."""
    _reglages(monkeypatch, _poses(platform__anthropic="true"))
    monkeypatch.setattr(CE, "cle_deposee", lambda org, f: True)
    rendu = _reserver("")
    assert rendu["delegation_refusee"] and "aucun fournisseur" in rendu["delegation_refusee"]
    assert arret["job"] == 9


def test_un_worker_sans_depot_sert_toujours_une_org_qui_n_exige_rien(monkeypatch, arret):
    _reglages(monkeypatch)
    assert "delegation_refusee" not in _reserver("") and not arret


def test_un_MEMBRE_qui_reserve_n_est_pas_arrete(monkeypatch, arret):
    """La garde vise la clé de NOTRE environnement, que seul un worker de plateforme
    porte. Un membre qui réserve tourne sur ce qu'il a."""
    _reglages(monkeypatch, _poses(platform__anthropic="true"))
    monkeypatch.setattr(RJ, "_depot_pose", lambda org, depot: False)
    assert "delegation_refusee" not in _reserver("anthropic", worker=False) and not arret


def test_un_refus_d_IDENTITE_garde_sa_raison(monkeypatch, arret):
    """Deux refus, un seul motif rendu : l'identité passe avant la clé — elle dit
    quoi réparer en premier, et le travail est déjà arrêté par elle."""
    _reglages(monkeypatch, _poses(platform__anthropic="true"))
    job = _travail(delegation_refusee="le compte n'existe plus")
    rendu = RJ._avec_cle(job, "anthropic", "worker:banc", worker=True)
    assert rendu["delegation_refusee"] == "le compte n'existe plus" and not arret


# ── 5. à la pose ─────────────────────────────────────────────────────────────

def _ctx():
    return ResolvedCtx(sub="alexis", org_id=ORG)


def test_poser_un_agent_sans_la_cle_exigee_est_refuse_LISIBLEMENT(monkeypatch):
    """Le modèle DÉCLARÉ est de la famille exigée (anthropic) : c'est le vrai
    positif que la garde doit toujours tenir, modèle déclaré compris."""
    _reglages(monkeypatch, _poses(platform__anthropic="true"))
    monkeypatch.setattr(CE, "cle_deposee", lambda org, f: False)
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: {"armed": True, "workers": 1, "last_seen": None,
                                     "families": ["anthropic"]})
    monkeypatch.setattr(RT.db, "create_trigger",
                        lambda *a, **k: pytest.fail("un refus n'écrit rien"))
    with pytest.raises(AuthzDenied) as e:
        asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(op="create", procedure="veille",
                                             cron="5 6 * * *", tools=["a"],
                                             model="claude-opus-5")))
    assert (e.value.status, e.value.code) == (400, "model_key_required")
    assert "anthropic" in e.value.message


def test_rallumer_sans_la_cle_exigee_est_refuse(monkeypatch):
    _reglages(monkeypatch, _poses(platform__anthropic="true"))
    monkeypatch.setattr(CE, "cle_deposee", lambda org, f: False)
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: {"armed": True, "workers": 1, "last_seen": None,
                                     "families": ["anthropic"]})
    monkeypatch.setattr(RT.db, "get_trigger",
                        lambda tid, org: {"id": tid, "model": "claude-opus-5"})
    monkeypatch.setattr(RT.db, "update_trigger",
                        lambda *a, **k: pytest.fail("un refus n'écrit rien"))
    with pytest.raises(AuthzDenied) as e:
        asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(op="update", trigger_id=3, enabled=True)))
    assert e.value.code == "model_key_required"


def test_armer_un_passage_sans_la_cle_exigee_est_refuse(monkeypatch):
    from oto_mcp import roles
    _reglages(monkeypatch, _poses(platform__anthropic="true"))
    monkeypatch.setattr(CE, "cle_deposee", lambda org, f: False)
    monkeypatch.setattr(roles, "is_org_admin", lambda *a, **k: True)
    monkeypatch.setattr(RF, "_run_courant", lambda: None)
    monkeypatch.setattr(RF.db, "get_fleet", lambda fid, org: {"model": "claude-opus-5"})
    monkeypatch.setattr(RF.db, "armer", lambda *a, **k: pytest.fail("un refus n'arme rien"))
    with pytest.raises(AuthzDenied) as e:
        RF._fleets(_ctx(), RF.FleetInput(op="launch", fleet_id=1))
    assert e.value.code == "model_key_required"


def test_creer_un_passage_ANTHROPIC_sans_sa_cle_est_refuse(monkeypatch):
    """La CRÉATION (14/09/2026) est l'un des trois moments de pose que le module
    documente — jusqu'ici sans la moindre garde. Nouveau chemin, nouveau banc."""
    _reglages(monkeypatch, _poses(platform__anthropic="true"))
    monkeypatch.setattr(CE, "cle_deposee", lambda org, f: False)
    monkeypatch.setattr(RF.db, "create_fleet",
                        lambda *a, **k: pytest.fail("un refus n'écrit rien"))
    with pytest.raises(AuthzDenied) as e:
        RF._fleets(_ctx(), RF.FleetInput(op="create", label="p", procedure="p",
                                         tools=["a"], model="claude-opus-5"))
    assert e.value.code == "model_key_required"


# ── 6. seule la famille DU MODÈLE DÉCLARÉ compte, jamais « tout ce qui est
#      exigé » ────────────────────────────────────────────────────────────────
# Régression de production, mesurée le 14/09/2026 : `runner.org_key_required=
# true` posé pour `anthropic` a refusé la pose de flottes déclarées
# `mistral-large-2512` (cinq campagnes de l'opérateur des campagnes), dans une org qui n'avait — à
# raison — déposé aucune clé Anthropic. `exiger_a_la_pose` regardait TOUTES les
# familles exigées, pas celle du modèle qu'on posait réellement.

def test_seule_la_famille_du_modele_declare_est_verifiee(monkeypatch):
    """L'épreuve de chute : remplacer le corps de `exiger_a_la_pose` par
    `manquantes(org_id)` (sans borner à `[famille]`, comme avant le 14/09) fait
    ROUGIR ce banc sur la ligne `mistral` — la preuve qu'il exerce la
    régression, pas une autre."""
    _reglages(monkeypatch, _poses(platform__anthropic="true"))
    monkeypatch.setattr(CE, "cle_deposee", lambda org, f: False)  # aucune clé, nulle part
    # anthropic EST exigée et absente : la famille déclarée en pâtit.
    with pytest.raises(AuthzDenied) as e:
        CE.exiger_a_la_pose(ORG, "anthropic")
    assert e.value.code == "model_key_required"
    # mistral n'est PAS exigée : la déclarer ne lève pas, même sans AUCUNE clé
    # déposée pour aucun fournisseur — c'est exactement le cas de l'opérateur des campagnes.
    CE.exiger_a_la_pose(ORG, "mistral")


def test_sans_modele_declare_rien_n_est_exige(monkeypatch):
    """Cette fonction ne juge que la famille qu'on lui donne. Un agent SANS modèle
    ne l'atteint plus : il est refusé à la pose (`_modele.exige_un_modele`) et
    arrêté à la réservation (24/09/2026)."""
    _reglages(monkeypatch, _poses(platform__anthropic="true"))
    monkeypatch.setattr(CE, "cle_deposee", lambda org, f: False)
    CE.exiger_a_la_pose(ORG, None)


def test_un_declencheur_MISTRAL_se_pose_meme_sans_cle_anthropic(monkeypatch):
    """Le cas exact de la régression, de bout en bout par la capacité — pas
    seulement la fonction isolée : un agent Mistral se pose dans une org qui
    n'a AUCUNE clé déposée, sous un réglage `anthropic` allumé."""
    _reglages(monkeypatch, _poses(platform__anthropic="true"))
    monkeypatch.setattr(CE, "cle_deposee", lambda org, f: False)
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: {"armed": True, "workers": 1, "last_seen": None,
                                     "families": ["mistral"]})
    monkeypatch.setattr(RT.db, "triggers_for_procedure", lambda o, p: [])
    monkeypatch.setattr(RT.db, "create_trigger", lambda *a, **k: {"id": 1})
    # Ne lève PAS : seul `anthropic` est exigé, l'agent déclare `mistral`.
    asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(op="create", procedure="veille",
                                         cron="5 6 * * *", tools=["a"],
                                         model="mistral-large-2512")))


def test_eteint_poser_un_agent_sans_cle_passe(monkeypatch):
    _reglages(monkeypatch)
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: {"armed": True, "workers": 1, "last_seen": None,
                                     "families": ["anthropic"]})
    monkeypatch.setattr(RT.db, "triggers_for_procedure", lambda o, p: [])
    monkeypatch.setattr(RT.db, "create_trigger", lambda *a, **k: {"id": 1})
    asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(op="create", procedure="veille",
                                         cron="5 6 * * *", tools=["a"],
                                         model="claude-sonnet-5")))


# ── la console admin refuse ce que la réservation lirait de travers ──────────

@pytest.mark.parametrize("valeur", ["True", "oui", "1", None])
def test_une_valeur_autre_que_true_false_est_refusee(monkeypatch, valeur):
    """`True` se lirait FAUX à la réservation : une org qu'on croit contrainte
    continuerait de tourner sur notre clé, sans une erreur."""
    from oto_mcp.capabilities import platform_connectors as PC
    monkeypatch.setattr("oto_mcp.db.connector_settings.set_connector_setting",
                        lambda *a, **k: pytest.fail("un refus n'écrit rien"))
    with pytest.raises(AuthzDenied) as e:
        PC._connector_setting(_ctx(), PC.ConnectorSettingInput(
            op="set", connector="anthropic", key=CE.CLE_REGLAGE, value=valeur))
    assert e.value.code == "invalid_setting"


def test_le_reglage_ne_se_pose_pas_sur_un_connecteur_ordinaire(monkeypatch):
    from oto_mcp.capabilities import platform_connectors as PC
    monkeypatch.setattr("oto_mcp.db.connector_settings.set_connector_setting",
                        lambda *a, **k: pytest.fail("un refus n'écrit rien"))
    with pytest.raises(AuthzDenied) as e:
        PC._connector_setting(_ctx(), PC.ConnectorSettingInput(
            op="set", connector="folk", key=CE.CLE_REGLAGE, value="true"))
    assert e.value.code == "invalid_setting"
