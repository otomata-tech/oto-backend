"""Une boucle agentique sans user à impersonner ne tourne pas.

Le worker est un **serveur de boucles agentiques** : chaque boucle impersonne son
user, et le serveur n'a AUCUNE identité métier. Un travail sans porteur n'a donc
personne à impersonner.

Jusqu'ici il était servi nu, et le worker retombait sur son propre jeton : un
agent qui agit au nom du compte qui héberge le runner, et tout ce qu'il écrit
signé par lui. Le défaut est silencieux par construction — les écritures
aboutissent, seule l'attribution est fausse, et rien ne la contredit.
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities import runner_jobs as RJ


@pytest.fixture
def _base(monkeypatch):
    """Ce que la base a vu passer — c'est le REFUS ÉCRIT qui compte."""
    refus = []
    monkeypatch.setattr(RJ.db, "refuser_pour_identite",
                        lambda jid, par, raison: refus.append((jid, par, raison)))
    monkeypatch.setattr(RJ.db, "create_api_token",
                        lambda *a, **k: pytest.fail("aucun jeton ne doit être émis"))
    return refus


def test_un_travail_sans_porteur_est_refuse_et_la_raison_est_ecrite(_base):
    job = RJ._delegue({"id": 7, "org_id": 42, "sub": None}, 600, "le-worker")
    assert "delegation_refusee" in job
    assert "ne nomme personne" in job["delegation_refusee"]
    assert _base == [(7, "le-worker", job["delegation_refusee"])], (
        "le refus se pose EN BASE : sinon le travail repart au worker suivant, "
        "indéfiniment, et rien ne dit pourquoi")


def test_il_n_emporte_ni_jeton_ni_cle(_base):
    """Les deux moyens d'agir tombent ensemble : sans user à impersonner, il n'y a
    ni identité à prêter, ni exécution à payer."""
    job = RJ._avec_cle(RJ._delegue({"id": 8, "org_id": 42}, 600, "w"), "anthropic", "w")
    assert "delegated_token" not in job and "model_key" not in job


def test_le_refus_dit_quoi_faire_pas_seulement_ce_qui_manque(_base):
    """Un refus qui nomme le manque sans nommer la sortie fait rouvrir le
    journal : celui-ci dit de reprogrammer, et pourquoi ça suffit."""
    job = RJ._delegue({"id": 9, "org_id": 42}, 600, "w")
    assert "reprogramme" in job["delegation_refusee"]


def test_un_travail_qui_nomme_son_porteur_passe_comme_avant(monkeypatch):
    """La garde vise l'ABSENCE de porteur, pas la délégation elle-même."""
    monkeypatch.setattr(RJ, "_identite_invalide", lambda sub, org: None)
    monkeypatch.setattr(RJ.db, "purger_delegations_expirees", lambda sub: None)
    monkeypatch.setattr(RJ.db, "create_api_token", lambda *a, **k: "oto_delegue")
    monkeypatch.setattr(RJ.db, "refuser_pour_identite",
                        lambda *a: pytest.fail("aucun refus attendu"))
    job = RJ._delegue({"id": 10, "org_id": 42, "sub": "alexis"}, 600, "w")
    assert job["delegated_token"] == "oto_delegue"
