"""Nextmotion — le praticien de chaque vente, et la fenêtre de dates des paiements.

Ce que ce fichier verrouille :
- `paid_from` / `paid_to` vont au client tels quels (le filtre est celui de Nextmotion),
  vérifiés AVANT tout appel ;
- `with_practitioner` nomme, sur chaque ligne de la facture payée, le praticien de son
  acte — UNE lecture par traitement dans l'appel, aucune entre deux appels ;
- un paiement qui couvre deux praticiens n'est PAS ventilé : ses montants restent ceux
  du paiement, chaque ligne garde son prix ;
- une lecture qui échoue est DITE sur la ligne (`unavailable` + raison) et comptée,
  jamais rendue comme un praticien vide ;
- sans `with_practitioner`, rien ne change (aucune lecture de traitement).

Toutes les valeurs sont factices.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from oto_mcp.mcp_errors import McpError

C = "00000000-0000-4000-8000-00000000000c"
P1 = "00000000-0000-4000-8000-0000000000a1"
P2 = "00000000-0000-4000-8000-0000000000a2"
T1 = "00000000-0000-4000-8000-0000000000b1"
T2 = "00000000-0000-4000-8000-0000000000b2"
D1 = "00000000-0000-4000-8000-0000000000d1"
D2 = "00000000-0000-4000-8000-0000000000d2"


def _tool(name):
    import importlib

    from fastmcp import FastMCP
    from oto_mcp.providers.nextmotion import CONNECTOR

    m = FastMCP("t")
    for mod in CONNECTOR.modules:
        importlib.import_module(f"oto_mcp.tools.{mod}").register(m)
    return asyncio.run(m.get_tool(name)).fn


@pytest.fixture
def client(monkeypatch):
    inst = MagicMock()
    monkeypatch.setattr("oto.tools.nextmotion.NextmotionClient", lambda **kw: inst)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: ("nm-test", False))
    return inst


def _provider(treatment_id):
    praticiens = {T1: {"id": D1, "first_name": "Prénom-A", "last_name": "Nom-A"},
                  T2: {"id": D2, "first_name": "Prénom-B", "last_name": "Nom-B"}}
    return {"data": {"id": treatment_id, "invoice": C, "provider": praticiens[treatment_id]}}


def _paiement(pid, *lignes, card="300.00"):
    return {"id": pid, "card": card, "invoice": {
        "id": C, "total_price": card,
        "invoiced_treatments": [{"id": f"l{i}", "name": n, "price": prix, "treatment": t}
                                for i, (n, prix, t) in enumerate(lignes)]}}


def test_la_fenetre_de_dates_va_au_client(client):
    client.list_payments.return_value = {"count": 0, "next": None, "data": []}
    _tool("nextmotion_payment")(clinic_id=C, paid_from="2026-10-01", paid_to="2026-10-31")
    client.list_payments.assert_called_once_with(
        C, invoice_id=None, paid_from="2026-10-01", paid_to="2026-10-31", limit=50,
        offset=0)


@pytest.mark.parametrize("kw,msg", [
    ({"paid_from": "01/10/2026"}, "YYYY-MM-DD"),
    ({"paid_from": "2026-10-31", "paid_to": "2026-10-01"}, "is after"),
])
def test_une_fenetre_invalide_est_refusee_avant_tout_appel(client, kw, msg):
    with pytest.raises(McpError, match=msg):
        _tool("nextmotion_payment")(clinic_id=C, **kw)
    client.list_payments.assert_not_called()


def test_get_et_update_refusent_la_fenetre(client):
    with pytest.raises(McpError, match="paid_from"):
        _tool("nextmotion_payment")(op="get", payment_id=P1, paid_from="2026-10-01")


def test_chaque_ligne_porte_son_praticien_sans_ventiler_le_paiement(client):
    client.list_payments.return_value = {"count": 2, "next": None, "data": [
        _paiement(P1, ("Acte-1", "200.00", T1), ("Acte-2", "100.00", T2)),
        _paiement(P2, ("Acte-1", "150.00", T1), card="150.00")]}
    client.get_treatment_provider.side_effect = _provider
    out = _tool("nextmotion_payment")(clinic_id=C, with_practitioner=True)

    p1 = out["payments"][0]
    assert p1["card"] == "300.00"  # le montant reste celui du paiement
    lignes = p1["invoice"]["invoiced_treatments"]
    assert [(li["price"], li["practitioner"]["id"]) for li in lignes] == [
        ("200.00", D1), ("100.00", D2)]
    assert lignes[0]["practitioner"] == {"status": "ok", "id": D1,
                                         "first_name": "Prénom-A", "last_name": "Nom-A"}
    assert "treatment" not in lignes[0]  # l'id clinique reste retenu
    # T1 paraît deux fois dans l'appel : une seule lecture.
    assert sorted(a.args[0] for a in client.get_treatment_provider.call_args_list) == [T1, T2]
    assert out["practitioners_unavailable"] == 0 and "NOT split" in out["practitioner_note"]


def test_pas_de_cache_entre_deux_appels(client):
    client.list_payments.return_value = {"count": 1, "next": None, "data": [
        _paiement(P1, ("Acte-1", "300.00", T1))]}
    client.get_treatment_provider.side_effect = _provider
    tool = _tool("nextmotion_payment")
    tool(clinic_id=C, with_practitioner=True)
    tool(clinic_id=C, with_practitioner=True)
    assert client.get_treatment_provider.call_count == 2


def test_une_lecture_en_echec_est_dite_sur_la_ligne(client):
    client.list_payments.return_value = {"count": 1, "next": None, "data": [
        _paiement(P1, ("Acte-1", "200.00", T1), ("Acte-2", "100.00", T2))]}

    def lecture(t):
        if t == T2:
            raise ValueError("unexpected Nextmotion answer: no `data` object.")
        return _provider(t)
    client.get_treatment_provider.side_effect = lecture
    out = _tool("nextmotion_payment")(clinic_id=C, with_practitioner=True)
    lignes = out["payments"][0]["invoice"]["invoiced_treatments"]
    assert lignes[0]["practitioner"]["status"] == "ok"
    assert lignes[1]["practitioner"] == {
        "status": "unavailable", "reason": "unexpected Nextmotion answer: no `data` object."}
    assert out["practitioners_unavailable"] == 1


def test_sans_praticien_nomme_ou_sans_traitement_la_raison_est_dite(client):
    client.get_payment.return_value = {"data": _paiement(
        P1, ("Acte-1", "200.00", T1), ("Produit", "100.00", None))}
    client.get_treatment_provider.return_value = {
        "data": {"id": T1, "invoice": C, "provider": None}}
    out = _tool("nextmotion_payment")(op="get", payment_id=P1, with_practitioner=True)
    lignes = out["payment"]["invoice"]["invoiced_treatments"]
    assert lignes[0]["practitioner"]["status"] == "none" and lignes[0]["practitioner"]["reason"]
    assert lignes[1]["practitioner"]["status"] == "no_treatment"
    assert client.get_treatment_provider.call_count == 1
    assert out["practitioners_unavailable"] == 0


def test_sans_l_option_aucune_lecture_de_traitement(client):
    client.list_payments.return_value = {"count": 1, "next": None, "data": [
        _paiement(P1, ("Acte-1", "300.00", T1))]}
    out = _tool("nextmotion_payment")(clinic_id=C)
    assert "practitioner" not in out["payments"][0]["invoice"]["invoiced_treatments"][0]
    assert "practitioners_unavailable" not in out
    client.get_treatment_provider.assert_not_called()
