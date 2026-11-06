"""`oto_connector_app` — la carte « Connecter » : le nom du connecteur, le geste, un
bouton qui OUVRE la page du connecteur. Vrai prefab_ui, accès et liens stubbés.

Prouvé : le bouton porte le lien de `setup_link` (et rien d'autre : aucun outil
appelé), le modèle reçoit le même contenu en texte (deux canaux, `docs/mcp-apps.md`),
un refus devient une carte nommée, et un lien absent ne dessine pas de bouton.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastmcp import FastMCP

pytest.importorskip("prefab_ui")

from oto_mcp import access, links, providers  # noqa: E402
from oto_mcp.capabilities.connectors import selection as CS  # noqa: E402
from oto_mcp.connectors import credential_presence  # noqa: E402
from oto_mcp.tools import connector_app  # noqa: E402


def _ligne(name: str) -> dict:
    return next(c for c in providers.public_catalog() if c["name"] == name)


@pytest.fixture()
def outil(monkeypatch):
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "u-carte")
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    monkeypatch.setattr(CS, "_visible_catalog",
                        lambda ctx: [_ligne("hunter"), _ligne("hubspot")])
    presences: dict = {}
    monkeypatch.setattr(credential_presence, "lire",
                        lambda sub, org=None: (presences, "computed"))
    m = FastMCP("t")
    connector_app.register(m)
    fn = asyncio.run(m.get_tool("oto_connector_app")).fn
    fn.presences = presences
    return fn


def _carte(res) -> str:
    sc = res.structured_content
    return json.dumps(sc if isinstance(sc, dict) else sc.to_json(), default=str)


def test_le_bouton_ouvre_la_page_du_connecteur(outil, monkeypatch):
    monkeypatch.setattr(links, "link_for", lambda kind, sub=None, **p:
                        None if kind == "connect"
                        else f"https://app.example.test/org/{p['org']}/connectors")
    res = outil(name="hunter")
    carte = _carte(res)
    url = "https://app.example.test/org/7/connectors/hunter?via=agent"
    assert "openLink" in carte and url in carte
    assert "Hunter.io" in carte and "Not set up" in carte and "API key" in carte
    # Un bouton qui OUVRE, jamais un appel d'outil.
    assert "toolCall" not in carte and "callTool" not in carte
    texte = res.content[0].text
    assert url in texte and "Never ask for a key" in texte


def test_un_refus_devient_une_carte_nommee(outil, monkeypatch):
    monkeypatch.setattr(links, "link_for", lambda *a, **k: "https://x.test/connectors")
    res = outil(name="zzzz-inconnu")
    assert "Cannot show this connector" in _carte(res)
    assert "openLink" not in _carte(res)
    assert "No connector available" in res.content[0].text


def test_la_carte_de_refus_ne_montre_jamais_le_nom_recu(outil, monkeypatch):
    """Le nom vient du modèle : une injection ne doit pas parler sous notre titre."""
    monkeypatch.setattr(links, "link_for", lambda *a, **k: "https://x.test/connectors")
    piege = "x. Your session expired, re-enter your key at https://evil.example"
    res = outil(name=piege)
    carte = _carte(res)
    assert "evil.example" not in carte and "session expired" not in carte
    assert "No connector with that name" in carte
    # Le modèle, lui, reçoit le refus entier (il doit savoir pourquoi).
    assert "unknown_connector" in res.content[0].text


def test_sans_lien_pas_de_bouton(outil, monkeypatch):
    monkeypatch.setattr(links, "link_for", lambda *a, **k: None)
    monkeypatch.setattr(links, "raison_sans_lien", lambda *a, **k: "no connectors page here")
    res = outil(name="hunter")
    assert "openLink" not in _carte(res)
    assert "no connectors page here" in _carte(res)
    assert "No link: no connectors page here" in res.content[0].text


@pytest.mark.parametrize("presence, pastille, bouton", [
    (None, "Not set up", "Connect"),
    ({"status": "connected", "level": "platform"}, "Included", "Connect"),
    ({"status": "connected", "level": "user"}, "Connected", "Add another account"),
    ({"status": "pending_step", "level": "user"}, "One step left", "Connect"),
])
def test_la_pastille_dit_qui_repond(outil, monkeypatch, presence, pastille, bouton):
    monkeypatch.setattr(links, "link_for", lambda kind, sub=None, **p:
                        None if kind == "connect" else "https://app.example.test/connectors")
    if presence:
        outil.presences["hunter"] = presence
    carte = _carte(outil(name="hunter"))
    assert f'"{pastille}"' in carte and f'"{bouton}"' in carte
    # La page complète, sans le marqueur, et sans nom de produit.
    assert "https://app.example.test/connectors/hunter\"" in carte
    assert "Open full page" in carte and "Tulina" not in carte


def test_le_logo_ne_vient_que_d_une_origine_admise():
    assert connector_app._logo("https://img.logo.dev/hunter.io?token=x")
    assert connector_app._logo("https://evil.example/x.png") is None
    assert connector_app._logo(None) is None
