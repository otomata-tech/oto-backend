"""Le coût d'un run Apify : plafonné au lancement, noté, relevé sous l'org qui l'a lancé.

Un run survit à l'appel qui l'a lancé, et `max_items` ne borne pas un acteur facturé à
l'événement. Verrouille les trois garde-fous : le plafond part toujours, un run que
personne ne relit est facturé à qui l'a lancé, une lecture depuis une autre org ne
facture personne.
"""
from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import MagicMock, patch

import psycopg
import pytest
import requests

from oto_mcp import apify_releve
from oto_mcp.db import journal_jour, usage
from oto_mcp.tools import apify


# Les réglages plateforme du connecteur `apify` que voit un test : vide = aucun posé.
REGLAGES: dict = {}
# Ceux de l'org de l'appel (`ORG_ID`), et le mode de clé de l'appel.
REGLAGES_ORG: dict = {}
ORG_ID = 7
# Ceux de l'org qui prête sa clé à l'appel, quand elle en prête une.
REGLAGES_PRETEUSE: dict = {}
PRETEUSE_ID = 9
APPEL = {"plateforme": False, "admin": False, "preteuse": None}


@pytest.fixture(autouse=True)
def _reglage_non_lu(monkeypatch):
    """Les réglages de plafond sont gardés par processus : chaque test part d'un
    processus qui ne les a pas encore lus, et d'une base où aucun n'est posé (un test
    qui en pose un le met dans `REGLAGES` ou `REGLAGES_ORG`, ou remplace la lecture).
    L'appel tourne par défaut sur une clé propre (BYOK), sous l'org `ORG_ID`, par un
    membre qui n'en est pas admin."""
    from oto_mcp.db import connector_settings
    monkeypatch.setattr(apify, "_reglages_lus", {})
    REGLAGES.clear()
    REGLAGES_ORG.clear()
    REGLAGES_PRETEUSE.clear()
    APPEL.update(plateforme=False, admin=False, preteuse=None)
    monkeypatch.setattr(connector_settings, "get_connector_setting",
                        lambda _t, _i, _c, cle: REGLAGES.get(cle))

    def _org(org_id, cle):
        reglages = {str(ORG_ID): REGLAGES_ORG, str(PRETEUSE_ID): REGLAGES_PRETEUSE}[org_id]
        return {"apify": reglages[cle]} if cle in reglages else {}
    monkeypatch.setattr(connector_settings, "org_settings_for_key", _org)
    monkeypatch.setattr(apify, "_org_de_l_appel", lambda: ORG_ID)
    monkeypatch.setattr(apify, "_sur_cle_plateforme", lambda: APPEL["plateforme"])
    monkeypatch.setattr(apify, "_org_qui_prete", lambda _o: APPEL["preteuse"])
    monkeypatch.setattr(apify, "_admin_de", lambda _o: APPEL["admin"])


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


def test_un_plafond_demande_sous_le_maximum_passe_tel_quel_sans_lire_la_carte():
    client = MagicMock()
    assert apify._plafond_du_lancement(client, "a/b", 5, 12.5) == 12.5
    client.actor.assert_not_called()


# --- le maximum par lancement, pour tous les appelants -------------------------------
# Avant le 03/11/2026, un plafond DEMANDÉ passait tel quel (`if demande is not None:
# return demande`) : au-delà des 25 $ des outils dédiés, `apify_run_sync` servait
# d'échappatoire. Fermée : la même vérification vaut pour tout lancement.

def test_le_maximum_par_defaut_est_de_vingt_cinq_dollars():
    assert apify._PLAFOND_MAX_USD == 25.0
    assert apify._plafond_max() == 25.0


def test_un_plafond_demande_au_maximum_passe():
    assert apify._plafond_du_lancement(MagicMock(), "a/b", None, 25) == 25


@pytest.mark.parametrize("usd", [25.01, 30, 1000])
def test_un_plafond_demande_au_dela_du_maximum_est_refuse_sans_echappatoire(usd):
    from oto_mcp.mcp_errors import McpError
    client = MagicMock()
    with pytest.raises(McpError) as e:
        apify._plafond_du_lancement(client, "a/b", 5, usd)
    msg = str(e.value)
    assert msg.startswith(apify.MAX_CHARGE_ABOVE_CAP) and "$25" in msg
    assert "apify_run_sync" not in msg
    client.actor.assert_not_called()


@pytest.mark.parametrize("usd", [0, -1])
def test_un_plafond_demande_nul_ou_negatif_est_refuse(usd):
    from oto_mcp.mcp_errors import McpError
    with pytest.raises(McpError, match="above 0"):
        apify._plafond_du_lancement(MagicMock(), "a/b", None, usd)


def test_le_maximum_se_regle_par_la_plateforme():
    APPEL["plateforme"] = True
    REGLAGES[apify.CLE_PLAFOND_MAX] = "50"
    REGLAGES_ORG[apify.CLE_PLAFOND_ORG] = "40"
    assert apify._plafond_du_lancement(MagicMock(), "a/b", None, 40) == 40


@pytest.mark.parametrize("pose", ["", "abc", "0", "-1", "inf", "nan"])
def test_un_maximum_illisible_fait_echouer_le_lancement(pose):
    APPEL["plateforme"] = True
    REGLAGES[apify.CLE_PLAFOND_MAX] = pose
    with pytest.raises(RuntimeError, match=apify.CLE_PLAFOND_MAX):
        apify._plafond_du_lancement(MagicMock(), "a/b", None, 5)
    with pytest.raises(RuntimeError, match=apify.CLE_PLAFOND_MAX):
        apify._plafond_du_lancement(MagicMock(), "a/b", None, None)


def test_une_base_illisible_pour_le_maximum_fait_echouer_le_lancement():
    APPEL["plateforme"] = True
    with patch("oto_mcp.db.connector_settings.get_connector_setting",
               side_effect=psycopg.OperationalError("base")), \
            pytest.raises(psycopg.OperationalError):
        apify._plafond_du_lancement(MagicMock(), "a/b", None, 5)


def test_un_plafond_derive_ou_par_defaut_est_ramene_au_maximum():
    APPEL["plateforme"] = True
    REGLAGES[apify.CLE_PLAFOND_MAX] = "3"
    client = MagicMock()
    client.actor.return_value = _carte(EVENEMENTS)
    # 1 000 000 items : dérivé ≈ 1 200 $, ramené à 3 $
    assert apify._plafond_du_lancement(client, "a/b", 1_000_000, None) == 3.0
    assert apify._plafond_du_lancement(client, "a/b", None, None) == 3.0


# --- le plafond de l'org (décision du 03/11/2026) -----------------------------------
# Le plafond par lancement est un réglage de l'ORG (25 $ sans réglage) ; sur la clé de la
# plateforme, il ne dépasse jamais le maximum de la plateforme ; sur une clé propre,
# jamais la borne de sécurité de la plateforme (1 000 $).

@pytest.mark.parametrize("plateforme", [True, False])
def test_sans_reglage_d_org_le_plafond_est_de_vingt_cinq_dollars(plateforme):
    APPEL["plateforme"] = plateforme
    assert apify._PLAFOND_ORG_DEFAUT_USD == 25.0 and apify._BORNE_BYOK_USD == 1000.0
    assert apify._borne_du_lancement() == (25.0, "default", ORG_ID)


@pytest.mark.parametrize("plateforme", [True, False])
def test_une_org_a_dix_dollars_plafonne_a_dix_sur_toute_cle(plateforme):
    from oto_mcp.mcp_errors import McpError
    APPEL["plateforme"] = plateforme
    REGLAGES_ORG[apify.CLE_PLAFOND_ORG] = "10"
    assert apify._plafond_du_lancement(MagicMock(), "a/b", None, 10) == 10
    with pytest.raises(McpError) as e:
        apify._plafond_du_lancement(MagicMock(), "a/b", None, 12)
    assert "your organization's ceiling of $10" in str(e.value)
    # le défaut (5 $) passe, un dérivé au-delà est ramené à 10, et le dit
    assert apify._plafond_du_lancement(MagicMock(), "a/b", None, None) == 5.0
    with patch.object(apify, "_plafond_derive", return_value=80.0), \
            patch.object(apify.session_org, "noter_avis") as avis:
        assert apify._plafond_du_lancement(MagicMock(), "a/b", 10_000, None) == 10.0
    assert "your organization's ceiling of $10" in avis.call_args.args[0]


def test_sur_la_cle_plateforme_le_maximum_borne_l_org_et_le_refus_le_dit():
    from oto_mcp.mcp_errors import McpError
    APPEL["plateforme"] = True
    REGLAGES_ORG[apify.CLE_PLAFOND_ORG] = "40"
    assert apify._borne_du_lancement() == (25.0, "platform", ORG_ID)
    with pytest.raises(McpError) as e:
        apify._plafond_du_lancement(MagicMock(), "a/b", None, 30)
    msg = str(e.value)
    assert msg.startswith(apify.MAX_CHARGE_ABOVE_CAP)
    assert "this platform's maximum of $25" in msg and "own Apify key" in msg


def test_sur_une_cle_propre_l_org_a_quarante_dollars_passe():
    REGLAGES_ORG[apify.CLE_PLAFOND_ORG] = "40"
    assert apify._plafond_du_lancement(MagicMock(), "a/b", None, 40) == 40


def test_sur_une_cle_propre_une_valeur_posee_au_dela_de_la_borne_est_bornee():
    from oto_mcp.mcp_errors import McpError
    REGLAGES_ORG[apify.CLE_PLAFOND_ORG] = "2000"
    assert apify._borne_du_lancement() == (1000.0, "byok", ORG_ID)
    with pytest.raises(McpError, match=r"safety bound of \$1000"):
        apify._plafond_du_lancement(MagicMock(), "a/b", None, 1500)


def test_la_borne_byok_se_regle_par_la_plateforme():
    REGLAGES[apify.CLE_BORNE_BYOK] = "300"
    REGLAGES_ORG[apify.CLE_PLAFOND_ORG] = "500"
    assert apify._borne_du_lancement() == (300.0, "byok", ORG_ID)


@pytest.mark.parametrize("pose", ["", "abc", "0", "-1", "inf", "nan"])
def test_une_borne_byok_illisible_fait_echouer_le_lancement(pose):
    REGLAGES[apify.CLE_BORNE_BYOK] = pose
    with pytest.raises(RuntimeError, match=apify.CLE_BORNE_BYOK):
        apify._plafond_du_lancement(MagicMock(), "a/b", None, 5)


@pytest.mark.parametrize("plateforme", [True, False])
@pytest.mark.parametrize("pose", ["", "abc", "0", "-1", "inf", "nan"])
def test_un_reglage_d_org_illisible_fait_echouer_le_lancement(pose, plateforme):
    APPEL["plateforme"] = plateforme
    REGLAGES_ORG[apify.CLE_PLAFOND_ORG] = pose
    with pytest.raises(RuntimeError, match=apify.CLE_PLAFOND_ORG):
        apify._plafond_du_lancement(MagicMock(), "a/b", None, 5)
    with pytest.raises(RuntimeError, match=apify.CLE_PLAFOND_ORG):
        apify._plafond_du_lancement(MagicMock(), "a/b", None, None)


def test_sans_org_le_defaut_sert_sans_lire_la_base():
    with patch.object(apify, "_org_de_l_appel", return_value=None), \
            patch("oto_mcp.db.connector_settings.org_settings_for_key") as lu:
        assert apify._borne_du_lancement() == (25.0, "default", None)
    lu.assert_not_called()


# --- la clé prêtée par une autre org (arbitrage du 03/11/2026) ------------------------
# C'est le prêteur qui paie : sur sa clé, le plus strict des deux plafonds s'applique ;
# la plateforme qui prête la sienne borne par son maximum.

def test_un_preteur_plus_strict_impose_son_plafond_et_le_refus_le_nomme():
    from oto_mcp.mcp_errors import McpError
    APPEL["preteuse"] = PRETEUSE_ID
    REGLAGES_ORG[apify.CLE_PLAFOND_ORG] = "40"
    REGLAGES_PRETEUSE[apify.CLE_PLAFOND_ORG] = "10"
    assert apify._borne_du_lancement() == (10.0, "lender", PRETEUSE_ID)
    with pytest.raises(McpError) as e:
        apify._plafond_du_lancement(MagicMock(), "a/b", None, 12)
    msg = str(e.value)
    assert "ceiling of organization #9, which lends this Apify key" in msg
    assert "set by the admins of organization #9" in msg


def test_un_preteur_sans_reglage_impose_le_defaut():
    APPEL["preteuse"] = PRETEUSE_ID
    REGLAGES_ORG[apify.CLE_PLAFOND_ORG] = "40"
    assert apify._borne_du_lancement() == (25.0, "lender", PRETEUSE_ID)


def test_une_appelante_plus_stricte_garde_son_plafond():
    APPEL["preteuse"] = PRETEUSE_ID
    REGLAGES_ORG[apify.CLE_PLAFOND_ORG] = "10"
    REGLAGES_PRETEUSE[apify.CLE_PLAFOND_ORG] = "40"
    assert apify._borne_du_lancement() == (10.0, "org", ORG_ID)
    assert apify._plafond_du_lancement(MagicMock(), "a/b", None, 10) == 10


def test_deux_plafonds_au_dela_de_la_borne_byok_y_restent():
    APPEL["preteuse"] = PRETEUSE_ID
    REGLAGES_ORG[apify.CLE_PLAFOND_ORG] = "2000"
    REGLAGES_PRETEUSE[apify.CLE_PLAFOND_ORG] = "1500"
    assert apify._borne_du_lancement() == (1000.0, "byok", ORG_ID)


def test_la_plateforme_qui_prete_borne_par_son_maximum_sans_chercher_de_preteuse():
    APPEL["plateforme"] = True
    REGLAGES_ORG[apify.CLE_PLAFOND_ORG] = "40"
    with patch.object(apify, "_org_qui_prete") as preteuse:
        assert apify._borne_du_lancement() == (25.0, "platform", ORG_ID)
    preteuse.assert_not_called()
    REGLAGES_ORG[apify.CLE_PLAFOND_ORG] = "10"
    assert apify._borne_du_lancement() == (10.0, "org", ORG_ID)


# L'original, avant que la fixture ne le remplace.
_ORG_QUI_PRETE = apify._org_qui_prete


def _sous(trace):
    return patch.object(apify.session_org, "current_call_trace", return_value=trace)


@pytest.mark.parametrize("ligne,attendu", [
    (("org", "9", "apify", ""), PRETEUSE_ID),
    (("org", "7", "apify", ""), None),
    (("member", "9:sub-x", "apify", ""), PRETEUSE_ID),
    (("member", "7:sub-x", "apify", ""), None),
])
def test_la_preteuse_se_lit_sur_la_ligne_qui_a_servi(ligne, attendu):
    with _sous({"key_mode": ligne[0], "credential_row": ligne}):
        assert _ORG_QUI_PRETE(ORG_ID) == attendu


def test_la_cle_d_une_equipe_est_pretee_par_l_org_de_l_equipe():
    with _sous({"key_mode": "group", "credential_row": ("group", "3", "apify", "")}), \
            patch("oto_mcp.group_store.get_group", return_value={"org_id": PRETEUSE_ID}):
        assert _ORG_QUI_PRETE(ORG_ID) == PRETEUSE_ID


@pytest.mark.parametrize("mode", ["platform", "tenant"])
def test_ni_la_plateforme_ni_un_tenant_ne_sont_une_org_preteuse(mode):
    with _sous({"key_mode": mode}):
        assert _ORG_QUI_PRETE(ORG_ID) is None


@pytest.mark.parametrize("trace", [
    None, {}, {"key_mode": "org"},
    {"key_mode": "user", "credential_row": ("user", "sub-x", "apify", "")},
])
def test_un_preteur_inconnaissable_fait_echouer_le_lancement(trace):
    with _sous(trace), pytest.raises(RuntimeError, match="lender of the key"):
        _ORG_QUI_PRETE(ORG_ID)


def test_une_equipe_sans_org_fait_echouer_le_lancement():
    with _sous({"key_mode": "group", "credential_row": ("group", "3", "apify", "")}), \
            patch("oto_mcp.group_store.get_group", return_value=None), \
            pytest.raises(RuntimeError, match="lender of the key"):
        _ORG_QUI_PRETE(ORG_ID)


# L'original, avant que la fixture ne le remplace.
_SUR_CLE_PLATEFORME = apify._sur_cle_plateforme


@pytest.mark.parametrize("mode,attendu", [("platform", True), ("org", False),
                                          ("user", False), (None, False)])
def test_le_mode_de_cle_se_lit_au_releve_de_l_appel(mode, attendu):
    with patch.object(apify.session_org, "current_call_trace",
                      return_value={"key_mode": mode} if mode else None):
        assert _SUR_CLE_PLATEFORME() is attendu


def test_le_refus_dit_a_l_admin_comment_changer_le_plafond_de_l_org():
    from oto_mcp.mcp_errors import McpError
    REGLAGES_ORG[apify.CLE_PLAFOND_ORG] = "10"
    with pytest.raises(McpError) as membre:
        apify._plafond_du_lancement(MagicMock(), "a/b", None, 12)
    assert "set by the admins of organization #7" in str(membre.value)
    assert "oto_org_settings" not in str(membre.value)
    APPEL["admin"] = True
    with pytest.raises(McpError) as admin:
        apify._plafond_du_lancement(MagicMock(), "a/b", None, 12)
    assert 'domain="apify_charge_cap"' in str(admin.value)
    assert "/api/orgs/7/connectors/apify/charge-cap" in str(admin.value)


def _lance(nom, **kwargs):
    with patch("oto_mcp.access.resolve_api_key", return_value=("k", False)), \
            patch("oto.tools.apify.client.ApifyClient") as cls, \
            patch.object(apify.session_org, "current_call_trace", return_value={}):
        cls.return_value.run.return_value = {"data": {"id": "r1", "status": "RUNNING"}}
        _tool(nom).fn(actor_id="a/b", **kwargs)
    return cls.return_value.run


@pytest.mark.parametrize("nom", ["apify_run", "apify_run_sync"])
def test_les_outils_de_lancement_refusent_au_dela_du_maximum(nom):
    from oto_mcp.mcp_errors import McpError
    with pytest.raises(McpError, match=apify.MAX_CHARGE_ABOVE_CAP):
        _lance(nom, max_total_charge_usd=30)


def test_apify_run_accepte_le_maximum_et_sans_plafond_part_a_cinq_dollars():
    assert _lance("apify_run", max_total_charge_usd=25).call_args.kwargs[
        "max_total_charge_usd"] == 25
    assert _lance("apify_run").call_args.kwargs["max_total_charge_usd"] == 5.0


def test_la_description_servie_dit_le_refus():
    prop = _tool("apify_run_sync").parameters["properties"]["max_total_charge_usd"]
    assert "max_charge_above_cap" in prop["description"]


def test_le_plafond_par_defaut_est_de_cinq_dollars():
    assert apify._PLAFOND_DEFAUT_USD == 5.0


def test_sans_volume_le_defaut_sert():
    client = MagicMock()
    with patch.object(apify, "_plafond_par_defaut", return_value=5.0):
        assert apify._plafond_du_lancement(client, "a/b", None, None) == 5.0
    client.actor.assert_not_called()


def _refus_apify():
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(500, {}, service="apify")


@pytest.mark.parametrize("panne", [
    _refus_apify(), requests.ConnectionError("reset"), requests.Timeout("lent"),
    ValueError("pas du JSON"),
])
def test_une_carte_illisible_ne_bloque_pas_le_lancement(panne, caplog):
    client = MagicMock()
    client.actor.side_effect = panne
    with patch.object(apify, "_plafond_par_defaut", return_value=5.0), \
            caplog.at_level(logging.WARNING, logger=apify.logger.name):
        assert apify._plafond_du_lancement(client, "a/b", 5, None) == 5.0
    assert "card of a/b unreadable" in caplog.text


def test_un_bug_a_la_lecture_de_la_carte_n_est_pas_masque():
    client = MagicMock()
    client.actor.side_effect = KeyError("data")
    with patch.object(apify, "_plafond_par_defaut", return_value=5.0), \
            pytest.raises(KeyError):
        apify._plafond_du_lancement(client, "a/b", 5, None)


@pytest.mark.parametrize("pose,attendu", [
    (None, 5.0), ("", 5.0), ("2.5", 2.5), ("abc", 5.0), ("0", 5.0), ("-1", 5.0),
    ("inf", 5.0), ("nan", 5.0),
])
def test_le_reglage_du_plafond_par_defaut(pose, attendu):
    with patch("oto_mcp.db.connector_settings.get_connector_setting",
               return_value=pose) as lire:
        assert apify._plafond_par_defaut() == attendu
    lire.assert_called_once_with("platform", "platform", "apify",
                                 apify.CLE_PLAFOND_DEFAUT)


def test_le_reglage_se_lit_une_fois_par_fenetre_et_par_processus():
    with patch("oto_mcp.db.connector_settings.get_connector_setting",
               return_value="2.5") as lire:
        assert apify._plafond_par_defaut() == 2.5
        assert apify._plafond_par_defaut() == 2.5
        assert lire.call_count == 1
        apify._reglages_lus[apify.CLE_PLAFOND_DEFAUT] = (
            time.monotonic() - apify._REGLAGE_TTL_S - 1, "2.5")
        assert apify._plafond_par_defaut() == 2.5
        assert lire.call_count == 2


def test_un_reglage_illisible_ne_bloque_pas_et_ne_se_garde_pas():
    with patch("oto_mcp.db.connector_settings.get_connector_setting",
               side_effect=[psycopg.OperationalError("base"), "2.5"]):
        assert apify._plafond_par_defaut() == 5.0
        assert apify._plafond_par_defaut() == 2.5


def test_le_403_dit_la_reponse_d_apify_et_de_ne_pas_reessayer():
    from oto.tools.common.errors import UpstreamHTTPError
    msg = apify._upstream_message(UpstreamHTTPError(403, {"error": "cap"}, service="apify"))
    assert "cap" in msg and "do not retry" in msg


TERMINE = {"data": {"id": "r1", "status": "SUCCEEDED", "usageTotalUsd": 0.5,
                    "options": {"maxTotalChargeUsd": 0.5}}}


def _releve(ligne, org_courante, mode="platform", panne=None):
    with patch("oto_mcp.db.apify_runs.lire", return_value=ligne,
               side_effect=panne) as lire, \
            patch("oto_mcp.db.apify_runs.marquer") as marquer, \
            patch("oto_mcp.auth.hooks.current_user_sub_from_token", return_value="s"), \
            patch.object(apify.access, "current_org", return_value=org_courante), \
            patch.object(apify.session_org, "current_call_trace",
                         return_value={"key_mode": mode}), \
            patch.object(apify.session_org, "note_call_trace") as trace, \
            patch.object(apify.session_org, "noter_avis") as avis:
        apify._releve(TERMINE, "r1")
    return trace.call_args.kwargs, marquer, avis, lire


def test_lu_depuis_une_autre_org_le_run_ne_facture_personne():
    trace, marquer, _, _ = _releve({"org_id": 7}, org_courante=1)
    assert trace == {"quantity": 0}
    marquer.assert_not_called()


def test_lu_par_l_org_qui_l_a_lance_le_run_est_trace_et_marque():
    trace, marquer, _, _ = _releve({"org_id": 7}, org_courante=7)
    assert trace == {"quantity": 500000, "apify_run_id": "r1"}
    marquer.assert_called_once_with("r1", statut="SUCCEEDED", cout_usd=0.5)


def test_un_run_non_note_se_trace_la_ou_il_est_lu():
    trace, marquer, _, _ = _releve(None, org_courante=1)
    assert trace == {"quantity": 500000, "apify_run_id": "r1"}
    marquer.assert_not_called()


@pytest.mark.parametrize("mode", ["org", "user", None])
def test_hors_cle_plateforme_la_table_n_est_pas_lue(mode):
    trace, marquer, _, lire = _releve({"org_id": 7}, org_courante=1, mode=mode)
    assert trace == {"quantity": 500000, "apify_run_id": "r1"}
    lire.assert_not_called()
    marquer.assert_not_called()


def test_une_fiche_de_lancement_illisible_ne_facture_pas_le_lecteur():
    """Sinon le lecteur ET le lanceur (au relevé) seraient facturés pour le même run."""
    trace, marquer, _, _ = _releve(None, org_courante=1,
                                   panne=psycopg.OperationalError("base"))
    assert trace == {"quantity": 0}
    marquer.assert_not_called()


def test_un_marquage_rate_n_empeche_pas_la_trace():
    with patch("oto_mcp.db.apify_runs.lire", return_value={"org_id": 7}), \
            patch("oto_mcp.db.apify_runs.marquer",
                  side_effect=psycopg.OperationalError("base")), \
            patch("oto_mcp.auth.hooks.current_user_sub_from_token", return_value="s"), \
            patch.object(apify.access, "current_org", return_value=7), \
            patch.object(apify.session_org, "current_call_trace",
                         return_value={"key_mode": "platform"}), \
            patch.object(apify.session_org, "note_call_trace") as trace, \
            patch.object(apify.session_org, "noter_avis"):
        apify._releve(TERMINE, "r1")
    assert trace.call_args.kwargs == {"quantity": 500000, "apify_run_id": "r1"}


def test_un_plafond_derive_au_dela_du_maximum_est_ramene_et_le_dit():
    """Ramené au maximum, jamais en silence : la réponse dit la sortie partielle."""
    with patch.object(apify, "_plafond_max", return_value=25.0), \
            patch.object(apify, "_plafond_derive", return_value=80.0), \
            patch.object(apify.session_org, "noter_avis") as avis:
        assert apify._plafond_du_lancement(MagicMock(), "a/b", 10_000, None) == 25.0
    texte = avis.call_args.args[0]
    assert "partial" in texte and "$25" in texte


def test_un_plafond_derive_sous_le_maximum_passe_sans_avis():
    with patch.object(apify, "_plafond_max", return_value=25.0), \
            patch.object(apify, "_plafond_derive", return_value=3.0), \
            patch.object(apify.session_org, "noter_avis") as avis:
        assert apify._plafond_du_lancement(MagicMock(), "a/b", 100, None) == 3.0
    avis.assert_not_called()


def test_un_defaut_au_dela_du_maximum_est_ramene_et_le_dit():
    APPEL["plateforme"] = True
    with patch.object(apify, "_plafond_max", return_value=4.0), \
            patch.object(apify, "_plafond_par_defaut", return_value=5.0), \
            patch.object(apify.session_org, "noter_avis") as avis:
        assert apify._plafond_du_lancement(MagicMock(), "a/b", None, None) == 4.0
    assert "partial" in avis.call_args.args[0]


def test_un_run_arrive_a_son_plafond_le_dit():
    _, _, avis, _ = _releve(None, org_courante=1)
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
            patch.object(apify, "_plafond_par_defaut", return_value=5.0):
        cls.return_value.run.return_value = {"data": {"id": "r1", "status": "RUNNING"}}
        out = _tool("apify_run").fn(actor_id="a/b")
    assert out["data"]["id"] == "r1"
    assert cls.return_value.run.call_args.kwargs["max_total_charge_usd"] == 5.0
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
    assert ligne["kind"] == journal_jour.KIND_RELEVE == "settlement"
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


def test_plusieurs_cles_plateforme_levent_une_erreur_nommee():
    with patch.object(apify_releve.credentials_store, "list_platform_credentials",
                      return_value=[{"label": "a"}, {"label": "b"}]), \
            patch.object(apify_releve.credentials_store,
                         "get_credential_with_meta") as lire, \
            pytest.raises(apify_releve.PlusieursClesPlateforme, match="2 Apify"):
        apify_releve._cle_plateforme()
    lire.assert_not_called()


def test_une_seule_cle_plateforme_sert():
    with patch.object(apify_releve.credentials_store, "list_platform_credentials",
                      return_value=[{"label": "a"}]), \
            patch.object(apify_releve.credentials_store, "get_credential_with_meta",
                         return_value={"secret": "chiffre"}), \
            patch.object(apify_releve.credentials_store, "unpack_secret",
                         return_value={"key": "k"}):
        assert apify_releve._cle_plateforme() == "k"


def test_une_comparaison_en_echec_garde_le_bilan_du_releve(caplog):
    bilan = {"releves": 1, "en_cours": 0, "illisibles": 0, "introuvables": 0,
             "echecs": 0, "purges": 0}
    with patch.object(apify_releve, "_cle_plateforme", return_value="k"), \
            patch.object(apify_releve, "relever", return_value=bilan), \
            patch.object(apify_releve, "_usage_du_cycle",
                         side_effect=KeyError("monthlyUsageCycle")), \
            caplog.at_level(logging.ERROR, logger=apify_releve.logger.name):
        out = apify_releve.maintenance()
    assert out["relevement"] == bilan
    assert out["comparaison"] == {"echec": "KeyError: 'monthlyUsageCycle'"}
    assert "apify cost comparison FAILED" in caplog.text


# --- la ligne de relevé : facturée, hors des lentilles d'usage -----------------

def test_la_facturation_lit_la_ligne_de_releve_l_audit_non():
    factures, _ = usage._audit_window_clauses(1, None, "2026-11-01", facturation=True)
    audit, _ = usage._audit_window_clauses(1, None, "2026-11-01")
    assert "'settlement'" in factures[0] and "'mcp'" in factures[0]
    assert audit[0] == "l.kind = 'mcp'"


def test_les_jobs_factures_et_les_totaux_comptent_la_ligne_de_releve():
    assert "'settlement'" in journal_jour.predicat_jobs()
    assert journal_jour.KIND_RELEVE in journal_jour.KINDS
    assert journal_jour.KINDS_FACTURES == ("mcp", "settlement")


def test_les_lentilles_d_usage_ne_lisent_que_les_appels():
    """Les lentilles d'activité lisent `kind='mcp'` (`journal_jour.source` par défaut) :
    une ligne de relevé n'est l'activité de personne."""
    import inspect
    assert inspect.signature(journal_jour.source).parameters["kinds"].default == ("mcp",)
    assert journal_jour.KIND_RELEVE != "mcp"
