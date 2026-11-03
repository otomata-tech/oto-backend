"""Le plafond par lancement Apify d'une org : l'admin le pose, dans les bornes de la
plateforme (décision du 03/11/2026).

Bancs SANS base : `connector_settings` est doublé par un dictionnaire, la présence d'une
clé Apify de l'org par un ensemble. Ce que le plafond fait à un lancement :
`tests/test_apify_spend_control.py`.
"""
from __future__ import annotations

import datetime as dt
import math

import pytest

from oto_mcp import org_store, roles
from oto_mcp.capabilities import org_console
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx
from oto_mcp.capabilities.connectors import apify_charge_cap as cap
from oto_mcp.capabilities.connectors import access_level
from oto_mcp.capabilities.registry import CAPABILITIES
from oto_mcp.db import connector_settings
from oto_mcp.tools import apify

ORG = 3
CTX = ResolvedCtx(sub="admin-x", org_id=ORG)
CLES_PROPRES: set = set()
PLATEFORME: dict = {}


@pytest.fixture(autouse=True)
def base(monkeypatch):
    lignes: dict = {}
    CLES_PROPRES.clear()
    PLATEFORME.clear()
    monkeypatch.setattr(apify, "_reglages_lus", {})
    monkeypatch.setattr(org_store, "get_org", lambda oid: {"id": oid} if oid == ORG else None)
    monkeypatch.setattr(org_store, "has_org_secret",
                        lambda oid, prov: (oid, prov) in CLES_PROPRES)
    monkeypatch.setattr(connector_settings, "get_connector_setting",
                        lambda _t, _i, _c, cle: PLATEFORME.get(cle))

    def _poser(t, i, c, k, v, set_by=None):
        lignes[(t, str(i), c, k)] = {
            "scope_type": t, "scope_id": str(i), "connector": c, "key": k, "value": v,
            "set_by": set_by, "set_at": dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc)}
    monkeypatch.setattr(connector_settings, "set_connector_setting", _poser)
    monkeypatch.setattr(connector_settings, "list_connector_settings",
                        lambda key=None: [r for r in lignes.values() if r["key"] == key])
    monkeypatch.setattr(connector_settings, "org_settings_for_key",
                        lambda oid, key: {r["connector"]: r["value"] for r in lignes.values()
                                          if r["key"] == key and r["scope_type"] == "org"
                                          and r["scope_id"] == str(oid)})
    return lignes


def _poser(usd):
    return cap._set(CTX, cap.OrgChargeCapSetInput(org_id=ORG, usd=usd))


def _refus(usd) -> AuthzDenied:
    with pytest.raises(AuthzDenied) as e:
        _poser(usd)
    return e.value


def test_sans_reglage_le_defaut_de_vingt_cinq_dollars():
    lu = cap._get(CTX, cap.OrgChargeCapInput(org_id=ORG))
    assert lu["usd"] is None and lu["default_usd"] == 25.0
    assert (lu["own_key"], lu["bound"], lu["bound_usd"]) == (False, "platform", 25.0)
    assert lu["platform_key_usd"] == 25.0 and lu["own_key_usd"] == 25.0
    assert lu["set_by"] is None


def test_l_admin_pose_dix_dollars_et_le_journal_dit_qui(base):
    out = _poser(10)
    assert base[("org", str(ORG), "apify", apify.CLE_PLAFOND_ORG)]["value"] == "10.0"
    assert out["usd"] == 10.0 and out["platform_key_usd"] == 10.0 and out["own_key_usd"] == 10.0
    assert out["set_by"] == "admin-x" and out["set_at"].startswith("2026-11-03")
    assert cap._get(CTX, cap.OrgChargeCapInput(org_id=ORG)) == out


@pytest.mark.parametrize("usd", [0, -1, math.inf, math.nan])
def test_une_valeur_invalide_est_refusee(base, usd):
    e = _refus(usd)
    assert (e.status, e.code) == (400, "invalid_charge_cap")
    assert not base


def test_sans_cle_propre_au_dela_du_maximum_est_refuse_en_le_disant(base):
    e = _refus(40)
    assert (e.status, e.code) == (409, "charge_cap_above_platform_max")
    assert "$25" in e.message and "no Apify key of its own" in e.message
    assert not base


def test_le_maximum_de_la_plateforme_suit_son_reglage():
    PLATEFORME[apify.CLE_PLAFOND_MAX] = "50"
    assert _poser(40)["platform_key_usd"] == 40.0


def test_avec_sa_cle_l_org_pose_quarante_et_la_cle_plateforme_reste_bornee():
    CLES_PROPRES.add((ORG, "apify"))
    out = _poser(40)
    assert (out["own_key"], out["bound"], out["bound_usd"]) == (True, "byok", 1000.0)
    assert out["own_key_usd"] == 40.0 and out["platform_key_usd"] == 25.0


def test_avec_sa_cle_au_dela_de_la_borne_de_securite_est_refuse(base):
    CLES_PROPRES.add((ORG, "apify"))
    e = _refus(2000)
    assert (e.status, e.code) == (409, "charge_cap_above_byok_bound")
    assert "$1000" in e.message and not base


def test_une_valeur_posee_malgre_tout_au_dela_est_bornee_a_la_lecture(base):
    base[("org", str(ORG), "apify", apify.CLE_PLAFOND_ORG)] = {
        "scope_type": "org", "scope_id": str(ORG), "connector": "apify",
        "key": apify.CLE_PLAFOND_ORG, "value": "2000", "set_by": None, "set_at": None}
    lu = cap._get(CTX, cap.OrgChargeCapInput(org_id=ORG))
    assert lu["usd"] == 2000.0 and lu["own_key_usd"] == 1000.0
    assert lu["platform_key_usd"] == 25.0


def test_une_valeur_illisible_en_base_leve(base):
    base[("org", str(ORG), "apify", apify.CLE_PLAFOND_ORG)] = {
        "scope_type": "org", "scope_id": str(ORG), "connector": "apify",
        "key": apify.CLE_PLAFOND_ORG, "value": "beaucoup", "set_by": None, "set_at": None}
    with pytest.raises(RuntimeError, match=apify.CLE_PLAFOND_ORG):
        cap._get(CTX, cap.OrgChargeCapInput(org_id=ORG))


@pytest.mark.parametrize("verbe", ["get", "set"])
def test_un_autre_connecteur_est_refuse_par_son_nom(base, verbe):
    with pytest.raises(AuthzDenied) as e:
        if verbe == "get":
            cap._get(CTX, cap.OrgChargeCapInput(org_id=ORG, name="serper"))
        else:
            cap._set(CTX, cap.OrgChargeCapSetInput(org_id=ORG, name="serper", usd=10))
    assert (e.value.status, e.value.code) == (404, "no_charge_cap")
    assert "serper" in e.value.message and not base


def test_la_face_rest_ne_nomme_aucun_connecteur():
    for cle in ("connectors.apify.charge_cap.org_get", "connectors.apify.charge_cap.set_org"):
        assert _cap(cle).rest.path == "/api/orgs/{id}/connectors/{name}/charge-cap"


def test_une_org_inconnue_est_refusee():
    with pytest.raises(AuthzDenied) as e:
        cap._get(CTX, cap.OrgChargeCapInput(org_id=99))
    assert (e.value.status, e.value.code) == (404, "unknown_org")


# --- qui peut le poser : la même règle que le niveau d'accès d'org ---------------------

def _cap(cle):
    return next(c for c in CAPABILITIES if c.key == cle)


def test_la_pose_a_la_regle_d_autorisation_du_niveau_d_acces(monkeypatch):
    from oto_mcp import access
    admins = {"admin-x"}
    from oto_mcp.capabilities import _authz
    monkeypatch.setattr(access, "get_user_role", lambda sub: "user")
    # le refus nomme les admins de l'org (base) : sa phrase est éprouvée ailleurs
    monkeypatch.setattr(_authz, "_refus_org_admin",
                        lambda org_id, *a, **k: AuthzDenied(403, "forbidden", "admin"))
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, oid: sub in admins and oid == ORG)
    monkeypatch.setattr(roles, "is_org_member", lambda sub, oid: oid == ORG)
    pose = _cap("connectors.apify.charge_cap.set_org").authz
    niveau = _cap("connectors.access_level.set_org").authz
    entree = cap.OrgChargeCapSetInput(org_id=ORG, usd=10)
    assert pose(RawCtx(sub="admin-x"), entree).org_id == ORG
    for regle, inp in ((pose, entree),
                       (niveau, access_level.OrgAccessLevelSetInput(
                           org_id=ORG, name="zoho", level="read"))):
        with pytest.raises(AuthzDenied) as e:
            regle(RawCtx(sub="membre-y"), inp)
        assert e.value.status == 403
    # un membre lit le plafond qui le refuse
    lire = _cap("connectors.apify.charge_cap.org_get").authz
    assert lire(RawCtx(sub="membre-y"), cap.OrgChargeCapInput(org_id=ORG)).org_id == ORG


def test_une_identite_de_service_est_refusee_comme_pour_le_niveau_d_acces(monkeypatch):
    """Un agent hébergé qui ne porte pas de compte : la règle d'org admin le refuse
    avant le handler, ici comme pour `connectors.access_level.set_org`."""
    from oto_mcp.auth import service_identity
    monkeypatch.setattr(service_identity, "current", lambda: object())
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, oid: True)
    for cle, inp in (("connectors.apify.charge_cap.set_org",
                      cap.OrgChargeCapSetInput(org_id=ORG, usd=10)),
                     ("connectors.access_level.set_org",
                      access_level.OrgAccessLevelSetInput(org_id=ORG, name="zoho",
                                                          level="read"))):
        with pytest.raises(AuthzDenied) as e:
            _cap(cle).authz(RawCtx(sub="agent-z"), inp)
        assert (e.value.status, e.value.code) == (403, "service_forbidden")


def test_la_face_rest_et_la_face_mcp(base):
    assert _cap("connectors.apify.charge_cap.set_org").rest.verb == "PUT"
    assert _cap("connectors.apify.charge_cap.org_get").rest.path == cap._PATH
    sortie = org_console._org_settings(CTX, org_console.OrgSettingsInput(
        op="set", domain="apify_charge_cap", org_id=ORG, usd=12))
    assert sortie["usd"] == 12.0
    with pytest.raises(AuthzDenied) as e:
        org_console._org_settings(CTX, org_console.OrgSettingsInput(
            op="set", domain="apify_charge_cap", org_id=ORG))
    assert e.value.code == "missing_usd"
