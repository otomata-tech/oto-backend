"""L'écran Admin des causes de panne d'agent (`capabilities/agent_causes_admin.py`), sur
une vraie base.

Ce qu'on prouve : la liste dit ce qu'une cause couvre et se filtre par statut ; approuver
une cause du côté du titulaire exige sa copie ; une copie illisible pour le client est
refusée ; la copie se fusionne par langue ; un agent hébergé ne passe pas par ici pour
approuver ; une clé inconnue est un 404.
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities import agent_causes_admin as C
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

OPS = ResolvedCtx(sub="ops")


@pytest.fixture(autouse=True)
def base(live):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("DELETE FROM agent_causes WHERE cle LIKE 'adm-%%'")
    from oto_mcp.db import causes_agents
    causes_agents.relever([("adm-cle", "clé <id> refusée", "inconnu", "proprietaire"),
                           ("adm-nous", "HTTP <n> overloaded", "refus_passager",
                            "plateforme")])


def _poser(**kw):
    return C._decider(OPS, C.AgentCauseSetInput(**kw))["cause"]


def test_la_liste_dit_ce_que_couvre_une_cause_et_se_filtre(live):
    cles = {c["cle"] for c in C._lister(OPS, C.AgentCausesInput())["causes"]}
    assert {"adm-cle", "adm-nous"} <= cles
    _poser(cause_key="adm-nous", status="approuvee")
    approuvees = C._lister(OPS, C.AgentCausesInput(status="approuvee"))["causes"]
    assert "adm-nous" in {c["cle"] for c in approuvees}
    assert {"episodes", "vivants", "orgs", "classes_vues"} <= set(approuvees[0])


def test_approuver_une_cause_du_titulaire_exige_sa_copie(live):
    with pytest.raises(AuthzDenied) as e:
        _poser(cause_key="adm-cle", status="approuvee")
    assert e.value.code == "client_copy_required"
    c = _poser(cause_key="adm-cle", status="approuvee", classe="cle_refusee",
               client_copy={"fr": {"headline": "La clé de l'outil d'emailing est refusée.",
                                   "action": "connectors"}})
    assert (c["statut"], c["classe"]) == ("approuvee", "cle_refusee")
    c = _poser(cause_key="adm-cle", client_copy={"en": {"headline": "The key is refused."}})
    assert set(c["copie"]) == {"fr", "en"}, "une langue posée garde l'autre"
    assert [g["acteur"] for g in c["gestes"]] == ["ops", "ops"]


def test_une_copie_que_le_client_ne_peut_pas_lire_est_refusee(live):
    with pytest.raises(AuthzDenied) as e:
        _poser(cause_key="adm-cle", client_copy={"fr": {"headline": "Erreur lemlist_act."}})
    assert e.value.code == "client_copy_refused"


def test_un_agent_heberge_n_approuve_pas(live, monkeypatch):
    from oto_mcp.capabilities import _outils_plateforme
    monkeypatch.setattr(_outils_plateforme, "_depuis_un_agent_heberge", lambda: True)
    with pytest.raises(AuthzDenied) as e:
        _poser(cause_key="adm-nous", status="approuvee")
    assert e.value.code == "approval_by_operator_only"


def test_une_cle_inconnue_est_un_404(live):
    with pytest.raises(AuthzDenied) as e:
        _poser(cause_key="adm-absente", status="refusee")
    assert e.value.status == 404
