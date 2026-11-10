"""Soigner un agent avant qu'on le remarque (`medecin_agents.py`) — sans base.

Ce qui fait que la boucle répare sans nuire : la classe se lit sur les motifs RÉELS de
la file ; seule une panne de la PLATEFORME se relance, une fois par travail mort,
`RELANCES_MAX` fois par épisode, jamais par-dessus une occurrence qui attend ni au-delà
de `AGE_MAX` ; la trace s'écrit AVANT l'enfilage, et un refus de la trace n'enfile rien ;
une panne du titulaire s'escalade sans geste ; une org d'un tenant tiers n'est pas
relevée."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from oto_mcp import medecin_agents as M

_MAINTENANT = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("motif, attendu", [
    # Les motifs relevés en production (forme exacte, tronquée comme la file la garde).
    # « … ne tournent que sur la clé déposée … » : une clé que le TITULAIRE n'a pas
    # déposée, jamais une famille que nous ne servons pas.
    ("ce travail demande un modèle `anthropic`, et les agents `anthropic` ne tournent "
     "que sur la clé déposée par l'organisation — celle-ci n'en a pas déposé.",
     ("cle_refusee", "proprietaire")),
    ("Aucun worker ne sert la famille `mistral` (aucun worker vu)",
     ("famille_non_servie", "plateforme")),
    ("fin_anormale (cle_sans_workspace, HTTP 400 : clé d'organisation Anthropic : rens",
     ("cle_refusee", "proprietaire")),
    ('RuntimeError : agent de la ferme : 422 {"erreur": "clé d\'API invalide : une clé ',
     ("cle_refusee", "proprietaire")),
    ("HTTP 401 invalid x-api-key", ("cle_refusee", "proprietaire")),
    ("identité invalide — le porteur a quitté l'org", ("identite", "proprietaire")),
    ("HTTP 529 overloaded_error", ("refus_passager", "plateforme")),
    ("HTTP 503 Service Unavailable", ("refus_passager", "plateforme")),
    # Un 429 de quota épuisé n'est PAS passager : la règle de la file le dit déjà.
    ("HTTP 429 insufficient_quota", ("cle_refusee", "proprietaire")),
    # L'ancien libellé d'un refus du fournisseur : jamais relancé d'office.
    ("fin_anormale (success)", ("inconnu", "inconnu")),
    (None, ("inconnu", "inconnu")),
])
def test_la_classe_se_lit_sur_les_motifs_reels(motif, attendu):
    assert M.classer(motif) == attendu


def _tete(motif: str) -> str:
    """Le motif tel que `pannes_en_cours` le rend : tronqué à `MOTIF_TETE`."""
    from oto_mcp.db.alertes_agents import MOTIF_TETE
    return motif[:MOTIF_TETE]


@pytest.mark.parametrize("depot", ["anthropic", "openai", "mistral", "openrouter"])
def test_une_cle_non_deposee_est_du_cote_du_titulaire(depot):
    """Les deux refus « clé non déposée » de la réservation, FORMATÉS par le code qui
    les écrit, puis tronqués comme la file les rend : du côté du titulaire, donc
    l'alerte lui écrit."""
    from oto_mcp.capabilities import _cle_exigee
    from oto_mcp.capabilities import runner_jobs as RJ

    assert M.classer(_tete(RJ._SANS_CLE_DEPOSEE.format(depot=depot))) == (
        "cle_refusee", "proprietaire")
    assert M.classer(_tete(_cle_exigee.raison_du_refus([depot]))) == (
        "cle_refusee", "proprietaire")


def test_une_famille_non_servie_se_lit_sur_le_texte_du_code(monkeypatch):
    from oto_mcp.capabilities import _abonnement

    monkeypatch.setattr(_abonnement.db_runner_jobs, "dernier_vu_de_famille",
                        lambda org, famille: None)
    base = "occurrence non prise dans son cycle : le déclencheur a enfilé la suivante."
    assert M.classer(_tete(_abonnement._famille_non_servie(base, 7, "mistral"))) == (
        "famille_non_servie", "plateforme")


def _panne(**kw):
    p = {"org_id": 7, "trigger_id": 55, "model": "claude-sonnet-5", "job_id": 900,
         "kind": "start", "payload": {"trigger_id": 55, "_plateforme": {"x": 1}},
         "sub": "porteur", "dernier": _MAINTENANT - timedelta(minutes=20), "travaux": 1,
         "motif": "HTTP 529 overloaded_error"}
    p.update(kw)
    return p


def _episode(**kw):
    e = {"id": 3, "org_id": 7, "trigger_id": 55, "classe": "refus_passager",
         "cote": "plateforme", "statut": "ouvert", "reparations": []}
    e.update(kw)
    return e


@pytest.fixture()
def file(monkeypatch):
    from oto_mcp import db
    from oto_mcp.db import episodes_agents

    etat = {"enfiles": [], "notes": [], "poses": [], "signaux": [],
            "attente": {"pending": 0, "held": 0, "claimed": 0},
            "garde": True, "familles": ["anthropic"],
            # L'épisode relu après un refus de la trace : par défaut, plafond atteint.
            "relu": _episode(statut="en_soin", reparations=[
                {"action": "relance", "job_id": 1}, {"action": "relance", "job_id": 2}])}
    monkeypatch.setattr(db, "file_du_declencheur", lambda t, o: dict(etat["attente"]))
    monkeypatch.setattr(db, "enqueue_job", lambda org, kind, payload=None, sub=None, **k:
                        etat["enfiles"].append((org, kind, payload, sub)) or {"id": 1})
    monkeypatch.setattr(db, "runner_arme", lambda o: {"families": etat["familles"]})

    def _noter(eid, **kw):
        etat["notes"].append((eid, kw))
        return {"id": eid} if etat["garde"] else None
    monkeypatch.setattr(episodes_agents, "noter_reparation", _noter)
    monkeypatch.setattr(episodes_agents, "poser",
                        lambda eid, **kw: etat["poses"].append((eid, kw)) or {"id": eid})
    monkeypatch.setattr(episodes_agents, "lire", lambda eid: etat["relu"])
    import sentry_sdk
    monkeypatch.setattr(sentry_sdk, "capture_message",
                        lambda message, level=None: etat["signaux"].append((message, level)))
    return etat


def test_un_refus_passager_se_relance_une_fois_trace_d_abord(file):
    assert M._soigner(_panne(), _episode(), _MAINTENANT) == "relance"
    assert file["notes"] == [(3, {"action": "relance", "acteur": "medecin", "job_id": 900,
                                  "detail": "relance du travail 900 (refus_passager)",
                                  "maximum": M.RELANCES_MAX})]
    org, kind, payload, sub = file["enfiles"][0]
    assert (org, kind, sub) == (7, "start", "porteur")
    assert payload["trigger_id"] == 55, "la même charge, rendue à `enqueue_job` qui l'épure"


def test_un_travail_deja_relance_ne_l_est_pas_deux_fois(file):
    ep = _episode(reparations=[{"action": "relance", "job_id": 900}])
    assert M._soigner(_panne(), ep, _MAINTENANT) is None
    assert file["enfiles"] == [] and file["notes"] == []


@pytest.mark.parametrize("attente", [{"pending": 1, "held": 0, "claimed": 0},
                                     {"pending": 0, "held": 1, "claimed": 0},
                                     {"pending": 0, "held": 0, "claimed": 1}])
def test_une_occurrence_qui_attend_ou_tourne_bloque_la_relance(file, attente):
    # `claimed` : un worker tient déjà une occurrence — relancer doublerait un run en vol.
    file["attente"] = attente
    assert M.relancer(_panne(), _episode(), maintenant=_MAINTENANT) == \
        "une occurrence de l'agent attend déjà dans la file, ou tourne"
    assert file["enfiles"] == [] and file["notes"] == []


def test_un_travail_mort_trop_ancien_ne_se_relance_pas(file):
    vieux = _panne(dernier=_MAINTENANT - M.AGE_MAX - timedelta(minutes=1))
    assert "trop ancien" in M.relancer(vieux, _episode(), maintenant=_MAINTENANT)
    assert file["enfiles"] == []


def test_la_garde_de_l_episode_refuse_donc_rien_n_est_enfile(file):
    # Plafond atteint (ou un autre processus a gagné) : la trace refuse, le geste n'a pas lieu,
    # et l'épisode passe à un humain — chez nous, pas chez le titulaire.
    file["garde"] = False
    assert M._soigner(_panne(), _episode(), _MAINTENANT) is None
    assert file["enfiles"] == []
    assert file["poses"] == [(3, {"statut": "escalade",
                                  "diagnostic": f"{M.RELANCES_MAX} relances sans guérison"})]
    # Et l'exploitation l'apprend ailleurs qu'au journal : le suivi d'erreurs.
    [(message, niveau)] = file["signaux"]
    assert niveau == "error" and "agent 55 (org 7)" in message and "escaladé" in message


def test_un_travail_relance_par_un_autre_processus_n_escalade_pas(file):
    # La garde en base a refusé parce que CE travail vient d'être relancé ailleurs :
    # ce n'est pas une panne qui résiste, rien ne s'escalade.
    file["garde"] = False
    file["relu"] = _episode(statut="en_soin", reparations=[{"action": "relance",
                                                            "job_id": 900}])
    assert M.relancer(_panne(), _episode(), maintenant=_MAINTENANT) == M.REFUS_DEJA_RELANCE
    assert M._soigner(_panne(), _episode(), _MAINTENANT) is None
    assert file["enfiles"] == [] and file["poses"] == [] and file["signaux"] == []


def test_une_famille_non_servie_attend_qu_un_worker_la_serve(file):
    ep = _episode(classe="famille_non_servie")
    file["familles"] = []
    assert M._soigner(_panne(), ep, _MAINTENANT) is None
    assert file["enfiles"] == []
    file["familles"] = ["anthropic"]
    assert M._soigner(_panne(), ep, _MAINTENANT) == "relance"


def test_une_panne_du_titulaire_s_escalade_sans_geste(file):
    ep = _episode(classe="cle_refusee", cote="proprietaire")
    assert M._soigner(_panne(), ep, _MAINTENANT) is None
    assert file["enfiles"] == [] and file["notes"] == []
    assert file["poses"] == [(3, {"statut": "escalade"})]


def test_une_panne_inconnue_reste_ouverte_pour_l_agent_medecin(file):
    ep = _episode(classe="inconnu", cote="inconnu")
    assert M._soigner(_panne(), ep, _MAINTENANT) is None
    assert file["enfiles"] == [] and file["poses"] == []


@pytest.fixture()
def tenants(monkeypatch):
    """L'org 9 est celle d'un tenant tiers ; les autres sont à nous."""
    from oto_mcp import db, tenancy
    monkeypatch.setattr(M, "_ECARTEES_DITES", set())
    lus = []

    def _slug(o):
        lus.append(int(o))
        return "partenaire" if int(o) == 9 else tenancy.primary_slug()
    monkeypatch.setattr(db, "org_tenant_slug", _slug)
    return lus


def test_un_passage_ne_s_arrete_pas_sur_une_panne_illisible(monkeypatch, file, tenants):
    from oto_mcp.db import episodes_agents

    monkeypatch.setattr(episodes_agents, "clore_les_gueris",
                        lambda: [{"id": 1, "reparations": [{"action": "relance"}]}])
    monkeypatch.setattr(episodes_agents, "pannes_en_cours",
                        lambda: [_panne(trigger_id=1), _panne(trigger_id=2)])
    appels = []

    def _ouvrir(**kw):
        appels.append(kw["trigger_id"])
        if kw["trigger_id"] == 1:
            raise RuntimeError("ligne illisible")
        return _episode(trigger_id=2)
    monkeypatch.setattr(episodes_agents, "ouvrir_ou_rafraichir", _ouvrir)
    bilan = M.balayer(_MAINTENANT)
    assert appels == [1, 2]
    assert bilan == {"pannes": 2, "relances": 1, "gueris": 1, "gueris_apres_soin": 1,
                     "erreurs": 1, "tenant_tiers_ecartes": 0}


def test_l_org_d_un_tenant_tiers_n_est_ni_relevee_ni_relancee(monkeypatch, file, tenants):
    """La règle de l'alerte (`alerte_agents_bloques.org_a_nous`), pas une copie : une
    panne d'une org de tenant tiers n'ouvre aucun épisode et ne relance rien."""
    from oto_mcp.db import episodes_agents

    monkeypatch.setattr(episodes_agents, "clore_les_gueris", lambda: [])
    monkeypatch.setattr(episodes_agents, "pannes_en_cours", lambda: [
        _panne(org_id=9, trigger_id=1), _panne(org_id=9, trigger_id=2),
        _panne(org_id=7, trigger_id=3)])
    ouverts = []
    monkeypatch.setattr(episodes_agents, "ouvrir_ou_rafraichir",
                        lambda **kw: ouverts.append(kw["org_id"]) or _episode(
                            org_id=kw["org_id"], trigger_id=kw["trigger_id"]))
    bilan = M.balayer(_MAINTENANT)
    assert ouverts == [7]
    assert [org for org, *_ in file["enfiles"]] == [7]
    assert (bilan["pannes"], bilan["relances"], bilan["tenant_tiers_ecartes"]) == (1, 1, 1)
    assert tenants == [9, 7], "un tenant lu une fois par org et par passage"


def test_un_tenant_illisible_ecarte_l_org(monkeypatch, file):
    from oto_mcp import db
    from oto_mcp.db import episodes_agents

    def _casse(o):
        raise RuntimeError("base indisponible")
    monkeypatch.setattr(db, "org_tenant_slug", _casse)
    monkeypatch.setattr(M, "_ECARTEES_DITES", set())
    monkeypatch.setattr(episodes_agents, "clore_les_gueris", lambda: [])
    monkeypatch.setattr(episodes_agents, "pannes_en_cours", lambda: [_panne()])
    monkeypatch.setattr(episodes_agents, "ouvrir_ou_rafraichir",
                        lambda **kw: pytest.fail("aucun épisode sur un tenant illisible"))
    assert M.balayer(_MAINTENANT)["tenant_tiers_ecartes"] == 1
    assert file["enfiles"] == []


@pytest.mark.parametrize("dernier", ["2026-11-09 08:51:38", "2026-11-09T08:51:38+00:00"])
def test_un_horodatage_en_chaine_ne_casse_pas_la_relance(file, dernier):
    """`oto_admin_agent_repair op=rerun` passe `finished_at` tel que `db.get_job` le
    rend : une CHAÎNE (`_str_dict_row`). `maintenant - str` levait `TypeError` →
    « Internal server error » à chaque relance manuelle."""
    from datetime import datetime as _dt
    maintenant = _dt(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    assert M.relancer(_panne(dernier=dernier), _episode(), maintenant=maintenant) is None
    assert len(file["enfiles"]) == 1


def test_un_horodatage_en_chaine_trop_ancien_refuse(file):
    from datetime import datetime as _dt
    maintenant = _dt(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    assert "trop ancien" in M.relancer(_panne(dernier="2026-11-09 08:51:38"), _episode(),
                                       maintenant=maintenant)
    assert file["enfiles"] == []


def test_un_horodatage_illisible_refuse_la_relance(file):
    assert "illisible" in M.relancer(_panne(dernier="pas une date"), _episode(),
                                     maintenant=_MAINTENANT)
    assert file["enfiles"] == []
