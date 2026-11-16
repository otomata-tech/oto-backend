"""`oto_connector(op="setup_link", name=…)` : le lien de la carte d'UN connecteur du
catalogue, pour qu'une personne y pose sa clé elle-même — jamais dans la conversation.

Ce qui est vérifié : le lien suit le produit de l'appelant (son sub, son org), il ne
porte aucun secret ni aucun texte choisi par le modèle, les champs annoncés sont ceux
de la carte (noms, jamais de valeur), et chaque issue sans lien est NOMMÉE.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import links, providers
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.connectors import console, selection as CS
from oto_mcp.capabilities.connectors import setup_link as SL

_CTX = ResolvedCtx(sub="u-lien", org_id=7)


def _ligne(name: str) -> dict:
    return next(c for c in providers.public_catalog() if c["name"] == name)


@pytest.fixture()
def catalogue(monkeypatch):
    lignes = [_ligne(n) for n in ("hunter", "hubspot", "api", "linkedin_unipile")]
    monkeypatch.setattr(CS, "_visible_catalog", lambda ctx: lignes)
    return lignes


@pytest.fixture()
def page(monkeypatch):
    vus = []

    def _link_for(kind, sub=None, **p):
        vus.append((kind, sub, p))
        if kind == "connect":
            return None                  # this product has no one-connector page
        return f"https://app.example.test/org/{p.get('org')}/connectors"
    monkeypatch.setattr(links, "link_for", _link_for)
    return vus


def test_le_lien_ouvre_la_carte_du_connecteur(catalogue, page):
    out = SL.setup_link(_CTX, "hunter")
    assert out["url"] == "https://app.example.test/org/7/connectors/hunter?via=agent"
    assert out["connector"] == "hunter" and out["method"] == "secret"
    # Le produit de l'APPELANT : son sub et son org.
    assert page == [("connect", "u-lien", {"org": 7, "connector": "hunter"}),
                    ("connectors", "u-lien", {"org": 7})]
    # Les champs du formulaire, sans valeur ; le lien ne porte qu'un marqueur FIXE.
    assert out["fields"] == [{"name": "key", "label": "API key", "secret": True,
                              "required": True}]
    assert out["url"].split("?", 1)[1] == "via=agent"
    assert "Never ask for a key" in out["instructions"]
    assert 'oto_connector(op="list", name="hunter")' in out["instructions"]
    assert "url_missing" not in out


def test_un_libelle_trouve_le_connecteur(catalogue, page):
    assert SL.setup_link(_CTX, "HubSpot")["connector"] == "hubspot"


def test_connexion_hebergee_dit_le_geste_et_le_compte(catalogue, page):
    out = SL.setup_link(_CTX, "linkedin_unipile")
    assert out["method"] == "hosted" and out["fields"] == []
    assert out["credential_of"] == "unipile"
    assert "hosted window" in out["instructions"]


def test_nom_inconnu_refus_nomme(catalogue, page):
    with pytest.raises(AuthzDenied) as e:
        SL.setup_link(_CTX, "hunterr")
    assert e.value.code == "unknown_connector"
    assert "hunter" in e.value.details["suggestions"]


def test_le_custom_api_renvoie_a_son_propre_lien(catalogue, page):
    with pytest.raises(AuthzDenied) as e:
        SL.setup_link(_CTX, "api")
    assert e.value.code == "use_http_setup_link"


def test_nom_ambigu_refuse_plutot_que_choisir(monkeypatch, page):
    a, b = dict(_ligne("hunter")), dict(_ligne("hubspot"))
    monkeypatch.setattr(CS, "_visible_catalog", lambda ctx: [a, b])
    monkeypatch.setattr(CS, "_resoudre_nom", lambda cat, q: [a, b])
    with pytest.raises(AuthzDenied) as e:
        SL.setup_link(_CTX, "h")
    assert e.value.code == "ambiguous_connector"
    assert e.value.details["candidates"] == ["hunter", "hubspot"]


def test_sans_page_connecteurs_la_raison_est_dite(catalogue, monkeypatch):
    monkeypatch.setattr(links, "link_for", lambda *a, **k: None)
    monkeypatch.setattr(links, "raison_sans_lien", lambda *a, **k: "no connectors page")
    out = SL.setup_link(_CTX, "hunter")
    assert out["url"] is None and out["url_missing"] == "no connectors page"


def test_la_console_route_l_op(catalogue, page):
    out = asyncio.run(console._connector(
        _CTX, console.ConnectorInput(op="setup_link", name="hunter")))
    assert out["connector"] == "hunter"
    with pytest.raises(AuthzDenied) as e:
        asyncio.run(console._connector(_CTX, console.ConnectorInput(op="setup_link")))
    assert e.value.code == "missing_name"


def test_la_page_d_un_seul_connecteur_quand_le_produit_la_declare(catalogue, monkeypatch):
    def _link_for(kind, sub=None, **p):
        if kind == "connect":
            return f"https://app.example.test/org/{p['org']}/connect/{p['connector']}"
        return "https://app.example.test/connectors"
    monkeypatch.setattr(links, "link_for", _link_for)
    assert SL.setup_link(_CTX, "hunter")["url"] == \
        "https://app.example.test/org/7/connect/hunter"


def test_le_patron_connect_se_declare_par_instance(monkeypatch):
    monkeypatch.setenv(links.ENV_INSTANCE_PATHS,
                       '{"connect": "/org/{org}/connect/{connector}"}')
    assert links.instance_paths()["connect"] == "/org/{org}/connect/{connector}"
    monkeypatch.delenv(links.ENV_INSTANCE_PATHS)
    assert "connect" not in links.instance_paths()
