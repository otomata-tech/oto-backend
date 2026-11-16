"""Lire et modifier un credential d'ÉQUIPE ou d'ORG sans détenir son secret (#448).

Le cas fondateur, vécu le 27/08 sur un pont client en production : il fallait changer
UNE valeur non secrète — la `base_url`, le pont ayant changé de machine. L'admin
autorisé s'est retrouvé devant un formulaire de douze champs TOUS VIDES, dont un
bearer qu'aucune surface ne pouvait relire, et l'écriture étant un remplacement total,
poser la nouvelle URL revenait à écraser la clé par du vide. Il a renoncé.

Deux défauts, un seul piège : sans lecture révélante au palier, le remplacement total
rend TOUTE modification partielle impossible. On grave les deux ensemble.
"""
import pytest

from _datastore_rest import call, stub_authz

from oto_mcp import credentials_store
from oto_mcp.capabilities import me_credentials as mc

ORG, GROUP = 35, 31
PONT = {"base_url": "http://172.16.16.3:8097", "auth_mode": "bearer", "token": "TOK-SECRET"}


@pytest.fixture()
def coffre(monkeypatch):
    """Un credential `api` d'équipe au coffre, et les droits qui vont avec."""
    monkeypatch.setattr(mc.access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(mc.access, "current_group", lambda sub: GROUP)
    monkeypatch.setattr(mc.roles, "can_admin_group", lambda sub, gid: True)
    monkeypatch.setattr(mc.roles, "is_org_admin", lambda sub, oid: True)
    stored = {("group", str(GROUP)): credentials_store.pack_secret("api", PONT)}

    def _ligne(et, eid, con, account=""):
        secret = stored.get((et, eid))
        return ({"secret": secret, "meta": {}, "set_at": "2026-08-27T10:00:00+00:00",
                 "set_by": "u-admin"} if secret else None)

    monkeypatch.setattr(mc.credentials_store, "get_credential_with_meta", _ligne)
    return stored


def _get(provider="api", query=b""):
    return call("me.credential.get", path_params={"provider": provider}, query=query)


# --- La lecture révélante existe hors du palier membre ----------------------

def test_le_palier_equipe_rend_les_champs_non_secrets(monkeypatch, coffre):
    """Ce que le registre déclare NON secret sort — c'est ce qui manquait.

    ⚠️ Ce test a dit « ce que le registre déclare `reveal=True` » jusqu'au 2026-08-31 :
    le cran existait alors, et il ouvrait aussi la valeur des champs SECRETS. Il est
    retiré (#671) — seul `secret=False` décide, et c'est ce qui a toujours suffi ici."""
    stub_authz(monkeypatch)
    code, out = _get(query=b"scope=group")
    assert code == 200, out
    assert out["base_url"] == "http://172.16.16.3:8097"
    assert out["auth_mode"] == "bearer"
    assert out["read_scope"] == "group"


def test_lenveloppe_necrase_pas_un_champ_du_connecteur(monkeypatch, coffre):
    """`api` déclare un champ nommé `scope` (les scopes oauth2) et le corps rendu est
    PLAT : une clé d'enveloppe nommée `scope` aurait mangé la valeur servie. Les
    clés d'enveloppe sont donc préfixées, et le champ du connecteur reste servi."""
    stub_authz(monkeypatch)
    _, out = _get(query=b"scope=group")
    assert out["read_scope"] == "group"
    assert "scope" in out and out["scope"] is None   # le champ oauth2, non renseigné


def test_le_secret_ne_sort_toujours_pas(monkeypatch, coffre):
    """La lecture s'ouvre au palier, pas au secret : le bearer reste au coffre.

    Ce qui le remplace depuis #671 : une empreinte qui le NOMME sans rien en dire."""
    stub_authz(monkeypatch)
    _, out = _get(query=b"scope=group")
    assert "token" not in out
    assert "TOK-SECRET" not in str(out)
    assert set(out["read_fingerprints"]) == {"token"}


def test_un_connecteur_byo_org_nest_plus_inconnu(monkeypatch, coffre):
    """`api` est `byo_org` pur : la garde d'éligibilité le déclarait « inconnu »
    à TOUS les paliers, donc les ponts clients (ADR 0003/0037) n'avaient aucune
    surface de lecture. C'est la racine du formulaire vide."""
    stub_authz(monkeypatch)
    code, _ = _get(query=b"scope=group")
    assert code == 200


def test_sans_le_droit_dadmin_dequipe_cest_refuse(monkeypatch, coffre):
    stub_authz(monkeypatch)
    monkeypatch.setattr(mc.roles, "can_admin_group", lambda sub, gid: False)
    monkeypatch.setattr("oto_mcp.group_store.get_group", lambda gid: None)  # oto#108 : sans annuaire
    code, out = _get(query=b"scope=group")
    monkeypatch.setattr("oto_mcp.detenteurs.est_membre", lambda *a: False)  # oto#108 : sans annuaire
    assert code == 403, out


def test_rien_de_pose_a_ce_palier_le_dit_par_palier(monkeypatch, coffre):
    stub_authz(monkeypatch)
    code, out = _get(query=b"scope=org")
    assert code == 404
    assert "org" in out["detail"]


# --- L'écriture complète l'existant au lieu de le remplacer -----------------

@pytest.fixture()
def au_coffre(monkeypatch):
    monkeypatch.setattr(credentials_store, "get_credential_with_meta",
                        lambda et, eid, con, account="":
                        {"secret": credentials_store.pack_secret("api", PONT), "meta": {}})


def test_changer_lurl_ne_touche_pas_a_la_cle(au_coffre):
    """Le geste que l'issue réclame : un seul champ envoyé, le secret préservé."""
    merged = credentials_store.merge_with_existing(
        "group", str(GROUP), "api", "", {"base_url": "http://127.0.0.1:8097"})
    assert merged["base_url"] == "http://127.0.0.1:8097"
    assert merged["token"] == "TOK-SECRET"
    assert merged["auth_mode"] == "bearer"


def test_un_champ_envoye_vide_est_un_effacement_explicite(au_coffre):
    """La règle qui garde le formulaire du dashboard intact : ce qui est ABSENT est
    complété, ce qui est PRÉSENT et vide est effacé. Sans cette distinction, on ne
    pourrait plus jamais vider un champ."""
    merged = credentials_store.merge_with_existing(
        "group", str(GROUP), "api", "", {"base_url": "http://127.0.0.1:8097",
                                          "token": ""})
    assert merged["token"] == ""


def test_une_saisie_complete_reste_un_remplacement(au_coffre):
    """Le dashboard poste tous ses champs : son comportement ne change pas."""
    complet = {"base_url": "https://api.test", "auth_mode": "bearer", "token": "NEUF"}
    assert credentials_store.merge_with_existing(
        "group", str(GROUP), "api", "", complet) == complet


def test_le_changement_de_mode_ne_ressuscite_pas_un_champ_mort(monkeypatch):
    """Merge et validation composent : passer d'oauth2 à bearer complète depuis le
    coffre PUIS écarte ce qu'aucun mode ne lit — pas de `client_secret` fantôme."""
    ancien = {"base_url": "https://api.test", "auth_mode": "oauth2",
              "token_url": "https://api.test/token", "client_id": "CID",
              "client_secret": "CSEC"}
    monkeypatch.setattr(credentials_store, "get_credential_with_meta",
                        lambda *a, **k: {"secret": credentials_store.pack_secret("api", ancien),
                                         "meta": {}})
    merged = credentials_store.merge_with_existing(
        "org", str(ORG), "api", "", {"auth_mode": "bearer", "token": "T"})
    kept = credentials_store.validate_fields("api", merged)
    assert "client_secret" not in kept and "token_url" not in kept
    assert kept["token"] == "T" and kept["base_url"] == "https://api.test"


def test_un_coffre_illisible_ne_bloque_pas_une_repose_complete(monkeypatch):
    """Une ligne écrite sous une clé de chiffrement périmée lève au déchiffrement.
    Ce n'est pas une raison d'interdire de la RÉÉCRIRE en entier."""
    def _boom(*a, **k):
        raise ValueError("InvalidTag")
    monkeypatch.setattr(credentials_store, "get_credential_with_meta", _boom)
    complet = {"base_url": "https://api.test", "auth_mode": "bearer", "token": "NEUF"}
    assert credentials_store.merge_with_existing(
        "group", str(GROUP), "api", "", complet) == complet


def test_un_connecteur_mono_champ_na_rien_a_completer(monkeypatch):
    monkeypatch.setattr(credentials_store, "get_credential_with_meta",
                        lambda *a, **k: pytest.fail("aucune lecture attendue"))
    assert credentials_store.merge_with_existing(
        "org", str(ORG), "serper", "", {"key": "K"}) == {"key": "K"}
