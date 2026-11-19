"""`oto_doc_app` (MCP App) — lecture/parcours rendu des pages d'un projet.

Même patron que test_datastore_app_v2 : prefab_ui STUBBÉ par des composants
enregistreurs, db/ownership/access stubbés (pas de DB), et on exerce la vraie
closure. On prouve : l'arbre indente les enfants sous leur parent (DFS), la vue
page rend le markdown, le défaut sans args résout la KB de l'org active, et un
projet non lisible rend une carte message (jamais une fuite).

Et le canal TEXTE (signal #1083) : l'hôte donne au modèle le seul `content`, la carte
va à la vue. Chaque vue doit donc porter son contenu en texte — prouvé ici sur le stub,
puis BOUT EN BOUT sur le vrai prefab_ui à travers la chaîne de middlewares servie
(`test_bout_en_bout_…`), qui retirait le canal structuré de la carte.
"""
import asyncio
import json
import sys
import types

import pytest
from _modules_recharges import recharger_module
# Importé ICI, avant tout stub : FastMCP fige `_HAS_PREFAB` à son premier import. Chargé
# pour la première fois sous le stub de la fixture, il croirait prefab_ui absent — les
# apps perdraient leur ressource d'UI et le test bout en bout jugerait un autre serveur.
from fastmcp import Client, FastMCP


# ── stub prefab_ui.components : composants enregistreurs (pile de contexte) ─────
_STACK: list = []


class _Node:
    def __init__(self, kind, text=None, **attrs):
        self.kind = kind
        self.text = text
        self.attrs = attrs
        self.children: list = []
        if _STACK:
            _STACK[-1].children.append(self)

    def __enter__(self):
        _STACK.append(self)
        return self

    def __exit__(self, *exc):
        _STACK.pop()
        return False

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def texts(self):
        return [n.text for n in self.walk()
                if n.kind in ("Heading", "Text", "Markdown") and n.text]

    def tables(self):
        return [n for n in self.walk() if n.kind == "DataTable"]


def _install_prefab_stub(monkeypatch):
    mod = types.ModuleType("prefab_ui")
    comp = types.ModuleType("prefab_ui.components")

    def Card(**k):
        return _Node("Card", **k)

    def Column(**k):
        return _Node("Column", **k)

    def Heading(text=None, **k):
        return _Node("Heading", text=text, **k)

    def Text(text=None, **k):
        return _Node("Text", text=text, **k)

    def Markdown(text=None, **k):
        return _Node("Markdown", text=text, **k)

    class DataTableColumn:
        def __init__(self, key=None, header=None, **k):
            self.key, self.header = key, header

    def DataTable(columns=None, rows=None, **k):
        return _Node("DataTable", columns=columns or [], rows=rows or [], **k)

    comp.Card, comp.Column, comp.Heading = Card, Column, Heading
    comp.Text, comp.Markdown = Text, Markdown
    comp.DataTable, comp.DataTableColumn = DataTable, DataTableColumn
    mod.components = comp
    # Posé par monkeypatch, donc RETIRÉ après chaque test : un stub laissé dans
    # sys.modules faisait échouer en silence l'import gardé de toute app testée
    # plus loin dans la session (sa `register()` sortait sans rien enregistrer).
    monkeypatch.setitem(sys.modules, "prefab_ui", mod)
    monkeypatch.setitem(sys.modules, "prefab_ui.components", comp)


DOCS = [
    {"id": 1, "project_id": 7, "parent_id": None, "title": "Racine", "kind": "doc",
     "updated_at": "2026-07-01 10:00:00", "body_md": "# Racine", "public": False},
    {"id": 2, "project_id": 7, "parent_id": 1, "title": "Enfant", "kind": "note",
     "updated_at": "2026-07-02 10:00:00", "body_md": "corps **gras**", "public": False},
    {"id": 3, "project_id": 7, "parent_id": None, "title": "Autre racine", "kind": "doc",
     "updated_at": "2026-07-03 10:00:00", "body_md": "", "public": False},
]


SENTINELLE = "SENTINELLE-1083 : le modèle lit ce corps"
DOCS[1]["body_md"] = f"corps **gras** — {SENTINELLE}"


class _Rendu:
    """Ce que `_rendu` remet à FastMCP : le texte au modèle, la carte (stub) à l'hôte.
    Le vrai `ToolResult` ne sait pas sérialiser un composant stub — le vrai chemin est
    joué par le test bout en bout."""

    def __init__(self, content, structured_content):
        self.texte = "".join(b.text for b in content)
        self.carte = structured_content


def _stub_backend(monkeypatch, da):
    monkeypatch.setattr(da.access, "current_user_sub_or_raise", lambda: "sub1")
    monkeypatch.setattr(da.access, "current_org", lambda sub: 42)
    monkeypatch.setattr(da.ownership, "can_access",
                        lambda sub, rt, rid, want: rid == "7")
    monkeypatch.setattr(da.db, "get_project_by_id",
                        lambda pid: {"id": pid, "name": "KB test"} if pid == 7 else None)
    # La KB de l'org active est RENOMMÉE : son nom n'est pas — n'a jamais été —
    # la clé qui la résout ; l'ancre `orgs.kb_project_id` l'est (lot 3, chantier 0.3).
    monkeypatch.setattr(da.db, "list_projects_for_owners",
                        lambda owners: [{"id": 7, "name": "Wiki interne"}])
    monkeypatch.setattr(da, "org_store",
                        types.SimpleNamespace(get_kb_project_id=lambda org: 7),
                        raising=False)
    monkeypatch.setattr(da.db, "list_docs_for_project",
                        lambda pid: list(DOCS) if pid == 7 else [])
    monkeypatch.setattr(da.db, "get_doc_by_id",
                        lambda did: next((d for d in DOCS if d["id"] == did), None))
    monkeypatch.setattr(da.db, "search_docs_in_project",
                        lambda pid, q, **k: [{"id": 2, "title": "Enfant", "kind": "note",
                                              "snippet": "corps <b>gras</b>"}])


@pytest.fixture
def doc_app(monkeypatch):
    _install_prefab_stub(monkeypatch)
    _STACK.clear()
    da = recharger_module(monkeypatch, "oto_mcp.tools.docs_app")

    captured = {}

    class _FakeMcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    _stub_backend(monkeypatch, da)
    monkeypatch.setattr(da, "ToolResult", _Rendu)
    da.register(_FakeMcp())
    assert "oto_doc_app" in captured, "oto_doc_app doit s'enregistrer avec le stub prefab_ui"
    return captured["oto_doc_app"]


def test_tree_indents_children_under_parent(doc_app):
    card = doc_app(project_id=7).carte
    tables = card.tables()
    assert len(tables) == 1
    pages = [r["page"] for r in tables[0].attrs["rows"]]
    # DFS : l'enfant suit sa racine, indenté ; l'autre racine vient après.
    assert pages[0] == "Racine"
    assert pages[1].endswith("└ Enfant") and pages[1] != "└ Enfant"
    assert pages[2] == "Autre racine"


def test_page_view_renders_markdown(doc_app):
    card = doc_app(doc_id=2).carte
    kinds = [n.kind for n in card.walk()]
    assert "Markdown" in kinds
    md = next(n for n in card.walk() if n.kind == "Markdown")
    assert md.text == DOCS[1]["body_md"]
    assert "Enfant" in card.texts()[0]


def test_default_resolves_active_org_kb(doc_app):
    """Sans argument, l'app ouvre la KB de l'org active — même RENOMMÉE (#527).

    `_kb_project_id` cherchait le projet dont le nom vaut `KB_NAME`, alors que
    l'identification par nom est morte au lot 3 (chantier 0.3) : l'ancre
    `orgs.kb_project_id` est la source de vérité. Conséquence mesurée : une org
    qui renomme sa base — ce que fait justement le client anglophone qui la
    rebaptise « Knowledge base » — perd `oto_doc_app` sans argument, qui répond
    « Aucun projet ciblé » alors que sa KB est là, ancrée, lisible."""
    card = doc_app().carte
    assert card.texts()[0] == "KB test"      # la KB (projet 7) a été résolue
    assert len(card.tables()) == 1


def test_unreadable_project_yields_message_card(doc_app):
    card = doc_app(project_id=99).carte
    assert "Project not found" in card.texts()
    assert card.tables() == []


def test_search_strips_headline_markup(doc_app):
    card = doc_app(project_id=7, query="gras").carte
    rows = card.tables()[0].attrs["rows"]
    assert rows[0]["extrait"] == "corps gras"


# ── Le canal TEXTE : ce que le modèle lit (signal #1083) ─────────────────────────

def test_la_page_arrive_au_modele_en_texte(doc_app):
    """Le cas du 18/09 : le modèle ne recevait que « [Rendered Prefab UI] » et a résumé
    une page qu'il n'avait pas lue. Le texte porte le titre ET le corps entier."""
    texte = doc_app(doc_id=2).texte
    assert texte.startswith("# Enfant")
    assert SENTINELLE in texte


def test_l_arbre_et_les_extraits_arrivent_au_modele_en_texte(doc_app):
    arbre = doc_app(project_id=7).texte
    for titre in ("KB test", "Racine", "Enfant", "Autre racine", "#3"):
        assert titre in arbre
    extraits = doc_app(project_id=7, query="gras").texte
    assert "Enfant" in extraits and "corps gras" in extraits


def test_une_carte_message_dit_son_message_au_modele(doc_app):
    """Un refus se dit aussi en texte : un modèle qui ne lit que le marqueur croirait
    la page servie."""
    assert doc_app(project_id=99).texte == "Project not found — No accessible project #99."
    assert "Page not found" in doc_app(doc_id=404).texte


# ── Bout en bout : vrai prefab_ui, vraie chaîne de middlewares servie ────────────

def test_bout_en_bout_le_modele_lit_la_page_et_la_carte_a_son_contenu(monkeypatch):
    """Les deux pannes du 18/09, à travers ce que sert le vrai serveur :

    - le TEXTE (ce que l'hôte donne au modèle) porte la page, pas le marqueur ;
    - `structuredContent` (l'unique entrée du renderer Prefab, qui reste sur « Waiting
      for content… » sans lui) survit à la chaîne : `UnSeulCanal` le retirait, l'outil
      n'ayant pas de schéma de sortie ;
    - la ressource d'UI annoncée par l'outil se lit, au type MIME des MCP Apps."""
    pytest.importorskip("prefab_ui")
    from _mcp_app import static_mcp

    from oto_mcp.middleware import un_seul_canal

    da = recharger_module(monkeypatch, "oto_mcp.tools.docs_app")

    m = FastMCP("banc")
    for mw in static_mcp().middleware:
        m.add_middleware(mw)
    da.register(m)
    un_seul_canal.retirer_les_schemas_vides(m)
    _stub_backend(monkeypatch, da)

    async def appel():
        async with Client(m) as c:
            outil = next(t for t in await c.list_tools() if t.name == "oto_doc_app")
            r = await c.call_tool("oto_doc_app", {"doc_id": 2}, raise_on_error=False)
            ui = (await c.read_resource(outil.meta["ui"]["resourceUri"]))[0]
            return r, ui

    r, ui = asyncio.run(appel())
    assert not r.is_error
    texte = "".join(getattr(b, "text", "") for b in r.content)
    assert SENTINELLE in texte and "[Rendered Prefab UI]" not in texte
    assert r.structured_content is not None, "la carte n'a rien à peindre"
    assert "$prefab" in r.structured_content
    assert SENTINELLE in json.dumps(r.structured_content, ensure_ascii=False)
    assert ui.mimeType == "text/html;profile=mcp-app" and ui.text
