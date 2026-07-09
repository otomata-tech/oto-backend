"""`data_app` (MCP App) — conscience du schéma datastore v2 (ADR 0046).

Le tool `data_app` ne s'enregistre que si l'extra `prefab_ui` est présent. On le
STUBBE ici par des composants enregistreurs (arbre de rendu inspectable), puis on
exerce la vraie closure `data_app` : on prouve que la vue DÉTAIL d'une fiche déplie
les sous-records (`contacts[]`, `occupant{}`) en sous-tables/clé-valeur au lieu du
`"n × {...}"` plat, montre le statut + lifecycle, et que la table de liste suit
l'ORDRE des fields déclarés. Pas de DB : store & identité stubbés.
"""
import sys
import types

import pytest


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
        return [n.text for n in self.walk() if n.kind in ("Heading", "Text") and n.text]

    def tables(self):
        return [n for n in self.walk() if n.kind == "DataTable"]


def _install_prefab_stub():
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

    class DataTableColumn:  # simple descripteur, pas un noeud d'arbre
        def __init__(self, key=None, header=None, **k):
            self.key, self.header = key, header

    def DataTable(columns=None, rows=None, **k):
        n = _Node("DataTable", columns=columns or [], rows=rows or [], **k)
        return n

    comp.Card, comp.Column, comp.Heading = Card, Column, Heading
    comp.Text, comp.DataTable, comp.DataTableColumn = Text, DataTable, DataTableColumn
    mod.components = comp
    sys.modules["prefab_ui"] = mod
    sys.modules["prefab_ui.components"] = comp


LEAD_SCHEMA = {
    "strict": True,
    "key": "fact_id",
    "fields": [
        {"key": "fact_id", "type": "text", "required": True, "role": "title"},
        {"key": "mwh", "type": "number"},
        {"key": "occupant", "type": "object",
         "fields": [{"key": "nom", "type": "text"}, {"key": "naf", "type": "text"}]},
        {"key": "contacts", "type": "list",
         "of": {"fields": [{"key": "nom", "type": "text"},
                           {"key": "email", "type": "text"}]}},
        {"key": "status", "role": "status",
         "lifecycle": {"states": ["nouveau", "en_cours", "qualified", "ecarte"],
                       "transitions": {"nouveau": ["en_cours"],
                                       "en_cours": ["qualified", "ecarte"]}}},
    ],
}

FICHE = {
    "_id": "r1",
    "fact_id": "F-42",
    "mwh": 3.5,
    "occupant": {"nom": "Acme SAS", "naf": "6201Z"},
    "contacts": [{"nom": "Alice", "email": "a@acme.fr"},
                 {"nom": "Bob", "email": "b@acme.fr"}],
    "status": "en_cours",
}


class _FakeStore:
    def __init__(self, rows, schema):
        self._rows, self._schema = rows, schema

    def list_namespaces(self):
        return [{"namespace": "leads", "schema": self._schema, "shared": False,
                 "url": "https://dash/leads"}]

    def list_rows(self, namespace, filter=None, limit=100):
        rows = self._rows
        if filter:
            rows = [r for r in rows
                    if all(str(r.get(k)) == str(v) for k, v in filter.items())]
        return rows[:limit]

    def get_url(self, namespace):
        return "https://dash/leads"

    def get_schema(self, namespace):
        return self._schema


@pytest.fixture
def data_app(monkeypatch):
    _install_prefab_stub()
    _STACK.clear()
    # (ré)importe le module APRÈS le stub pour que l'import gardé réussisse
    sys.modules.pop("oto_mcp.tools.datastore", None)
    import oto_mcp.tools.datastore as ds

    captured = {}

    class _FakeMcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    monkeypatch.setattr(ds.access, "current_user_sub_or_raise", lambda: "sub1")
    store = _FakeStore([FICHE], LEAD_SCHEMA)
    monkeypatch.setattr(ds, "make_store", lambda sub: store)
    ds.register(_FakeMcp())
    assert "data_app" in captured, "data_app doit s'enregistrer avec le stub prefab_ui"
    return captured["data_app"]


def test_single_fiche_expands_nested_records(data_app):
    card = data_app(namespace="leads", row="F-42")
    texts = " | ".join(card.texts())
    # titre = field role=title ; jamais le blob compact
    assert "F-42" in texts
    assert "3 ×" not in texts and "×" not in texts
    # statut + lifecycle (suites possibles)
    assert "Statut : en_cours" in texts
    assert "qualified" in texts and "ecarte" in texts
    # occupant{} déplié en clé/valeur
    assert "Acme SAS" in texts
    # contacts[] rendu en SOUS-TABLE (2 lignes), pas une cellule compacte
    contact_tables = [t for t in card.tables()
                      if {c.key for c in t.attrs["columns"]} >= {"nom", "email"}]
    assert contact_tables, "contacts[] doit devenir une sous-DataTable"
    assert len(contact_tables[0].attrs["rows"]) == 2


def test_filter_narrowing_to_one_row_auto_opens_detail(data_app):
    card = data_app(namespace="leads", filter={"fact_id": "F-42"})
    # vue détail auto ⇒ la sous-table contacts est présente
    assert any({c.key for c in t.attrs["columns"]} >= {"nom", "email"}
               for t in card.tables())


def test_list_view_columns_follow_schema_order(data_app):
    # 2 lignes ⇒ pas de détail auto ⇒ table de liste
    two = _FakeStore([FICHE, {**FICHE, "_id": "r2", "fact_id": "F-43"}], LEAD_SCHEMA)
    import oto_mcp.tools.datastore as ds
    ds.make_store = lambda sub: two  # store à 2 lignes
    card = data_app(namespace="leads")
    tables = card.tables()
    assert tables, "vue liste attendue"
    keys = [c.key for c in tables[0].attrs["columns"]]
    # ordre déclaré du schéma respecté (fact_id avant mwh avant contacts)
    assert keys.index("fact_id") < keys.index("mwh") < keys.index("contacts")


def test_unknown_row_returns_message(data_app):
    card = data_app(namespace="leads", row="does-not-exist")
    assert any("introuvable" in t.lower() for t in card.texts())
