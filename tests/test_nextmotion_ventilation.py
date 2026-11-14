"""Nextmotion — la ventilation d'un paiement entre les praticiens de sa facture.

Ce que ce fichier verrouille :
- un paiement qui SOLDE sa facture est réparti au prorata du prix des lignes, par
  praticien ; « solde » se lit dans les paiements de la facture (cumul dans l'ordre de
  `payment_time` égal à `total_price` avec lui), jamais dans un code de statut ;
- un acompte ou un paiement partiel n'est PAS réparti (`not_split`, montant entier,
  raison) ;
- la part d'une ligne sans praticien nommé (`none`, `no_treatment`, `unavailable`) va
  dans une entrée qui porte ce statut, aucun praticien inventé ;
- arrondi au centime, reste au dernier praticien, somme EXACTEMENT égale au paiement ;
- ce qui empêche de savoir (montant illisible, cumul au-delà du total, ordre inconnu,
  lecture en échec) donne `undetermined` et sa raison, rien n'est réparti ;
- la liste totalise sa page : par praticien, non attribué, non réparti, indéterminé.

Toutes les valeurs sont factices.
"""
from decimal import Decimal

import pytest

from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import nextmotion_ventilation as v

from test_nextmotion_praticien import (C, D1, D2, P1, P2, T1, T2, _liste, _paiement,
                                       _provider, _tool, client)  # noqa: F401

T3 = "00000000-0000-4000-8000-0000000000b3"
F = "00000000-0000-4000-8000-0000000000f9"
OK1 = {"status": "ok", "id": D1, "first_name": "Prénom-A", "last_name": "Nom-A"}
OK2 = {"status": "ok", "id": D2, "first_name": "Prénom-B", "last_name": "Nom-B"}


def _versement(pid, montant, quand, total="300.00", lignes=(("200.00", T1),
                                                            ("100.00", T2))):
    return {"id": pid, "card": montant, "payment_time": quand, "invoice": {
        "id": F, "total_price": total,
        "invoiced_treatments": [{"price": p, "treatment": t} for p, t in lignes]}}


def _parts(split):
    return [(s["practitioner"].get("id") or s["practitioner"]["status"], s["amount"])
            for s in split["shares"]]


def test_un_paiement_qui_solde_est_reparti_au_prorata():
    p = _versement(P1, "300.00", "2026-10-01T10:00:00+02:00")
    out = v.ventilate(p, [OK1, OK2], [p], False)
    assert out["status"] == "split" and out["amount"] == "300.00"
    assert _parts(out) == [(D1, "200.00"), (D2, "100.00")]


def test_un_acompte_n_est_pas_reparti_puis_le_solde_l_est():
    acompte = _versement(P1, "100.00", "2026-10-01T10:00:00+02:00")
    solde = _versement(P2, "200.00", "2026-10-15T10:00:00+02:00")
    out = v.ventilate(acompte, [OK1, OK2], [solde, acompte], False)
    assert out["status"] == "not_split" and out["amount"] == "100.00"
    assert "does not settle" in out["reason"] and "100.00 of 300.00" in out["reason"]
    out = v.ventilate(solde, [OK1, OK2], [solde, acompte], False)
    assert out["status"] == "split" and _parts(out) == [(D1, "133.33"), (D2, "66.67")]


def test_un_paiement_partiel_seul_n_est_pas_reparti():
    p = _versement(P1, "120.00", "2026-10-01T10:00:00+02:00")
    out = v.ventilate(p, [OK1, OK2], [p], False)
    assert out["status"] == "not_split" and "shares" not in out


def test_la_part_d_un_praticien_indisponible_va_dans_une_entree_explicite():
    p = _versement(P1, "300.00", "2026-10-01T10:00:00+02:00",
                   lignes=(("200.00", T1), ("50.00", T2), ("50.00", None)))
    indispo = {"status": "unavailable", "reason": "read failed."}
    sans = {"status": "no_treatment", "reason": "this invoice line names no treatment."}
    out = v.ventilate(p, [OK1, indispo, sans], [p], False)
    assert _parts(out) == [(D1, "200.00"), ("unavailable", "50.00"),
                           ("no_treatment", "50.00")]
    assert out["shares"][1]["practitioner"] == indispo  # aucun praticien inventé
    assert "id" not in out["shares"][1]["practitioner"]


def test_arrondi_cent_sur_trois_le_reste_au_dernier_praticien():
    ok3 = {"status": "ok", "id": "d3", "first_name": "C", "last_name": "C"}
    p = _versement(P1, "100.00", "2026-10-01T10:00:00+02:00", total="100.00",
                   lignes=(("10.00", T1), ("10.00", T2), ("10.00", T3)))
    out = v.ventilate(p, [OK1, OK2, ok3], [p], False)
    assert _parts(out) == [(D1, "33.33"), (D2, "33.33"), ("d3", "33.34")]
    assert sum(Decimal(s["amount"]) for s in out["shares"]) == Decimal("100.00")


@pytest.mark.parametrize("montant,prix", [
    ("100.00", ("1", "1", "1")), ("0.01", ("1", "1", "1")), ("99.99", ("7", "11", "13")),
    ("1234.57", ("0.10", "333.33", "9")), ("10.00", ("2", "2", "2"))])
def test_la_somme_des_parts_vaut_exactement_le_paiement(montant, prix):
    parts = v.split(Decimal(montant), [(px, {"status": "ok", "id": i})
                                       for i, px in enumerate(prix)])
    assert sum(Decimal(s["amount"]) for s in parts) == Decimal(montant)
    assert all(Decimal(s["amount"]).as_tuple().exponent == -2 for s in parts)


def test_le_reste_va_au_dernier_praticien_nomme_pas_a_une_entree_sans_praticien():
    parts = v.split(Decimal("100.00"), [("1", OK1), ("1", {"status": "none", "reason": "r"}),
                                        ("1", OK2)])
    assert [(s["practitioner"].get("id") or "none", s["amount"]) for s in parts] == [
        (D1, "33.33"), (D2, "33.34"), ("none", "33.33")]


@pytest.mark.parametrize("paiements,attendu", [
    ("trop", "exceed"), ("sans_date", "no payment_time"), ("absent", "not among"),
    ("illisible", "not a decimal")])
def test_ce_qui_empeche_de_savoir_est_dit(paiements, attendu):
    p = _versement(P1, "300.00", "2026-10-01T10:00:00+02:00")
    autre = _versement(P2, "50.00", "2026-10-02T10:00:00+02:00")
    rows = {"trop": [p, autre], "sans_date": [p, {**autre, "payment_time": None}],
            "absent": [autre], "illisible": [p, {**autre, "card": "cinquante"}]}[paiements]
    out = v.ventilate(p, [OK1, OK2], rows, False)
    assert out["status"] == "undetermined" and attendu in out["reason"]
    assert "shares" not in out


def test_une_liste_de_paiements_coupee_ne_conclut_pas():
    p = _versement(P1, "300.00", "2026-10-01T10:00:00+02:00")
    assert v.ventilate(p, [OK1, OK2], [p], True)["status"] == "undetermined"


def test_le_montant_d_un_paiement_somme_ses_moyens():
    p = {"card": "100.00", "cash": "20.50", "custom_medium_list": [
        {"id": "m", "name": "x", "amount": "9.50"}], "check": None}
    assert v.amount(p) == Decimal("130.00")


def test_l_outil_ventile_et_totalise_la_page(client):
    solde = _paiement(P1, ("Acte-1", "200.00", T1), ("Acte-2", "100.00", T2))
    acompte = _paiement(P2, ("Acte-1", "150.00", T1), card="50.00")
    acompte["invoice"]["total_price"] = "150.00"
    client.list_payments.side_effect = _liste(solde, acompte)
    client.get_treatment_provider.side_effect = _provider
    out = _tool("nextmotion_payment")(clinic_id=C, with_practitioner=True)
    s1, s2 = (p["split"] for p in out["payments"])
    assert s1["status"] == "split" and _parts(s1) == [(D1, "200.00"), (D2, "100.00")]
    assert s2 == {"status": "not_split", "amount": "50.00", "reason": s2["reason"]}
    assert out["totals"]["by_practitioner"] == [
        {"practitioner": OK1, "amount": "200.00"}, {"practitioner": OK2, "amount": "100.00"}]
    assert out["totals"]["not_split"] == "50.00" and out["totals"]["unattributed"] == []
    assert "settles" in out["split_note"].lower() or "SETTLES" in out["split_note"]
    # Une lecture des paiements par facture distincte, en plus de la page.
    factures = [c.kwargs.get("invoice_id") for c in client.list_payments.call_args_list]
    assert sorted(f for f in factures if f) == sorted(
        [solde["invoice"]["id"], acompte["invoice"]["id"]])


def test_les_totaux_survivent_a_fields(client):
    client.list_payments.side_effect = _liste(_paiement(P1, ("Acte-1", "300.00", T1)))
    client.get_treatment_provider.side_effect = _provider
    out = _tool("nextmotion_payment")(clinic_id=C, with_practitioner=True, fields=["card"])
    assert "split" not in out["payments"][0]
    assert out["totals"]["by_practitioner"][0]["amount"] == "300.00"


def test_une_lecture_des_paiements_de_la_facture_en_echec_est_dite(client):
    paiement = _paiement(P1, ("Acte-1", "300.00", T1))

    def lire(clinic_id, invoice_id=None, **kw):
        if invoice_id is not None:
            raise ValueError("upstream down.")
        return {"count": 1, "next": None, "data": [paiement]}
    client.list_payments.side_effect = lire
    client.get_treatment_provider.side_effect = _provider
    out = _tool("nextmotion_payment")(clinic_id=C, with_practitioner=True)
    split = out["payments"][0]["split"]
    assert split["status"] == "undetermined" and "upstream down." in split["reason"]
    assert out["totals"]["undetermined"] == "300.00"


def test_get_avec_praticien_exige_la_clinique(client):
    with pytest.raises(McpError, match="clinic_id"):
        _tool("nextmotion_payment")(op="get", payment_id=P1, with_practitioner=True)
    client.get_payment.assert_not_called()


def test_get_sans_praticien_refuse_toujours_la_clinique(client):
    with pytest.raises(McpError, match="clinic_id"):
        _tool("nextmotion_payment")(op="get", payment_id=P1, clinic_id=C)
