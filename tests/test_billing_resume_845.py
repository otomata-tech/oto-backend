"""Annuler une résiliation — l'inverse qui n'existait pas (#845 ②).

L'écran de facturation annonçait la date de bascule vers le palier gratuit sans offrir
de revenir en arrière : **un clic de trop était définitif jusqu'à la fin de la période.**

⚠️ **Aucun appel au prestataire de paiement, et c'est ce qui rend ce lot sûr** :
résilier ne révoque pas le mandat, et l'abonnement reste `active` jusqu'à l'échéance.
Reprendre défait deux écritures locales — rien qui touche l'encaissement. Ces bancs
l'exigent explicitement, pour qu'un ajout futur ne glisse pas un appel PSP ici.
"""
from __future__ import annotations

import pytest

from oto_mcp import billing


@pytest.fixture()
def sub(monkeypatch):
    """Un abonnement résilié à fin de période : `active`, `canceled_at` posé."""
    etat = {"status": "active", "canceled_at": "2026-09-05T10:00:00Z"}
    repris = {"n": 0}
    monkeypatch.setattr(billing.db_billing, "get_org_subscription", lambda org: dict(etat))

    def _resume(org):
        repris["n"] += 1
        return True
    monkeypatch.setattr(billing.db_billing, "resume_canceled", _resume)
    monkeypatch.setattr(billing, "status", lambda org: {"ok": True})
    return etat, repris


def test_une_resiliation_se_reprend(sub):
    etat, repris = sub
    assert billing.resume(7) == {"ok": True}
    assert repris["n"] == 1


def test_aucun_appel_au_PRESTATAIRE_de_paiement(sub, monkeypatch):
    """⚠️ Le banc qui garde la sûreté du lot. Résilier n'a rien révoqué : reprendre
    n'a donc rien à encaisser, rien à autoriser, personne à appeler. Si quelqu'un
    glisse un appel PSP dans ce chemin, il le saura ici."""
    from oto_mcp import mollie_client

    def _interdit(*a, **k):
        raise AssertionError("appel au prestataire de paiement depuis une reprise")
    for nom in ("create_first_payment", "create_recurring_payment", "revoke_mandate",
                "valid_mandate", "list_mandates", "get_payment"):
        monkeypatch.setattr(mollie_client, nom, _interdit, raising=False)
    assert billing.resume(7) == {"ok": True}


def test_une_periode_ECHUE_refuse_la_reprise(sub, monkeypatch):
    """La garde de symétrie. Le sweep du runner a basculé le statut : reprendre
    rouvrirait l'entitlement sans qu'aucune échéance ne soit tirée — un abonnement
    gratuit créé par un bouton « annuler la résiliation »."""
    monkeypatch.setattr(billing.db_billing, "get_org_subscription",
                        lambda org: {"status": "canceled", "canceled_at": "x"})
    with pytest.raises(ValueError) as e:
        billing.resume(7)
    assert "already_ended" in str(e.value)
    assert "souscription" in str(e.value), "le refus doit dire par où passer"


def test_un_abonnement_NON_resilie_ne_se_reprend_pas(sub, monkeypatch):
    """Rien à annuler : le dire plutôt que de rendre un succès qui n'a rien fait."""
    monkeypatch.setattr(billing.db_billing, "get_org_subscription",
                        lambda org: {"status": "active", "canceled_at": None})
    with pytest.raises(ValueError) as e:
        billing.resume(7)
    assert "not_canceled" in str(e.value)


def test_sans_abonnement_le_refus_le_dit(sub, monkeypatch):
    monkeypatch.setattr(billing.db_billing, "get_org_subscription", lambda org: None)
    with pytest.raises(ValueError) as e:
        billing.resume(7)
    assert "not_subscribed" in str(e.value)


def test_une_course_avec_le_runner_est_DITE_pas_avalee(sub, monkeypatch):
    """La lecture disait « reprenable », l'écriture n'a rien touché : le runner est
    passé entre les deux. Rendre un succès ferait croire à une reprise qui n'a pas eu
    lieu — c'est le seul endroit où deux écrivains se disputent cette ligne."""
    monkeypatch.setattr(billing.db_billing, "resume_canceled", lambda org: False)
    with pytest.raises(ValueError) as e:
        billing.resume(7)
    assert "already_ended" in str(e.value)


def test_la_route_est_servie_et_reservee_a_l_admin_d_org():
    """Même palier que la résiliation : ce qui défait un geste se garde comme lui."""
    from oto_mcp.capabilities.registry import CAPABILITIES
    caps = {c.key: c for c in CAPABILITIES}
    resume, cancel = caps["billing.resume"], caps["billing.cancel"]
    assert resume.authz is cancel.authz, (
        "reprendre et résilier doivent partager le palier : sinon l'un des deux "
        "devient une porte dérobée sur l'autre")
    assert resume.rest.path == "/api/me/billing/resume"
