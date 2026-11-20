"""`GET /api/datastores/{datastore}/rows/export.csv` — le tableau entier, en CSV.

Fait tourner le VRAI handler (`api/datastore_export.py`) derrière un store
factice (même patron que `tests/datastore/test_datastore_rows_capability.py`),
via `TestClient` : la question posée ici est le COMPORTEMENT (auth, 404,
pagination interne, dialecte des cellules), pas le CORS sur un vrai socket —
déjà couvert en transitivité par `test_reponses_binaires_cors.py`'s
`test_aucune_reponse_fichier_ne_se_construit_hors_de_base_file`, qui refuse
toute réponse fichier construite hors de `base._file`/`base._file_stream`.
"""
from __future__ import annotations

import csv
import io

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from oto_mcp.api import base, datastore_export
from oto_mcp.datastore.core import DatastoreNotFound

SCHEMA = {
    "fields": [
        {"key": "raison_sociale", "label": "Raison sociale"},
        {"key": "siren"},  # pas de label déclaré → retombe sur la clé
    ]
}


class _Store:
    """Enregistre chaque appel `cursor_rows`, rend les pages posées par le test
    dans l'ordre — un curseur factice suffit, rien ne le décode réellement ici."""

    def __init__(self, schema, pages, *, dernier=None):
        self.schema = schema
        self.pages = list(pages)
        self.calls: list[dict] = []
        self.dernier_tableau = dernier or {"ns_id": 174, "datastore": "vivier"}

    def get_schema(self, ref):
        if isinstance(self.schema, Exception):
            raise self.schema
        return self.schema

    def cursor_rows(self, ref, **kw):
        self.calls.append(kw)
        return self.pages.pop(0)


def _app(monkeypatch, store, *, sub="u-1"):
    async def _auth(_req, _verifier, **kw):
        return sub, None

    monkeypatch.setattr(datastore_export, "make_store", lambda _sub: store)
    monkeypatch.setattr(datastore_export, "_authenticate", _auth)
    return Starlette(routes=[
        Route("/api/datastores/{datastore}/rows/export.csv",
              base.bind(datastore_export.export_csv, verifier=None), methods=["GET"]),
    ])


def _rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def test_en_tete_et_lignes_dans_lordre_du_schema(monkeypatch):
    store = _Store(SCHEMA, [
        {"rows": [{"_id": "r1", "raison_sociale": "ACME", "siren": "123456789",
                   "_created_at": "2026-09-01 10:00:00", "_updated_at": "2026-09-01 10:00:00"}],
         "next_cursor": None},
    ])
    client = TestClient(_app(monkeypatch, store))

    rep = client.get("/api/datastores/174/rows/export.csv")

    assert rep.status_code == 200
    assert rep.headers["content-type"].startswith("text/csv")
    lignes = _rows(rep.text)
    assert lignes[0] == ["Raison sociale", "siren", "_id", "_created_at", "_updated_at"]
    assert lignes[1] == ["ACME", "123456789", "r1", "2026-09-01 10:00:00", "2026-09-01 10:00:00"]


def test_pagine_en_interne_jusquau_dernier_lot(monkeypatch):
    """Deux pages posées, un seul GET client : la boucle interne (pas le client)
    doit repasser le `next_cursor` et s'arrêter au premier `None`."""
    store = _Store(SCHEMA, [
        {"rows": [{"_id": "r1", "raison_sociale": "ACME", "siren": "1"}], "next_cursor": "c2"},
        {"rows": [{"_id": "r2", "raison_sociale": "BETA", "siren": "2"}], "next_cursor": None},
    ])
    client = TestClient(_app(monkeypatch, store))

    rep = client.get("/api/datastores/174/rows/export.csv")

    lignes = _rows(rep.text)
    assert [l[0] for l in lignes[1:]] == ["ACME", "BETA"]
    assert len(store.calls) == 2
    assert store.calls[1]["cursor"] == "c2"
    # Le second appel repasse le MÊME jeu de paramètres (tri/recherche/filtre) —
    # une pagination qui les perd en route changerait de jeu de lignes à mi-fichier.
    assert store.calls[0]["order_by"] == store.calls[1]["order_by"]


def test_colonne_non_declaree_decouverte_sur_le_premier_lot(monkeypatch):
    """Tableau LIBRE (pas de schéma) : les colonnes sortent des lignes elles-mêmes,
    même limite assumée que `columnsFor()` côté front — vues sur le premier lot,
    ou pas du tout."""
    store = _Store(None, [
        {"rows": [{"_id": "r1", "ville": "Paris"}], "next_cursor": None},
    ])
    client = TestClient(_app(monkeypatch, store))

    rep = client.get("/api/datastores/174/rows/export.csv")

    lignes = _rows(rep.text)
    assert lignes[0] == ["ville", "_id", "_created_at", "_updated_at"]
    assert lignes[1] == ["Paris", "r1", "", ""]


def test_valeurs_null_liste_et_objet(monkeypatch):
    """Le dialecte CSV convenu (docs/datastore.md « un CSV ne sait pas dire null ») :
    `None` → cellule vide, une liste jointe sur `"; "`, un objet en JSON."""
    store = _Store({"fields": [{"key": "tags"}, {"key": "notes"}, {"key": "meta"}]}, [
        {"rows": [{"_id": "r1", "tags": ["a", "b"], "notes": None, "meta": {"k": 1}}],
         "next_cursor": None},
    ])
    client = TestClient(_app(monkeypatch, store))

    rep = client.get("/api/datastores/174/rows/export.csv")

    lignes = _rows(rep.text)
    assert lignes[1][:3] == ["a; b", "", '{"k": 1}']


def test_404_sur_tableau_introuvable(monkeypatch):
    store = _Store(DatastoreNotFound("999999999"), [])
    client = TestClient(_app(monkeypatch, store))

    rep = client.get("/api/datastores/999999999/rows/export.csv")

    assert rep.status_code == 404
    assert rep.json()["error"] == "datastore_not_found"


def test_refuse_sans_authentification(monkeypatch):
    store = _Store(SCHEMA, [])

    async def _refuse(_req, _verifier, **kw):
        from starlette.responses import JSONResponse
        return None, JSONResponse({"error": "stale_session"}, status_code=401)

    monkeypatch.setattr(datastore_export, "make_store", lambda _sub: store)
    monkeypatch.setattr(datastore_export, "_authenticate", _refuse)
    app = Starlette(routes=[
        Route("/api/datastores/{datastore}/rows/export.csv",
              base.bind(datastore_export.export_csv, verifier=None), methods=["GET"]),
    ])
    client = TestClient(app)

    rep = client.get("/api/datastores/174/rows/export.csv")

    assert rep.status_code == 401
    assert store.calls == []  # jamais atteint le store sans authentification


def test_nom_de_fichier_reprend_le_nom_canonique_du_tableau(monkeypatch):
    """`store.dernier_tableau` porte le nom CANONIQUE (posé par `_resolve`), pas
    la référence brute que le client a tapée — même contrat que le reste du
    datastore (cf. `docs/datastore-semantics.md` §0)."""
    store = _Store(SCHEMA, [{"rows": [], "next_cursor": None}],
                   dernier={"ns_id": 174, "datastore": "prospection-q3"})
    client = TestClient(_app(monkeypatch, store))

    rep = client.get("/api/datastores/174/rows/export.csv")

    assert "prospection-q3" in rep.headers["content-disposition"]


def test_aucune_cellule_texte_ne_s_ouvre_en_formule(monkeypatch):
    """Les lignes viennent d'imports et d'agents : un texte qu'un tableur lirait
    comme une formule est préfixé d'une apostrophe, en-tête compris ; un nombre
    reste un nombre."""
    schema = {"fields": [{"key": "nom", "label": "=Nom"}, {"key": "tel"}, {"key": "solde"},
                         {"key": "note"}, {"key": "tags"}]}
    store = _Store(schema, [
        {"rows": [{"_id": "r1", "nom": '=HYPERLINK("x";"y")', "tel": "+33123456789",
                   "solde": -5, "note": "client fidèle", "tags": ["@a", "b"],
                   "_created_at": "2026-09-01 10:00:00", "_updated_at": "2026-09-01 10:00:00"}],
         "next_cursor": None},
    ])
    client = TestClient(_app(monkeypatch, store))

    lignes = _rows(client.get("/api/datastores/174/rows/export.csv").text)

    assert lignes[0][:5] == ["'=Nom", "tel", "solde", "note", "tags"]
    assert lignes[1][:5] == ["'=HYPERLINK(\"x\";\"y\")", "'+33123456789", "-5",
                             "client fidèle", "'@a; b"]
