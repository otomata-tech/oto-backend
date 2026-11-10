"""Un outil sans connecteur : le refus « nom inconnu » propose le Custom API (`http`).

Le fait : une cliente n'a trouvé ni comment brancher son outil de paie (absent du
catalogue, mais doté d'une API à clé), ni comment partager sa clé sans la coller dans
une conversation. Le refus de `oto_connector(name=…)` sait désormais dire l'issue — SI
l'org atteint `http` — avec le lien de la carte pré-remplie du nom cherché, jamais un
secret, construit sur la page connecteurs du produit de l'appelant (`links`).
"""
from __future__ import annotations

import pytest

from oto_mcp import links, providers
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.connectors import selection as CS

_CTX = ResolvedCtx(sub="u-repli", org_id=7)


def _ligne(name: str) -> dict:
    return next(c for c in providers.public_catalog() if c["name"] == name)


def _refus(catalog, demande="Acme Paie") -> AuthzDenied:
    return CS._refus_nom_inconnu(catalog, demande, _CTX)


def test_http_atteignable_le_refus_donne_le_lien_de_la_carte(monkeypatch):
    vus = []

    def _link_for(kind, sub=None, **p):
        vus.append((kind, sub, p))
        return "https://app.example.test/connectors"
    monkeypatch.setattr(links, "link_for", _link_for)
    e = _refus([_ligne("http"), _ligne("folk")])
    assert e.code == "unknown_connector"
    url = "https://app.example.test/connectors/http?label=Acme+Paie"
    assert e.details["custom_api"] == {"connector": "http", "setup_url": url}
    assert url in e.message and "never ask for the key in the chat" in e.message
    # Le lien suit le produit de l'APPELANT : son sub et son org.
    assert vus == [("connectors", "u-repli", {"org": 7})]


def test_http_hors_de_portee_aucune_issue_promise(monkeypatch):
    monkeypatch.setattr(links, "link_for", lambda *a, **k: "https://x.test/connectors")
    e = _refus([_ligne("folk")])
    assert "custom_api" not in e.details and "Custom API" not in e.message


def test_sans_page_connecteurs_l_issue_reste_nommee_sans_lien(monkeypatch):
    monkeypatch.setattr(links, "link_for", lambda *a, **k: None)
    e = _refus([_ligne("http")])
    assert e.details["custom_api"]["setup_url"] is None
    assert '`http(op="setup_link")`' in e.message


def test_le_connecteur_se_presente_en_custom_api():
    c = providers.REGISTRY["http"]
    assert c.label == "Custom API" and c.category == "Custom & industry apps"
    assert "never in a chat" in c.help
    assert _ligne("http")["name"] == "http"


@pytest.mark.parametrize("cherche", ["custom api", "Custom API"])
def test_le_libelle_trouve_le_connecteur(cherche):
    assert [c["name"] for c in CS._resoudre_nom([_ligne("http"), _ligne("folk")],
                                                 cherche)] == ["http"]


# ── un connecteur PERSONNALISÉ de l'org se trouve par son nom ─────────────────

@pytest.fixture()
def org_paie(monkeypatch):
    """L'org #7 a configuré une fois son outil de paie par le Custom API (compte `paie`,
    nom affiché « Acme Payroll » sur la carte, deux actions)."""
    from oto_mcp import credentials_store
    from oto_mcp.db import connector_instances as ci
    monkeypatch.setattr(credentials_store, "list_accounts", lambda et, eid, con:
                        [{"account": "paie", "meta": {"label": "Acme Payroll"}}]
                        if (et, eid, con) == ("org", "7", "http") else [])
    monkeypatch.setattr(ci, "live_instance_configs", lambda ot, oid, con, account=None: [
        {"id": 3, "account": "paie", "config": {
            "description": "Payroll SaaS.", "logo_domain": "acme.test",
            "operations": [{"name": "a"}, {"name": "b"}]}}])


def test_le_nom_d_un_connecteur_personnalise_le_rend_au_lieu_du_refus(monkeypatch, org_paie):
    monkeypatch.setattr(CS, "_visible_catalog", lambda ctx: [_ligne("http"), _ligne("folk")])
    monkeypatch.setattr(CS.connector_selection, "list_selection_detail", lambda s, o: {})
    monkeypatch.setattr(CS.connector_selection, "list_removed", lambda s, o: {})
    monkeypatch.setattr(CS.org_store, "get_org_default_connectors", lambda o: [])
    monkeypatch.setattr(CS, "_guide_refs_by_ns", lambda o: {})
    monkeypatch.setattr(CS.access, "reachable_instances_map", lambda s, o: {})
    out = CS._me_projection(_CTX, CS.MyConnectorsInput(name="  acme PAYROLL "))
    assert out["connectors"] == []
    c = out["custom_connector"]
    assert (c["connector"], c["account"], c["label"], c["actions_count"]) == \
        ("http", "paie", "Acme Payroll", 2)
    assert 'http(op="list", _account="paie")' in c["how_to_call"]
    assert 'http_act(op="call"' in c["how_to_call"]
    # Un autre nom reste refusé — avec l'issue Custom API.
    monkeypatch.setattr(links, "link_for", lambda *a, **k: None)
    with pytest.raises(AuthzDenied):
        CS._me_projection(_CTX, CS.MyConnectorsInput(name="Acme ERP"))


def test_le_contexte_de_l_agent_nomme_les_connecteurs_personnalises(org_paie):
    from oto_mcp import instructions
    ctx = {"org_name": "Org", "role": "", "group_name": "", "connectors": ["http"],
           "custom_connectors": [{"label": "Acme Payroll", "account": "paie"}],
           "projects": [], "runs": []}
    texte = instructions._format_context(ctx)
    assert '- Custom connector: Acme Payroll — `http(op="list", _account="paie")`' \
        in texte


def test_le_roster_des_instances_porte_la_carte_du_connecteur(monkeypatch):
    from oto_mcp.capabilities.connectors import custom_api
    from oto_mcp.db import connector_instances as ci
    monkeypatch.setattr(ci, "configs_for_instance_ids", lambda ids: {
        3: {"label": "Acme Payroll", "logo_domain": "acme.test",
            "operations": [{"name": "a"}]}, 4: {}})
    out = [{"name": "x"}, {"name": "Custom API · rh"}, {"name": "folk"}]
    cles = [("org", "7", "http", "paie"), ("org", "7", "http", "rh"),
            ("org", "7", "folk", "")]
    connues = {cles[0]: {"id": 3}, cles[1]: {"id": 4}, cles[2]: {"id": 5}}
    custom_api.stamp_instances(out, connues, cles)
    assert out[0]["custom_api"] == {"label": "Acme Payroll", "description": None,
                                    "logo_domain": "acme.test", "actions_count": 1}
    assert out[1]["custom_api"]["label"] == "Custom API · rh"
    assert "custom_api" not in out[2]
