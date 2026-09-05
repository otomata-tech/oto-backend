"""Changer de moyen de paiement — le geste qui manquait (#845 ①).

**On perdait un abonné payant en silence** : sa carte meurt, toutes les relances
échouent, et le seul geste qui le sauverait n'existait pas. La procédure d'impayé des
conditions de vente courait sur quelqu'un sans aucun moyen d'agir.

⚠️ **CE BANC EST UN CONTRAT, PAS UNE PREUVE.** Il n'existe pas de clé Mollie de test ici
(décision d'Alexis, 05/09/2026) : le client est simulé. On vérifie que NOTRE code fait la
bonne séquence face à un prestataire qui se comporte comme sa documentation le décrit —
rien de plus. Le premier vrai changement se fera en production, sous son œil.

D'où le soin mis à couvrir ce que la doc **tait** autant que ce qu'elle promet : un
encaissement sans mandat, un webhook qui n'arrive jamais, un ancien mandat déjà révoqué,
deux changements ouverts en même temps, un ménage qui rate après la bascule, et une carte
qui refuse l'autorisation à zéro.
"""
from __future__ import annotations

import pytest

from oto_mcp import billing_method as M

ORG = 7
SUB = {"status": "active", "customer_id": "cst_1", "mandate_id": "mdt_ancien",
       "currency": "eur", "plan": "pro"}


class _Mollie:
    """Le prestataire, tel que sa doc le décrit — et tel qu'il déraille."""

    def __init__(self, *, statut="paid", mandat=("mdt_neuf", "creditcard"),
                 revoque_leve=False):
        self.statut, self.mandat, self.revoque_leve = statut, mandat, revoque_leve
        self.crees, self.revoques, self.maj = [], [], []

    def create_first_payment(self, cents, **kw):
        self.crees.append({"cents": cents, **kw})
        return {"id": "tr_neuf", "status": "open",
                "_links": {"checkout": {"href": "https://pay.invalid/x"}}}

    def get_payment(self, pid):
        return {"id": pid, "status": self.statut}

    def valid_mandate(self, customer_id):
        if not self.mandat:
            return None
        mid, methode = self.mandat
        return {"id": mid, "method": methode, "mandateReference": "RUM-1"}

    def revoke_mandate(self, customer_id, mandate_id):
        if self.revoque_leve:
            raise RuntimeError("mandat déjà révoqué")
        self.revoques.append(mandate_id)

    def update_payment(self, pid, **kw):
        self.maj.append((pid, kw))

    checkout_url = staticmethod(lambda p: (p.get("_links", {}).get("checkout") or {}).get("href"))
    method_from_mollie = staticmethod(lambda m: {"creditcard": "card"}.get(m, "card"))


@pytest.fixture()
def monde(monkeypatch):
    """L'org, ses paiements, et un prestataire simulé."""
    etat = {"sub": dict(SUB), "paiements": [], "swaps": []}
    psp = _Mollie()

    monkeypatch.setattr(M.db_billing, "get_org_subscription", lambda o: dict(etat["sub"]))
    monkeypatch.setattr(M.db_billing, "list_billing_payments",
                        lambda o, limit=20: list(etat["paiements"]))
    monkeypatch.setattr(M.db_billing, "insert_billing_payment",
                        lambda o, kind, amount, **kw: etat["paiements"].insert(
                            0, {"id": len(etat["paiements"]) + 1, "kind": kind,
                                "status": kw.get("status"),
                                "payment_intent_id": kw.get("payment_intent_id")}) or 1)
    monkeypatch.setattr(M.db_billing, "update_billing_payment",
                        lambda pid, **kw: None)

    def _swap(org, *, mandate_id, mandate_rum=None, method=None):
        etat["swaps"].append(mandate_id)
        ancien = etat["sub"].get("mandate_id")
        etat["sub"]["mandate_id"] = mandate_id
        return ancien
    monkeypatch.setattr(M.db_billing, "swap_mandate", _swap)
    monkeypatch.setattr(M, "mollie_client", psp)
    monkeypatch.setattr(M.billing, "webhook_url", lambda: "https://h.invalid/wh")
    monkeypatch.setattr(M.billing, "_return_url_with_ref", lambda u, p: f"{u}?ref={p}")
    return etat, psp


# ── ouvrir ────────────────────────────────────────────────────────────────────

def test_le_premier_paiement_est_a_ZERO_et_par_carte(monde):
    """Le choix qui évite un remboursement, donc un avoir, à chaque changement."""
    etat, psp = monde
    out = M.start(ORG, "https://retour.invalid/billing")
    assert psp.crees[0]["cents"] == 0
    assert psp.crees[0]["method"] == "creditcard"
    assert out["checkout_url"] == "https://pay.invalid/x"


def test_l_ouverture_DIT_que_l_ancien_moyen_reste_actif(monde):
    """⚠️ Sans cette phrase, qui abandonne le checkout croit s'être coupé lui-même."""
    out = M.start(ORG, "https://retour.invalid/billing")
    assert "reste actif" in out["notice"]


def test_un_abonnement_CLOS_n_a_pas_de_moyen_a_changer(monde):
    etat, _ = monde
    etat["sub"]["status"] = "canceled"
    with pytest.raises(ValueError) as e:
        M.start(ORG, "https://retour.invalid/b")
    assert "already_ended" in str(e.value)


def test_un_impaye_PEUT_changer_de_moyen(monde):
    """C'est justement quand la carte est morte qu'on vient ici : refuser un
    `past_due` fermerait la porte à ceux pour qui elle existe."""
    etat, psp = monde
    etat["sub"]["status"] = "past_due"
    M.start(ORG, "https://retour.invalid/b")
    assert psp.crees, "un abonnement en impayé doit pouvoir réparer sa carte"


# ── confirmer ─────────────────────────────────────────────────────────────────

def _ouvre(etat, ref="tr_neuf"):
    etat["paiements"].insert(0, {"id": 1, "kind": "method_change", "status": "open",
                                 "payment_intent_id": ref})


def test_la_bascule_puis_la_revocation_de_l_ancien(monde):
    etat, psp = monde
    _ouvre(etat)
    out = M.confirm(ORG, "tr_neuf")
    assert out["status"] == "changed" and out["mandate_id"] == "mdt_neuf"
    assert etat["swaps"] == ["mdt_neuf"]
    assert psp.revoques == ["mdt_ancien"], "l'ancien est révoqué APRÈS la bascule"


def test_encaisse_SANS_mandat_encore_visible_ne_bascule_rien(monde):
    """Le mandat apparaît quelques minutes après chez Mollie : ce n'est pas un échec,
    et l'ancien moyen tient pendant ce temps."""
    etat, psp = monde
    psp.mandat = None
    _ouvre(etat)
    out = M.confirm(ORG, "tr_neuf")
    assert out["status"] == "pending_mandate"
    assert etat["swaps"] == [] and psp.revoques == []
    assert etat["sub"]["mandate_id"] == "mdt_ancien"


def test_le_webhook_qui_n_arrive_JAMAIS_ne_casse_rien(monde):
    """Le paiement reste ouvert : on rend `pending`, l'ancien moyen est intact, et
    rien n'est révoqué. C'est l'état stable — pas une erreur."""
    etat, psp = monde
    psp.statut = "open"
    _ouvre(etat)
    out = M.confirm(ORG, "tr_neuf")
    assert out["status"] == "pending"
    assert etat["swaps"] == [] and psp.revoques == []
    assert "reste actif" in out["notice"]


def test_une_carte_qui_REFUSE_le_zero_laisse_tout_en_place(monde):
    """Toutes les cartes n'acceptent pas l'autorisation à zéro. Celui qui essayait de
    bien faire ne doit rien y perdre — et il doit l'apprendre."""
    etat, psp = monde
    psp.statut = "failed"
    _ouvre(etat)
    out = M.confirm(ORG, "tr_neuf")
    assert out["status"] == "failed"
    assert etat["sub"]["mandate_id"] == "mdt_ancien"
    assert psp.revoques == []
    assert "n'a pas changé" in out["notice"]


def test_la_revocation_qui_ECHOUE_ne_defait_pas_la_bascule(monde):
    """⚠️ Le sens du risque : un ancien mandat qui traîne coûte moins cher qu'une
    bascule annulée parce que le ménage a raté. L'encaissement suivant DOIT prendre le
    nouveau moyen."""
    etat, psp = monde
    psp.revoque_leve = True
    _ouvre(etat)
    out = M.confirm(ORG, "tr_neuf")
    assert out["status"] == "changed"
    assert out["previous_revoked"] is False
    assert etat["sub"]["mandate_id"] == "mdt_neuf", (
        "la bascule tient : c'est le nouveau mandat qui encaissera")


def test_deux_changements_ouverts_le_REF_designe_lequel(monde):
    """Page rechargée, hésitation : plusieurs pages payables coexistent. Sans la
    référence, on confirmerait sur la foi d'un retour qui concerne l'autre."""
    etat, psp = monde
    _ouvre(etat, "tr_vieux")
    _ouvre(etat, "tr_neuf")
    M.confirm(ORG, "tr_vieux")
    assert etat["swaps"] == ["mdt_neuf"]
    with pytest.raises(ValueError) as e:
        M.confirm(ORG, "tr_inconnu")
    assert "unknown_payment" in str(e.value)


def test_rejouer_sur_un_mandat_DEJA_courant_ne_revoque_rien(monde):
    """Idempotence : l'ancien d'aujourd'hui EST le nouveau. Le révoquer couperait le
    moyen de paiement en croyant faire le ménage."""
    etat, psp = monde
    etat["sub"]["mandate_id"] = "mdt_neuf"
    _ouvre(etat)
    out = M.confirm(ORG, "tr_neuf")
    assert out["status"] == "already_current"
    assert psp.revoques == [] and etat["swaps"] == []


def test_sans_changement_en_cours_le_refus_le_dit(monde):
    with pytest.raises(ValueError) as e:
        M.confirm(ORG)
    assert "no_pending_change" in str(e.value)
