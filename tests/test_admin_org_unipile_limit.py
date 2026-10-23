"""`platform.org.unipile_limit_{get,set}` — le plafond de comptes de messagerie hébergés
d'une org, enfin lisible et posable ailleurs que par la synchronisation d'un plan.

La colonne `orgs.unipile_account_limit` existait et la connexion la lisait déjà
(`unipile_connect.hosted_auth_url`) ; son seul écrivain était `billing.py`. Un tenant qui
applique ses propres crédits (une org gratuite = un seul compte hébergé) n'avait donc aucun
moyen de la poser. Ce que ce fichier garde :

- **la vue dit ce que la connexion applique** : `effective_limit` est lu chez
  `unipile_connect.plafond_de_comptes` — `0` depuis le 2026-10-23, plus aucune org n'est
  plafonnée ; la colonne et le défaut restent lisibles et posables, inertes ;
- **`0` = sans plafond**, `null` = retour au défaut : deux sens distincts, tous deux posables ;
- **un PUT sans `limit` est refusé**, pas lu comme `null` (il lèverait un plafond en silence) ;
- **lecture = admin plateforme, écriture = super admin**, REST seule.
"""
from __future__ import annotations

import pytest

from _datastore_rest import call, stub_authz

from oto_mcp.capabilities import users_admin
from oto_mcp.capabilities.registry import CAPABILITIES

ORG = 35


@pytest.fixture()
def socle(monkeypatch):
    """Une org connue (35), un plafond en mémoire, un siège consommé."""
    etat = {"limit": None, "ecrit": []}

    def _set(org_id, limit):
        etat["ecrit"].append((org_id, limit))
        etat["limit"] = limit

    monkeypatch.setenv("OTO_MCP_UNIPILE_DEFAULT_LIMIT", "5")
    monkeypatch.setattr(users_admin.org_store, "get_org",
                        lambda oid: {"id": oid, "name": "Acme"} if oid == ORG else None)
    monkeypatch.setattr(users_admin.db, "get_org_unipile_limit", lambda oid: etat["limit"])
    monkeypatch.setattr(users_admin.db, "set_org_unipile_limit", _set)
    monkeypatch.setattr(users_admin.db, "count_unipile_accounts_for_org", lambda oid: 1)
    return etat


def _roles(monkeypatch, *, operateur: bool, super_admin: bool) -> None:
    stub_authz(monkeypatch)
    from oto_mcp.capabilities import _authz
    monkeypatch.setattr(_authz.access, "is_platform_operator", lambda sub: operateur)
    monkeypatch.setattr(_authz.access, "is_super_admin", lambda sub: super_admin)


@pytest.fixture()
def super_admin(monkeypatch):
    _roles(monkeypatch, operateur=True, super_admin=True)


def _get():
    return call("platform.org.unipile_limit_get", path_params={"id": str(ORG)})


def _put(body, org=ORG):
    return call("platform.org.unipile_limit_set", path_params={"id": str(org)}, body=body)


# --- La vue ------------------------------------------------------------------

def test_sans_plafond_propre_l_org_suit_le_defaut_plateforme(socle, super_admin):
    code, out = _get()
    assert code == 200, out
    assert out == {"org_id": ORG, "limit": None, "default_limit": 5,
                   "effective_limit": 0, "accounts": 1}
    assert set(out) == set(users_admin.OrgUnipileLimitView.model_fields)


def test_le_defaut_est_lu_chez_unipile_connect_mais_n_est_plus_applique(socle, super_admin, monkeypatch):
    """Une seule lecture de l'environnement : celle de `unipile_connect`. Le défaut reste
    affiché, mais la connexion n'applique plus aucun plafond (2026-10-23)."""
    from oto_mcp import unipile_connect
    monkeypatch.setenv("OTO_MCP_UNIPILE_DEFAULT_LIMIT", "3")
    _, out = _get()
    assert out["default_limit"] == unipile_connect._default_limit() == 3
    assert out["effective_limit"] == 0, "la connexion n'applique plus aucun plafond"


def test_poser_un_plafond_d_un_compte(socle, super_admin):
    code, out = _put({"limit": 1})
    assert code == 200, out
    assert socle["ecrit"] == [(ORG, 1)]
    assert (out["limit"], out["default_limit"], out["effective_limit"]) == (1, 5, 0)
    assert _get()[1] == out, "le PUT rend la même vue que la lecture"


def test_zero_veut_dire_sans_plafond_et_n_est_pas_absorbe_par_le_defaut(socle, super_admin):
    """La connexion teste `if limit and count >= limit` : 0 lève le plafond. La vue ne
    doit pas le confondre avec « pas de valeur » et retomber sur le défaut."""
    code, out = _put({"limit": 0})
    assert code == 200, out
    assert (out["limit"], out["effective_limit"]) == (0, 0)


def test_null_rend_l_org_au_defaut_plateforme(socle, super_admin):
    _put({"limit": 1})
    code, out = _put({"limit": None})
    assert code == 200, out
    assert socle["ecrit"] == [(ORG, 1), (ORG, None)]
    assert (out["limit"], out["effective_limit"]) == (None, 0)


# --- Les refus ---------------------------------------------------------------

def test_un_plafond_negatif_est_refuse(socle, super_admin):
    code, out = _put({"limit": -1})
    assert (code, out["error"]) == (400, "invalid_body")
    assert socle["ecrit"] == []


@pytest.mark.parametrize("corps", [{}, {"limit": True}, {"limit": "1"}, {"limit": 1.5}])
def test_un_corps_sans_entier_ni_null_n_ecrit_rien(socle, super_admin, corps):
    """Surtout le corps VIDE : lu comme `null`, il lèverait le plafond en silence."""
    code, _ = _put(corps)
    assert code == 400
    assert socle["ecrit"] == []


def test_une_org_inconnue_est_un_404_des_deux_cotes(socle, super_admin):
    code, out = call("platform.org.unipile_limit_get", path_params={"id": "999"})
    assert (code, out["error"]) == (404, "unknown_org")
    code, out = _put({"limit": 1}, org=999)
    assert (code, out["error"]) == (404, "unknown_org")
    assert socle["ecrit"] == []


# --- Les paliers d'autz ------------------------------------------------------

def test_un_admin_plateforme_lit_mais_ne_pose_pas(socle, monkeypatch):
    _roles(monkeypatch, operateur=True, super_admin=False)
    assert _get()[0] == 200
    code, out = _put({"limit": 1})
    assert (code, out["error"]) == (403, "forbidden")
    assert socle["ecrit"] == []


def test_un_membre_ne_lit_pas(socle, monkeypatch):
    _roles(monkeypatch, operateur=False, super_admin=False)
    code, out = _get()
    assert (code, out["error"]) == (403, "forbidden")


def test_rest_seule_jamais_en_mcp():
    caps = {c.key: c for c in CAPABILITIES}
    lire, poser = caps["platform.org.unipile_limit_get"], caps["platform.org.unipile_limit_set"]
    assert lire.mcp is None and poser.mcp is None
    assert (lire.rest.verb, lire.rest.path) == ("GET", "/api/admin/orgs/{id}/unipile-limit")
    assert (poser.rest.verb, poser.rest.path) == ("PUT", "/api/admin/orgs/{id}/unipile-limit")
