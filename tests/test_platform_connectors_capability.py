"""Le palier PLATEFORME des connecteurs, en capacité : mêmes chemins, mêmes codes.

Les cinq routes `/api/admin/connectors/activation` et
`/api/admin/connectors/{provider}/platform-access` ont quitté `api_routes_connectors.py`
pour `capabilities/platform_connectors.py` (27/08). C'est l'étage qui manquait : les
paliers org et équipe de la même famille étaient déjà des capacités
(`capabilities/connectors_activation.py`).

Ce que ce fichier garde :

- **Les codes de refus écrits à la main.** `unknown_connector`, `enabled_must_be_bool`,
  `connector_and_org_id_required`, `org_id_must_be_int`, `invalid_body`, `unknown_org`,
  `unknown_user`, `no_platform_access` : huit jetons machine servis à la console admin.
  Pydantic les remplacerait tous par un `invalid_input` générique — d'où des champs
  déclarés facultatifs et une validation au handler (cf. le module).
- **`restart_required`**, qui ne vaut que pour le master GLOBAL : le chargement des
  tools est résolu au boot. Un override d'org, lui, prend effet tout de suite.
- **`enabled: null` = OFF**, pas « indéterminé » (deny-by-default).
"""
from __future__ import annotations

import pytest

from _datastore_rest import call, stub_authz

from oto_mcp.capabilities import platform_connectors as pc


class _Conn:
    def __init__(self, label, help_, ns):
        self.label, self.help, self.namespaces = label, help_, ns


_REGISTRE = {"serper": _Conn("Serper", "Recherche web", ["serper"]),
             "unipile": _Conn("Unipile", "Messagerie hébergée", ["unipile", "linkedin"])}


@pytest.fixture()
def socle(monkeypatch):
    vus: list = []
    monkeypatch.setattr(pc.providers, "REGISTRY", _REGISTRE)
    monkeypatch.setattr(pc.access, "paid_option_for", lambda n: "messagerie")
    monkeypatch.setattr(pc.connector_activation, "list_activations", lambda: [
        {"connector": "serper", "org_id": None, "enabled": True},
        {"connector": "serper", "org_id": 35, "enabled": False},
    ])
    monkeypatch.setattr(pc.connector_activation, "set_activation",
                        lambda c, e, org_id=None, set_by=None:
                        vus.append(("set", c, e, org_id, set_by)))
    monkeypatch.setattr(pc.connector_activation, "clear_activation",
                        lambda c, oid: vus.append(("clear", c, oid)))
    from oto_mcp import credentials_store
    monkeypatch.setattr(credentials_store, "list_platform_instances",
                        lambda p: [{"share_mode": "grants",
                                    "share_down": ["org:35", "user:u-9", "group:2"]}])
    monkeypatch.setattr(credentials_store, "platform_grant",
                        lambda p, g: vus.append(("grant", p, g)))
    monkeypatch.setattr(credentials_store, "platform_revoke",
                        lambda p, g: vus.append(("revoke", p, g)))
    monkeypatch.setattr(pc.db, "list_option_comps_for_option", lambda o: [
        {"entity_type": "org", "entity_id": 35},
        {"entity_type": "user", "entity_id": "u-9"}])
    monkeypatch.setattr(pc.db, "set_option_comp",
                        lambda s, i, o, granted_by=None: vus.append(("comp+", s, i, o)))
    monkeypatch.setattr(pc.db, "clear_option_comp",
                        lambda s, i, o: vus.append(("comp-", s, i, o)))
    monkeypatch.setattr(pc.db, "get_user",
                        lambda sub: {"name": "Zoé", "email": "z@b.c"} if sub == "u-9" else None)
    monkeypatch.setattr(pc.org_store, "get_org",
                        lambda oid: {"id": oid, "name": "Otomata"} if oid == 35 else None)
    monkeypatch.setattr(pc.org_store, "effective_logo_url", lambda o: "https://cdn/l.png")
    return vus


@pytest.fixture()
def admin(monkeypatch):
    """Opérateur plateforme ET super admin — le cas nominal des cinq chemins."""
    stub_authz(monkeypatch)
    from oto_mcp.capabilities import _authz
    monkeypatch.setattr(_authz.access, "is_platform_operator", lambda sub: True)
    monkeypatch.setattr(_authz.access, "is_super_admin", lambda sub: True)


# --- Le cran d'activation ---------------------------------------------------

def test_l_admin_voit_TOUT_le_registre_meme_ce_qui_est_off(socle, admin):
    """C'est sa surface pour activer : un connecteur absent de la liste serait
    inactivable depuis l'UI."""
    code, out = call("platform.connector.activation_list")
    assert code == 200, out
    assert {c["connector"] for c in out["connectors"]} == {"serper", "unipile"}


def test_enabled_null_veut_dire_off_pas_indetermine(socle, admin):
    """Deny-by-default : le master n'a jamais été posé ⇒ le connecteur n'est pas
    exposé. Un front qui lit `null` comme « activé » se trompe — d'où le `Output`."""
    _, out = call("platform.connector.activation_list")
    par_nom = {c["connector"]: c for c in out["connectors"]}
    assert par_nom["unipile"]["enabled"] is None      # jamais posé
    assert par_nom["serper"]["enabled"] is True
    assert par_nom["serper"]["overrides"] == [{"org_id": 35, "enabled": False}]


def test_le_master_global_demande_un_redemarrage_pas_l_override(socle, admin):
    """⚠️ La distinction porte tout le sens de cette console : le chargement des tools
    est résolu au boot, donc basculer le master GLOBAL est écrit mais pas encore servi.
    Un override d'ORG est lu à la résolution et prend effet tout de suite."""
    code, out = call("platform.connector.activation_set",
                     body={"connector": "serper", "enabled": True})
    assert (code, out["restart_required"], out["org_id"]) == (200, True, None)
    code, out = call("platform.connector.activation_set",
                     body={"connector": "serper", "enabled": False, "org_id": 35})
    assert (code, out["restart_required"], out["org_id"]) == (200, False, 35)


@pytest.mark.parametrize("corps,attendu", [
    ({"connector": "zzz", "enabled": True}, "unknown_connector"),
    ({"connector": "serper"}, "enabled_must_be_bool"),
    ({}, "unknown_connector"),
])
def test_les_refus_de_pose_gardent_leur_code_machine(socle, admin, corps, attendu):
    """Pydantic rendrait `invalid_input` pour les trois. La console admin lit ces
    jetons-là : les remplacer casserait ses messages."""
    code, out = call("platform.connector.activation_set", body=corps)
    assert code == 400 and out["error"] == attendu
    assert socle == [], "rien ne doit être écrit quand la pose est refusée"


@pytest.mark.parametrize("query,attendu", [
    (b"connector=serper", "connector_and_org_id_required"),
    (b"org_id=35", "connector_and_org_id_required"),
    (b"", "connector_and_org_id_required"),
    (b"connector=serper&org_id=zz", "org_id_must_be_int"),
])
def test_les_refus_de_retrait_d_override_aussi(socle, admin, query, attendu):
    code, out = call("platform.connector.activation_clear", query=query)
    assert code == 400 and out["error"] == attendu


def test_retirer_un_override_le_transmet_converti(socle, admin):
    code, out = call("platform.connector.activation_clear",
                     query=b"connector=serper&org_id=35")
    assert (code, out) == (200, {"ok": True, "connector": "serper", "org_id": 35})
    assert socle == [("clear", "serper", 35)], "l'org_id arrive en ENTIER au store"


# --- L'accès plateforme -----------------------------------------------------

def test_la_vue_reunit_les_deux_couches(socle, admin):
    """Clé plateforme (couche 2) et option offerte (couche 3) sont indépendantes : un
    bénéficiaire peut porter l'une, l'autre, ou les deux."""
    code, out = call("platform.connector.access_list", path_params={"provider": "unipile"})
    assert code == 200, out
    assert set(out) == set(pc.PlatformAccessView.model_fields)
    par_cle = {(b["scope"], b["id"]): b for b in out["beneficiaries"]}
    assert par_cle[("org", "35")]["has_key"] and par_cle[("org", "35")]["has_option"]
    assert par_cle[("org", "35")]["label"] == "Otomata"
    assert par_cle[("user", "u-9")]["email"] == "z@b.c"
    assert ("group", "2") not in par_cle, "seuls org et user sont des bénéficiaires"


def test_les_deux_couches_sont_independantes(socle, admin, monkeypatch):
    """Le cas à trois branches, repris de `test_platform_access_surface.py` (fondu ici) :
    une org qui a la CLÉ et l'OPTION, un membre qui n'a que la clé, un autre qui n'a que
    l'option. Les deux drapeaux sont indépendants — l'un sans l'autre est un état normal,
    pas une incohérence, et c'est ce que la vue doit laisser voir."""
    from oto_mcp import credentials_store
    monkeypatch.setattr(credentials_store, "list_platform_instances",
                        lambda p: [{"share_mode": "closed",
                                    "share_down": ["org:42", "user:u1"]}])
    monkeypatch.setattr(pc.db, "list_option_comps_for_option", lambda o: [
        {"entity_type": "org", "entity_id": "42"},
        {"entity_type": "user", "entity_id": "u2"}])
    monkeypatch.setattr(pc.org_store, "get_org", lambda oid: {"id": oid, "name": f"Org{oid}"})
    monkeypatch.setattr(pc.org_store, "effective_logo_url", lambda o: None)
    monkeypatch.setattr(pc.db, "get_user", lambda s: {"name": None, "email": f"{s}@x.io"})

    code, out = call("platform.connector.access_list", path_params={"provider": "unipile"})
    assert code == 200 and out["platform_key"] is True
    by = {(b["scope"], b["id"]): b for b in out["beneficiaries"]}
    assert by[("org", "42")]["has_key"] and by[("org", "42")]["has_option"]
    assert by[("org", "42")]["label"] == "Org42"
    assert by[("user", "u1")]["has_key"] and not by[("user", "u1")]["has_option"]
    assert by[("user", "u2")]["has_option"] and not by[("user", "u2")]["has_key"]
    # Le libellé retombe sur l'email quand le compte n'a pas de nom.
    assert by[("user", "u2")]["label"] == "u2@x.io" == by[("user", "u2")]["email"]


def test_une_org_fantome_garde_un_libelle_lisible(socle, admin, monkeypatch):
    """Un grant vers une org supprimée depuis : la ligne reste, avec `org #<id>` — la
    faire disparaître cacherait le grant à nettoyer."""
    monkeypatch.setattr(pc.org_store, "get_org", lambda oid: None)
    _, out = call("platform.connector.access_list", path_params={"provider": "unipile"})
    org = next(b for b in out["beneficiaries"] if b["scope"] == "org")
    assert org["label"] == "org #35" and org["logo_url"] is None


def test_open_tier_change_la_lecture_de_la_liste(socle, admin, monkeypatch):
    """Une instance en partage `open` ouvre le connecteur à TOUS sans grant : la liste
    ne dit alors plus la population servie, seulement les grants explicites. Le flag
    est ce qui empêche un admin de conclure « seules ces orgs y ont accès »."""
    from oto_mcp import credentials_store
    monkeypatch.setattr(credentials_store, "list_platform_instances",
                        lambda p: [{"share_mode": "open", "share_down": []}])
    _, out = call("platform.connector.access_list", path_params={"provider": "unipile"})
    assert out["open_tier"] is True
    # Les bénéficiaires restants ne viennent QUE de la couche option : aucun grant de
    # clé n'existe (`share_down` vide), et pourtant le connecteur est servi à tous.
    # C'est exactement ce que le flag sert à ne pas confondre.
    assert all(b["has_key"] is False and b["has_option"] is True
               for b in out["beneficiaries"])


def test_un_connecteur_inconnu_est_un_404(socle, admin):
    code, out = call("platform.connector.access_list", path_params={"provider": "zzz"})
    assert code == 404 and out["error"] == "unknown_connector"


def test_ouvrir_l_acces_pose_les_DEUX_leviers_ensemble(socle, admin):
    """C'est tout l'objet de l'acte unique (ADR 0044 §H) : le backend couplait déjà
    option et grant de clé, l'API ne demandait qu'un seul geste — les deux partent."""
    code, out = call("platform.connector.access_set",
                     path_params={"provider": "unipile"},
                     body={"scope": "org", "id": 35, "on": True})
    assert code == 200 and out["ok"] is True
    assert socle == [("comp+", "org", "35", "messagerie"), ("grant", "unipile", "org:35")]


def test_fermer_retire_les_deux(socle, admin):
    call("platform.connector.access_set", path_params={"provider": "unipile"},
         body={"scope": "user", "id": "u-9", "on": False})
    assert socle == [("comp-", "user", "u-9", "messagerie"),
                     ("revoke", "unipile", "user:u-9")]


def test_un_id_numerique_est_normalise_en_texte(socle, admin):
    """Un id d'org arrive en NOMBRE depuis le front, un sub en TEXTE : les deux formes
    étaient acceptées via `str(body.get("id", ""))` et le restent."""
    _, out = call("platform.connector.access_set", path_params={"provider": "unipile"},
                  body={"scope": "org", "id": 35, "on": True})
    assert out["id"] == "35"


@pytest.mark.parametrize("corps,code,erreur", [
    ({"scope": "planete", "id": "x", "on": True}, 400, "invalid_body"),
    ({"scope": "org", "id": "", "on": True}, 400, "invalid_body"),
    ({"scope": "org", "id": 999, "on": True}, 404, "unknown_org"),
    ({"scope": "user", "id": "u-zz", "on": True}, 404, "unknown_user"),
])
def test_pas_de_grant_vers_un_fantome(socle, admin, corps, code, erreur):
    got, out = call("platform.connector.access_set",
                    path_params={"provider": "unipile"}, body=corps)
    assert (got, out["error"]) == (code, erreur)
    assert socle == []


def test_un_connecteur_sans_option_ni_cle_n_a_rien_a_ouvrir(socle, admin, monkeypatch):
    monkeypatch.setattr(pc.access, "paid_option_for", lambda n: None)
    from oto_mcp import credentials_store
    monkeypatch.setattr(credentials_store, "list_platform_instances", lambda p: [])
    code, out = call("platform.connector.access_set",
                     path_params={"provider": "unipile"},
                     body={"scope": "org", "id": 35, "on": True})
    assert code == 400 and out["error"] == "no_platform_access"


def test_un_corps_illisible_rend_le_meme_invalid_body_qu_avant(socle, admin):
    """L'adaptateur avale un corps JSON illisible (traité comme absent) ⇒ `scope` est
    None ⇒ `invalid_body`. Le code servi est donc INCHANGÉ ici, contrairement à
    d'autres chemins du même chantier."""
    code, out = call("platform.connector.access_set",
                     path_params={"provider": "unipile"}, body="{pas du json")
    assert code == 400 and out["error"] == "invalid_body"


# --- Les paliers d'autz -----------------------------------------------------

def test_la_lecture_exige_un_operateur_plateforme(socle, monkeypatch):
    stub_authz(monkeypatch)
    from oto_mcp.capabilities import _authz
    monkeypatch.setattr(_authz.access, "is_platform_operator", lambda sub: False)
    code, out = call("platform.connector.activation_list")
    assert code == 403 and out["error"] == "forbidden"


def test_ouvrir_un_acces_exige_le_SUPER_admin_pas_un_admin(socle, monkeypatch):
    """La lecture est ouverte à l'admin opérationnel ; l'acte qui engage la plateforme
    (une clé, une option offerte) est réservé au super admin."""
    stub_authz(monkeypatch)
    from oto_mcp.capabilities import _authz
    monkeypatch.setattr(_authz.access, "is_platform_operator", lambda sub: True)
    monkeypatch.setattr(_authz.access, "is_super_admin", lambda sub: False)
    assert call("platform.connector.access_list",
                path_params={"provider": "unipile"})[0] == 200
    code, out = call("platform.connector.access_set",
                     path_params={"provider": "unipile"},
                     body={"scope": "org", "id": 35, "on": True})
    assert code == 403 and out["error"] == "forbidden"


def test_le_403_porte_desormais_un_detail(socle, monkeypatch):
    """**Écart visible, additif.** Le refus écrit à la main rendait `{"error":
    "forbidden"}` nu ; les règles d'autz de la couche capacité portent un message
    actionnable, et l'adaptateur le RENVOIE. C'est ce que font déjà les ~60 capacités
    `/api/admin/*` — le nu était l'exception."""
    stub_authz(monkeypatch)
    from oto_mcp.capabilities import _authz
    monkeypatch.setattr(_authz.access, "is_platform_operator", lambda sub: False)
    _, out = call("platform.connector.activation_list")
    assert out == {"error": "forbidden", "detail": "Réservé à un admin plateforme."}
