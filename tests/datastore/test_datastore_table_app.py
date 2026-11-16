"""`data_app` peint par le bundle d'un front (`OTO_DATA_APP_BUNDLE`).

Ce qu'on fige :
1. **sans bundle déclaré, rien ne change** — ce module n'enregistre rien et la carte
   Prefab de `tools/datastore.py` reste ; **avec**, un seul `data_app`, le sien ;
2. **la vue reçoit une page de lignes, le modèle une ligne** — et un client qui ne
   peint pas les apps reçoit les lignes en texte ;
3. **l'agent choisit les colonnes** (`fields`, dans son ordre) et la taille de page ;
4. **les colonnes gardées des agents ne sont pas servies**, la vue dit combien.
"""
from __future__ import annotations

import asyncio
import json
import sys

import pytest
from _modules_recharges import recharger_module
from fastmcp import Client, FastMCP

BUNDLE = "https://app.example.com/mcp-app/process-graph.js"

SCHEMA = {"key": "domaine", "fields": [
    {"key": "nom", "type": "text", "display": "title"},
    {"key": "domaine", "type": "text"},
    {"key": "statut", "type": "enum", "options": ["nouveau", "qualifié"]},
    {"key": "score", "type": "number"},
]}
ROWS = [
    {"_id": "r1", "_created_at": "2026-10-01", "_updated_at": "2026-10-02",
     "nom": "Alpha", "domaine": "alpha.example", "statut": "nouveau", "score": 3},
    {"_id": "r2", "_created_at": "2026-10-01", "_updated_at": "2026-10-02",
     "nom": "Beta", "domaine": "beta.example", "statut": "qualifié", "score": 9},
]


class _Store:
    def __init__(self):
        self.dernier_tableau = None
        self.appels = []

    def list_datastores(self):
        return [{"ns_id": 7, "datastore": "leads", "schema": SCHEMA, "shared": False,
                 "url": "https://app.example.com/org/1/tables/7"}]

    def schema_servi_et_masquees(self, ref):
        if ref == "inconnu":
            from oto_mcp.datastore.core import DatastoreNotFound
            raise DatastoreNotFound(ref)
        return SCHEMA, 2

    def cursor_rows(self, ref, **kw):
        self.appels.append(kw)
        self.dernier_tableau = {"ns_id": 7, "datastore": "leads"}
        champs = kw.get("fields")
        rows = [{k: v for k, v in r.items() if not champs or k in champs} for r in ROWS]
        return {"rows": rows[: kw["limit"]], "next_cursor": "c2" if kw["limit"] < 2 else None}

    def count_rows(self, ref, **kw):
        return len(ROWS)

    def get_url(self, ref):
        return "https://app.example.com/org/1/tables/7"


def _module(monkeypatch):
    return recharger_module(monkeypatch, "oto_mcp.tools.datastore_table_app")


def _banc(monkeypatch, *, ui: bool = True):
    pytest.importorskip("fastmcp.apps")
    from _mcp_app import static_mcp

    # The shared catalogue is cached for the whole process: build it BEFORE the bundle
    # is set, or a later test (the frozen surface) reads the bundled `data_app`.
    middleware = static_mcp().middleware
    from oto_mcp import access, procedure_diagram
    from oto_mcp.middleware import un_seul_canal
    from oto_mcp.tools import datastore as ds
    store = _Store()
    monkeypatch.setattr(ds, "_store_for", lambda sub: store)
    monkeypatch.setattr(ds, "_ns", lambda ref: ref)
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "sub1")
    # The test client advertises no MCP Apps extension: say what the host would.
    monkeypatch.setattr(procedure_diagram, "client_affiche_les_apps", lambda: ui)
    monkeypatch.setenv("OTO_DATA_APP_BUNDLE", BUNDLE)
    ta = _module(monkeypatch)
    m = FastMCP("banc")
    for mw in middleware:
        m.add_middleware(mw)
    ta.register(m)
    un_seul_canal.retirer_les_schemas_vides(m)
    return m, store


def _appel(m, args):
    async def go():
        async with Client(m) as c:
            return await c.call_tool("data_app", args, raise_on_error=False)
    return asyncio.run(go())


def _texte(r) -> str:
    return "".join(getattr(b, "text", "") for b in r.content)


def test_sans_bundle_rien_ne_s_enregistre_ici(monkeypatch):
    monkeypatch.delenv("OTO_DATA_APP_BUNDLE", raising=False)
    ta = _module(monkeypatch)
    m = FastMCP("banc")
    ta.register(m)

    async def lister():
        async with Client(m) as c:
            return await c.list_tools(), await c.list_resources()

    assert asyncio.run(lister()) == ([], [])
    assert ta.bundle() is None


def test_un_bundle_qui_n_est_pas_un_script_garde_la_carte_prefab(monkeypatch):
    monkeypatch.setenv("OTO_DATA_APP_BUNDLE", "https://app.example.com/mcp-app/")
    assert _module(monkeypatch).bundle() is None


def test_la_page_charge_le_bundle_et_admet_son_origine(monkeypatch):
    m, _ = _banc(monkeypatch)

    async def lire():
        async with Client(m) as c:
            outil = next(t for t in await c.list_tools() if t.name == "data_app")
            uri = outil.meta["ui"]["resourceUri"]
            ui = (await c.read_resource(uri))[0]
            page = next(p for p in await c.list_resources() if str(p.uri) == uri)
            return ui, page

    ui, page = asyncio.run(lire())
    assert f'src="{BUNDLE}"' in ui.text
    assert 'href="https://app.example.com/mcp-app/process-graph.css"' in ui.text
    assert "https://app.example.com" in json.dumps(page.meta)
    assert page.meta["ui"]["prefersBorder"] is True


def test_la_vue_recoit_la_page_et_le_modele_une_ligne(monkeypatch):
    m, store = _banc(monkeypatch)
    r = _appel(m, {"datastore": 7})
    assert not r.is_error
    vue = r.structured_content
    assert vue["view"] == "table" and vue["ns_id"] == 7 and vue["name"] == "leads"
    assert vue["total"] == 2 and vue["count"] == 2 and vue["hidden_columns"] == 2
    # Declared order, title flagged; meta columns out of the columns, `_id` kept on rows.
    assert [c["key"] for c in vue["columns"]] == ["nom", "domaine", "statut", "score"]
    assert vue["columns"][0]["is_title"] is True
    assert vue["columns"][2]["options"] == ["nouveau", "qualifié"]
    assert "_created_at" not in vue["rows"][0] and vue["rows"][0]["_id"] == "r1"
    assert vue["table_url"].endswith("/tables/7") and vue["origin"] == "https://app.example.com"
    # The model: one line, no row values.
    texte = _texte(r)
    assert "2 of 2 row(s) shown to the user" in texte and "Alpha" not in texte
    # Measured in MCPJam: without it, Haiku called twice for one question.
    assert "Do not call this again for the same question" in texte
    assert store.appels[0]["limit"] == 25


def test_l_agent_choisit_les_colonnes_et_la_page(monkeypatch):
    m, store = _banc(monkeypatch)
    r = _appel(m, {"datastore": 7, "fields": ["statut", "nom"], "limit": 1,
                   "order_by": "score", "order_dir": "asc"})
    vue = r.structured_content
    assert [c["key"] for c in vue["columns"]] == ["statut", "nom"]
    assert store.appels[0]["fields"] == ["statut", "nom"]
    assert store.appels[0]["order_by"] == "score" and store.appels[0]["limit"] == 1
    assert vue["next_cursor"] == "c2" and "cursor=c2" in _texte(r)


def test_un_client_texte_recoit_les_lignes(monkeypatch):
    m, _ = _banc(monkeypatch, ui=False)
    r = _appel(m, {"datastore": 7})
    texte = _texte(r)
    assert "Alpha" in texte and "beta.example" in texte


def test_sans_tableau_la_liste_et_tableau_inconnu_une_note(monkeypatch):
    m, _ = _banc(monkeypatch)
    liste = _appel(m, {}).structured_content
    assert liste["view"] == "tables" and liste["tables"][0]["ns_id"] == 7
    # The model gets the numbers too, or it guesses one to show a table.
    assert "7 leads" in _texte(_appel(m, {}))
    note = _appel(m, {"datastore": "inconnu"}).structured_content
    assert note["view"] == "note" and "inconnu" in note["message"]


def test_avec_bundle_un_seul_data_app_et_c_est_le_sien(monkeypatch):
    """La carte Prefab se retire : deux enregistrements du même nom, le second
    écraserait le premier selon l'ordre de chargement — jamais un choix."""
    pytest.importorskip("fastmcp.apps")
    pytest.importorskip("prefab_ui")
    from oto_mcp.tools import datastore as ds

    def data_app_de(env: str | None):
        if env:
            monkeypatch.setenv("OTO_DATA_APP_BUNDLE", env)
        else:
            monkeypatch.delenv("OTO_DATA_APP_BUNDLE", raising=False)
        ta = _module(monkeypatch)
        m = FastMCP("banc")
        ds.register(m)
        ta.register(m)

        async def lister():
            async with Client(m) as c:
                return [t for t in await c.list_tools() if t.name == "data_app"]
        return asyncio.run(lister())

    avec = data_app_de(BUNDLE)
    assert len(avec) == 1 and avec[0].meta["ui"]["resourceUri"] == "ui://oto/data-table.html"
    sans = data_app_de(None)
    assert len(sans) == 1 and sans[0].meta["ui"]["resourceUri"] != "ui://oto/data-table.html"


def test_row_montre_une_fiche_comme_la_carte_prefab(monkeypatch):
    """`row` ouvre UNE fiche, cherchée comme la carte Prefab (`_id`, clé métier, titre) :
    une procédure qui l'appelle ne casse pas sur l'instance qui déclare un bundle."""
    m, store = _banc(monkeypatch)
    vue = _appel(m, {"datastore": 7, "row": "r2"}).structured_content
    assert vue["view"] == "table" and vue["row"] == "r2"
    assert [r["nom"] for r in vue["rows"]] == ["Beta"] and vue["total"] == 1
    assert vue["next_cursor"] is None
    # Cherchée sur les lignes ENTIÈRES (le titre n'est pas parmi `fields`), puis coupée.
    vue = _appel(m, {"datastore": 7, "row": "Alpha", "fields": ["statut"]}).structured_content
    assert vue["rows"] == [{"_id": "r1", "statut": "nouveau"}]
    assert store.appels[-1].get("fields") is None and store.appels[-1]["limit"] == 200
    assert _appel(m, {"datastore": 7, "row": "beta.example"}).structured_content["rows"][0]["_id"] == "r2"


def test_row_introuvable_une_note_qui_le_nomme(monkeypatch):
    m, _ = _banc(monkeypatch)
    r = _appel(m, {"datastore": 7, "row": "zeta"})
    assert r.structured_content["view"] == "note"
    assert "zeta" in _texte(r) and "leads" in _texte(r)


def test_row_pour_un_client_texte(monkeypatch):
    m, _ = _banc(monkeypatch, ui=False)
    texte = _texte(_appel(m, {"datastore": 7, "row": "r1"}))
    assert "Alpha" in texte and "Beta" not in texte


def test_bundle_pose_sans_l_extra_apps_leve_au_demarrage(monkeypatch):
    """La carte Prefab s'est déjà retirée pour ce bundle : se taire laisserait l'instance
    sans aucun `data_app`."""
    monkeypatch.setenv("OTO_DATA_APP_BUNDLE", BUNDLE)
    ta = _module(monkeypatch)
    monkeypatch.setitem(sys.modules, "fastmcp.apps", None)
    with pytest.raises(RuntimeError, match="OTO_DATA_APP_BUNDLE"):
        ta.register(FastMCP("banc"))


def test_une_seule_regle_pour_les_bundles_des_deux_apps(monkeypatch, caplog):
    from oto_mcp import procedure_diagram
    pas_un_script = "https://app.example.com/mcp-app/"
    monkeypatch.setenv("OTO_DATA_APP_BUNDLE", pas_un_script)
    monkeypatch.setenv("OTO_PROCEDURE_APP_BUNDLE", pas_un_script)
    with caplog.at_level("ERROR"):
        assert _module(monkeypatch).bundle() is None
        assert procedure_diagram.bundle_du_graphe() is None
    nommees = [r.getMessage() for r in caplog.records]
    assert any("OTO_DATA_APP_BUNDLE" in t and "Prefab" in t for t in nommees)
    assert any("OTO_PROCEDURE_APP_BUNDLE" in t for t in nommees)
    monkeypatch.setenv("OTO_DATA_APP_BUNDLE", BUNDLE)
    assert _module(monkeypatch).bundle() == BUNDLE
