"""Le coût d'un run Apify : plafonné au lancement, noté, relevé sous l'org qui l'a lancé.

Un run survit à l'appel qui l'a lancé, et `max_items` ne borne pas un acteur facturé à
l'événement. Verrouille les trois garde-fous : le plafond part toujours, un run que
personne ne relit est facturé à qui l'a lancé, une lecture depuis une autre org ne
facture personne.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from oto_mcp import apify_releve
from oto_mcp.tools import apify


def _carte(evenements, modele="PAY_PER_EVENT", minimum=None):
    return {"data": {"pricingInfos": [
        {"pricingModel": "FLAT_PRICE_PER_MONTH", "pricePerUnitUsd": 20},
        {"pricingModel": modele, "minimalMaxTotalChargeUsd": minimum,
         "pricingPerEvent": {"actorChargeEvents": evenements}}]}}


EVENEMENTS = {"start": {"isOneTimeEvent": True, "eventPriceUsd": 0.00005},
              "job": {"eventPriceUsd": 0.0002},
              "organization": {"eventTieredPricingUsd": {
                  "FREE": {"tieredEventPriceUsd": 0.0004},
                  "GOLD": {"tieredEventPriceUsd": 0.0002}}}}


def test_le_plafond_derive_suit_le_volume_et_compte_chaque_evenement_repete():
    # 1000 items × (0,0002 + palier le plus haut 0,0004) × marge 2 + démarrage
    assert apify._plafond_derive(_carte(EVENEMENTS), 1000) == pytest.approx(1.20005)


def test_le_plafond_derive_a_un_plancher_pour_un_petit_run():
    assert apify._plafond_derive(_carte(EVENEMENTS), 2) == apify._PLANCHER_USD


def test_le_minimum_de_l_acteur_prime():
    assert apify._plafond_derive(_carte(EVENEMENTS, minimum=0.2), 2) == 0.2


@pytest.mark.parametrize("carte", [
    None, {}, {"data": {"pricingInfos": []}},
    _carte(EVENEMENTS, modele="FLAT_PRICE_PER_MONTH"),
    _carte({"job": {}}),
    _carte({"start": {"isOneTimeEvent": True, "eventPriceUsd": 0.1}}),
])
def test_une_carte_sans_prix_lisible_ne_derive_rien(carte):
    assert apify._plafond_derive(carte, 10) is None


def test_un_plafond_demande_passe_tel_quel_sans_lire_la_carte():
    client = MagicMock()
    assert apify._plafond_du_lancement(client, "a/b", 5, 12.5) == 12.5
    client.actor.assert_not_called()


def test_sans_volume_le_defaut_sert():
    client = MagicMock()
    with patch.object(apify, "_plafond_par_defaut", return_value=1.0):
        assert apify._plafond_du_lancement(client, "a/b", None, None) == 1.0
    client.actor.assert_not_called()


def test_une_carte_illisible_ne_bloque_pas_le_lancement():
    from oto.tools.common.errors import UpstreamHTTPError
    client = MagicMock()
    client.actor.side_effect = UpstreamHTTPError(500, {}, service="apify")
    with patch.object(apify, "_plafond_par_defaut", return_value=1.0):
        assert apify._plafond_du_lancement(client, "a/b", 5, None) == 1.0


@pytest.mark.parametrize("pose,attendu", [
    (None, 1.0), ("", 1.0), ("2.5", 2.5), ("abc", 1.0), ("0", 1.0), ("-1", 1.0),
    ("inf", 1.0), ("nan", 1.0),
])
def test_le_reglage_du_plafond_par_defaut(pose, attendu):
    with patch("oto_mcp.db.connector_settings.get_connector_setting", return_value=pose):
        assert apify._plafond_par_defaut() == attendu


def test_le_403_dit_la_reponse_d_apify_et_de_ne_pas_reessayer():
    from oto.tools.common.errors import UpstreamHTTPError
    msg = apify._upstream_message(UpstreamHTTPError(403, {"error": "cap"}, service="apify"))
    assert "cap" in msg and "do not retry" in msg


TERMINE = {"data": {"id": "r1", "status": "SUCCEEDED", "usageTotalUsd": 0.5,
                    "options": {"maxTotalChargeUsd": 0.5}}}


def _releve(ligne, org_courante):
    with patch("oto_mcp.db.apify_runs.lire", return_value=ligne), \
            patch("oto_mcp.db.apify_runs.marquer") as marquer, \
            patch("oto_mcp.auth.hooks.current_user_sub_from_token", return_value="s"), \
            patch.object(apify.access, "current_org", return_value=org_courante), \
            patch.object(apify.session_org, "note_call_trace") as trace, \
            patch.object(apify.session_org, "noter_avis") as avis:
        apify._releve(TERMINE, "r1")
    return trace.call_args.kwargs, marquer, avis


def test_lu_depuis_une_autre_org_le_run_ne_facture_personne():
    trace, marquer, _ = _releve({"org_id": 7}, org_courante=1)
    assert trace == {"quantity": 0}
    marquer.assert_not_called()


def test_lu_par_l_org_qui_l_a_lance_le_run_est_trace_et_marque():
    trace, marquer, _ = _releve({"org_id": 7}, org_courante=7)
    assert trace == {"quantity": 500000, "apify_run_id": "r1"}
    marquer.assert_called_once_with("r1", statut="SUCCEEDED", cout_usd=0.5)


def test_un_run_non_note_se_trace_la_ou_il_est_lu():
    trace, marquer, _ = _releve(None, org_courante=1)
    assert trace == {"quantity": 500000, "apify_run_id": "r1"}
    marquer.assert_not_called()


def test_un_run_arrive_a_son_plafond_le_dit():
    _, _, avis = _releve(None, org_courante=1)
    assert "partial" in avis.call_args.args[0]


def _tool(name):
    from fastmcp import FastMCP
    m = FastMCP("t")
    apify.register(m)
    return asyncio.run(m.get_tool(name))


@pytest.mark.parametrize("mode,note", [("platform", True), ("org", False), (None, False)])
def test_seul_un_run_de_la_cle_plateforme_est_note(mode, note):
    with patch("oto_mcp.access.resolve_api_key", return_value=("k", False)), \
            patch("oto.tools.apify.client.ApifyClient") as cls, \
            patch.object(apify.session_org, "current_call_trace",
                         return_value={"key_mode": mode}), \
            patch("oto_mcp.auth.hooks.current_user_sub_from_token", return_value="s"), \
            patch.object(apify.access, "current_org", return_value=7), \
            patch.object(apify.session_org, "current_call_run", return_value="tr"), \
            patch("oto_mcp.db.apify_runs.noter") as noter, \
            patch.object(apify, "_plafond_par_defaut", return_value=1.0):
        cls.return_value.run.return_value = {"data": {"id": "r1", "status": "RUNNING"}}
        out = _tool("apify_run").fn(actor_id="a/b")
    assert out["data"]["id"] == "r1"
    assert cls.return_value.run.call_args.kwargs["max_total_charge_usd"] == 1.0
    assert noter.called is note
    if note:
        assert noter.call_args.kwargs["org_id"] == 7


def test_une_ecriture_ratee_n_empeche_pas_le_run():
    with patch.object(apify.session_org, "current_call_trace",
                      return_value={"key_mode": "platform"}), \
            patch("oto_mcp.auth.hooks.current_user_sub_from_token", return_value="s"), \
            patch.object(apify.access, "current_org", return_value=7), \
            patch("oto_mcp.db.apify_runs.noter", side_effect=RuntimeError("base")):
        apify._noter_lancement("r1", "a/b", "apify_run", 1.0)


LIGNE = {"apify_run_id": "r1", "sub": "s", "org_id": 7, "run_id": "tr",
         "lance_le": "2026-01-01T00:00:00+00:00"}


def _relever(run, ligne=LIGNE, dry_run=False, effet=None):
    client = MagicMock()
    client.run_status.return_value = run
    client.run_status.side_effect = effet
    with patch("oto.tools.apify.client.ApifyClient", return_value=client), \
            patch.object(apify_releve.apify_runs, "a_relever", return_value=[ligne]), \
            patch.object(apify_releve.apify_runs, "marquer") as marquer, \
            patch.object(apify_releve.apify_runs, "purger", return_value=0), \
            patch.object(apify_releve.usage, "insert_tool_call") as inserer:
        compte = apify_releve.relever("k", dry_run=dry_run)
    return compte, marquer, inserer


def test_le_releve_facture_le_lanceur_avec_la_ligne_d_un_status():
    compte, marquer, inserer = _relever(
        {"data": {"id": "r1", "status": "SUCCEEDED", "usageTotalUsd": 5.08865}})
    ligne = inserer.call_args.args[0]
    assert ligne["tool"] == "apify_run_status" and ligne["org_id"] == 7
    assert ligne["sub"] == "s" and ligne["key_mode"] == "platform" and ligne["ok"] is True
    assert ligne["quantity"] == 5088650
    assert ligne["args"]["apify_run_id"] == "r1"
    marquer.assert_called_once_with("r1", statut="SUCCEEDED", cout_usd=5.08865)
    assert compte["releves"] == 1


def test_un_run_a_zero_ne_cree_pas_de_ligne_mais_se_clot():
    _, marquer, inserer = _relever(
        {"data": {"id": "r1", "status": "FAILED", "usageTotalUsd": 0}})
    inserer.assert_not_called()
    marquer.assert_called_once()


def test_un_run_en_cours_attend():
    compte, marquer, inserer = _relever({"data": {"id": "r1", "status": "RUNNING"}})
    assert compte["en_cours"] == 1
    inserer.assert_not_called()
    marquer.assert_not_called()


def test_a_blanc_rien_ne_s_ecrit():
    compte, marquer, inserer = _relever(
        {"data": {"id": "r1", "status": "SUCCEEDED", "usageTotalUsd": 1}}, dry_run=True)
    assert compte["releves"] == 1
    inserer.assert_not_called()
    marquer.assert_not_called()


def test_un_run_inconnu_d_apify_se_clot_sans_cout():
    from oto.tools.common.errors import UpstreamHTTPError
    compte, marquer, _ = _relever(None, effet=UpstreamHTTPError(404, {}, service="apify"))
    assert compte["introuvables"] == 1
    marquer.assert_called_once_with("r1", statut="introuvable", cout_usd=None)


def test_une_panne_laisse_le_run_a_demain():
    from oto.tools.common.errors import UpstreamHTTPError
    compte, marquer, _ = _relever(None, effet=UpstreamHTTPError(500, {}, service="apify"))
    assert compte["echecs"] == 1
    marquer.assert_not_called()


def _comparer(apify_usd, releve, premier, dry_run=False):
    bilan = {"releve_usd": releve, "en_attente": 0, "premier": premier}
    with patch.object(apify_releve, "_usage_du_cycle",
                      return_value=("2026-10-01T00:00:00.000Z", apify_usd)), \
            patch.object(apify_releve.apify_runs, "bilan_depuis", return_value=bilan), \
            patch.object(apify_releve.usage, "insert_usage_signal") as signal:
        return apify_releve.comparer("k", dry_run=dry_run), signal


def test_un_ecart_se_signale():
    out, signal = _comparer(30, 4.5, "2026-09-01T00:00:00+00:00")
    assert out["ecart_usd"] == 25.5
    assert signal.call_args.kwargs["target"] == "apify_cost_gap"


def test_une_date_de_la_base_arrive_sans_fuseau():
    out, _ = _comparer(30, 26, "2026-09-01 00:00:00")
    assert out["ecart_usd"] == 4


def test_un_petit_ecart_ne_dit_rien():
    out, signal = _comparer(30, 26, "2026-09-01T00:00:00+00:00")
    assert out["ecart_usd"] == 4
    signal.assert_not_called()


@pytest.mark.parametrize("premier", [None, "2026-10-15T00:00:00+00:00",
                                     "2026-10-15 00:00:00"])
def test_un_cycle_que_la_table_ne_couvre_pas_entier_ne_se_compare_pas(premier):
    out, signal = _comparer(100, 1, premier)
    assert out["ecart_usd"] is None
    signal.assert_not_called()


def test_sans_cle_plateforme_rien_ne_se_passe():
    with patch.object(apify_releve, "_cle_plateforme", return_value=None):
        assert apify_releve.maintenance() == {"cle_plateforme": False}
