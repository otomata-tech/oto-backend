"""Le texte des valeurs servies MATÉRIALISÉ par ligne (`db/valeurs_servies.py`).

La recherche `q_scope=values` revérifiait chaque candidat de l'index trigramme en
recalculant `datastore_valeurs_texte_v1(data)`, et la recalculait encore pour classer et
compter : son coût suivait le nombre de lignes trouvées, pas `limit`. Stocké par ligne,
le texte se relit. Ce banc tient les quatre promesses du lot :

1. **toute écriture de `data` remplit les colonnes**, quel que soit son chemin — c'est
   un déclencheur, l'ancien code comme le nouveau ;
2. **les deux chemins trouvent les mêmes lignes, dans le même ordre** — colonnes lues ou
   non, colonnes pleines ou à moitié vides ;
3. **une ligne vide n'est jamais perdue** : elle se cherche par l'ancien prédicat ;
4. **le rattrapage** remplit les lignes vides sans avancer leur révision.

Données 100 % fictives ; vrai PostgreSQL (`live`).
"""
from __future__ import annotations

import json
import pathlib
import sys
import uuid
from dataclasses import replace

import pytest

from oto_mcp.datastore.recherche import Recherche, recherche
from oto_mcp.db import search, valeurs_servies as vs

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from scripts import remplir_valeurs_servies as R  # noqa: E402

SUB = "sub-test-valeurs-materialisees"

LIGNES = {
    "frag":   {"code": "XCAHIERSY"},
    "cahier": {"titre": "Le Cahier des charges"},
    "acme":   {"societe": "ACME Corp", "siren": "552100554"},
    "url":    {"site": "https://www.exemple-acme.fr/contact?id=42"},
    "evry":   {"ville": "Évry-Courcouronnes", "enseigne": "Cafe du Port"},
    "couche": {"email": {"valeur": "jean@exemple.fr", "comment": "trouvé via hunter"}},
    "cle":    {"departement_xyz": "Lyon"},
    "liste":  {"contacts": [{"nom": "Durand", "role": {"valeur": "DRH", "comment": "x"}}]},
    "maison": {"note": "maison d'édition", "ville": "Lyon"},
}
REQUETES = ["cahiers", "corp acme", "2100", "acme.fr/cont", "café", "evry",
            "jean@exemple", "durand drh", "hunter", "lyon", "edition", "zzz-absent"]


def _texte_attendu(data: dict) -> str:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute(f"SELECT {vs.texte_de('%s::jsonb')} AS t",
                            (json.dumps(data),)).fetchone()["t"]


@pytest.fixture(scope="module")
def tableau(live):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    from oto_mcp.db._conn import _connect

    db.upsert_user(SUB, email=f"{SUB}@valeurs.invalid", name=SUB)
    ns_id = db.create_datastore("user", SUB, "tvs-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    with _connect() as conn:
        for rang, (rid, data) in enumerate(LIGNES.items()):
            conn.execute(
                "INSERT INTO datastore_rows (ns_id, row_id, data, created_at) "
                "VALUES (%s, %s, %s::jsonb, now() - make_interval(mins => %s))",
                (ns_id, rid, json.dumps(data), 100 - rang))
    return make_store(SUB), ns, ns_id


def _colonnes(ns_id: int, row_id: str) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return dict(conn.execute(
            f"SELECT {vs.COLONNE_TEXTE} AS t, {vs.COLONNE_VECTEUR}::text AS v, rev, data "
            "FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id)).fetchone())


def _sql(ordre: str, *params) -> int:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute(ordre, params).rowcount


@pytest.fixture
def lecture(monkeypatch):
    """Force le chemin : colonnes lues (True) ou ancien prédicat (False)."""
    def forcer(lues: bool):
        monkeypatch.setattr(vs, "pretes", lambda: lues)
    return forcer


def _resultats(store, ns) -> list:
    out = []
    for q in REQUETES:
        for kw in ({}, {"order_by": "_created_at", "order_dir": "asc"}):
            page = store.page_rows(ns, q=q, q_scope="values", limit=50, **kw)
            out.append((q, page["total"], [r["_id"] for r in page["rows"]]))
    return out


# ── 1. le déclencheur ─────────────────────────────────────────────────────────

def test_l_insertion_remplit_les_deux_colonnes(tableau):
    _, _, ns_id = tableau
    for rid, data in LIGNES.items():
        c = _colonnes(ns_id, rid)
        assert c["t"] == _texte_attendu(data)
        assert c["t"] is not None and c["v"] is not None


@pytest.mark.parametrize("ordre", [
    "UPDATE datastore_rows SET data = jsonb_set(data, '{titre}', '\"Registre neuf\"') "
    "WHERE ns_id = %s AND row_id = %s",
    "UPDATE datastore_rows SET data = data || '{\"titre\": \"Registre neuf\"}'::jsonb "
    "WHERE ns_id = %s AND row_id = %s",
])
def test_toute_reecriture_de_data_recalcule(tableau, ordre):
    _, _, ns_id = tableau
    rid = "w-" + uuid.uuid4().hex[:6]
    _sql("INSERT INTO datastore_rows (ns_id, row_id, data) VALUES (%s, %s, %s::jsonb)",
         ns_id, rid, json.dumps({"titre": "Ancien titre"}))
    _sql(ordre, ns_id, rid)
    c = _colonnes(ns_id, rid)
    assert "registre neuf" in c["t"].lower() and "ancien" not in c["t"].lower()
    assert c["t"] == _texte_attendu(c["data"])


def test_les_chemins_du_store_remplissent_aussi(tableau):
    """Sur un tableau à lui, qui déclare la colonne écrite : celui du module n'en
    déclare aucune, et ses recherches ne doivent pas en dépendre."""
    from oto_mcp import db
    store, _, _ = tableau
    ns_id = db.create_datastore("user", SUB, "tvs-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    store.set_schema(ns, {"fields": [{"key": "titre", "type": "text"}]})
    ecrite = store.append_row(ns, {"titre": "Ligne du store"})
    rid = ecrite.get("_id") or ecrite.get("id")
    assert "ligne du store" in _colonnes(ns_id, rid)["t"].lower()
    store.update_where(ns, {"titre": "Ligne réécrite"}, filter={"titre": "Ligne du store"})
    assert "ligne reecrite" in _colonnes(ns_id, rid)["t"].lower()


def test_data_intact_sur_ligne_remplie_ne_recalcule_rien(tableau):
    """Le déclencheur ne paie pas une écriture sans effet sur `data` : une sentinelle
    posée hors de lui survit à `SET data = data`."""
    _, _, ns_id = tableau
    _sql(f"UPDATE datastore_rows SET {vs.COLONNE_TEXTE} = 'sentinelle' "
         "WHERE ns_id = %s AND row_id = 'cle'", ns_id)
    _sql("UPDATE datastore_rows SET data = data WHERE ns_id = %s AND row_id = 'cle'", ns_id)
    assert _colonnes(ns_id, "cle")["t"] == "sentinelle"
    _sql("UPDATE datastore_rows SET data = data || '{}'::jsonb "
         "WHERE ns_id = %s AND row_id = 'cle'", ns_id)
    assert _colonnes(ns_id, "cle")["t"] == "sentinelle", "jsonb normalise : même data"
    _sql(f"UPDATE datastore_rows SET {vs.COLONNE_TEXTE} = NULL, {vs.COLONNE_VECTEUR} = NULL "
         "WHERE ns_id = %s AND row_id = 'cle'", ns_id)
    _sql("UPDATE datastore_rows SET data = data WHERE ns_id = %s AND row_id = 'cle'", ns_id)
    assert _colonnes(ns_id, "cle")["t"] == _texte_attendu(LIGNES["cle"])


# ── 2 et 3. les deux chemins, et la ligne vide ─────────────────────────────────

def test_les_deux_chemins_rendent_les_memes_lignes(tableau, lecture):
    store, ns, _ = tableau
    lecture(False)
    ancien = _resultats(store, ns)
    lecture(True)
    assert _resultats(store, ns) == ancien
    assert any(total for _, total, _ in ancien), "le banc trouve quelque chose"


def test_une_ligne_vide_reste_trouvee(tableau, lecture):
    store, ns, ns_id = tableau
    lecture(False)
    ancien = _resultats(store, ns)
    videes = _sql(f"UPDATE datastore_rows SET {vs.COLONNE_TEXTE} = NULL, "
                  f"{vs.COLONNE_VECTEUR} = NULL WHERE ns_id = %s "
                  "AND row_id IN ('acme', 'evry', 'maison', 'frag')", ns_id)
    assert videes == 4
    lecture(True)
    assert _resultats(store, ns) == ancien
    assert store.count_rows(ns, q="acme", q_scope="values") == 2


def test_la_portee_all_ne_lit_pas_ces_colonnes(monkeypatch):
    with pytest.raises(ValueError, match="values"):
        search.lignes_trouvees_sql(Recherche(("x",), "all", colonnes=True))
    monkeypatch.setattr(vs, "pretes", lambda: True)
    assert vs.resoudre(recherche("x", "all")).colonnes is False
    assert vs.resoudre(recherche("x", "values")).colonnes is True
    assert vs.resoudre(None) is None


def test_construire_la_clause_ne_lit_pas_la_base(monkeypatch):
    """La construction du SQL est PURE : l'état des index se lit à l'exécution, par
    le store (`resoudre`), et voyage dans la recherche. Un test sans base construit
    la clause d'une recherche `values` sans jamais toucher PostgreSQL."""
    from oto_mcp import db

    def interdit():
        raise AssertionError("la construction du SQL a lu la base")
    monkeypatch.setattr(vs, "pretes", interdit)
    where, _ = db._ds_where(7, recherche("x", "values"), None)
    assert f"{vs.COLONNE_VECTEUR} @@" not in where, "sans résolution : l'ancien chemin"
    where, _ = db._ds_where(7, Recherche(("x",), "values", colonnes=True), None)
    assert f"{vs.COLONNE_TEXTE} IS NULL AND" in where


def test_le_predicat_lit_les_colonnes_puis_l_ancien():
    clause, params = search.lignes_trouvees_sql(
        replace(recherche("a b", "values"), colonnes=True))
    assert f"{vs.COLONNE_VECTEUR} @@" in clause and f"{vs.COLONNE_TEXTE} ILIKE" in clause
    assert f"{vs.COLONNE_TEXTE} IS NULL AND" in clause
    assert params == ["a"] * 4 + ["b"] * 4 and clause.count("%s") == len(params)
    rang, _ = search.rang_des_lignes_sql(replace(recherche("a", "values"), colonnes=True))
    assert rang.startswith(f"ts_rank_cd(COALESCE({vs.COLONNE_VECTEUR}, ")


def test_les_index_valides_ouvrent_la_lecture(live):
    vs.oublier()
    assert vs.pretes() is True, "base neuve : le démarrage construit les deux index"
    vs.oublier()


# ── 4. le rattrapage ──────────────────────────────────────────────────────────

def test_le_rattrapage_remplit_sans_avancer_la_revision(tableau):
    _, _, ns_id = tableau
    _sql(f"UPDATE datastore_rows SET {vs.COLONNE_TEXTE} = NULL, {vs.COLONNE_VECTEUR} = NULL "
         "WHERE ns_id = %s", ns_id)
    revs = {rid: _colonnes(ns_id, rid)["rev"] for rid in LIGNES}
    lignes: list[str] = []
    a_blanc = R.executer(tableaux=[ns_id], sortie=lignes.append)
    assert a_blanc["vides_avant"] >= len(LIGNES) and a_blanc["remplies"] == 0
    assert _colonnes(ns_id, "acme")["t"] is None, "à blanc : rien d'écrit"
    assert lignes[0].startswith("À BLANC")
    fait = R.executer(appliquer=True, tableaux=[ns_id], lot=3, pause=0, sortie=lignes.append)
    assert fait["remplies"] == fait["vides_avant"] and fait["vides_apres"] == 0
    for rid, data in LIGNES.items():
        c = _colonnes(ns_id, rid)
        assert c["rev"] == revs[rid], "la révision de la ligne n'avance pas"
        if rid != "cle":
            assert c["t"] == _texte_attendu(data)
    assert R.executer(tableaux=[ns_id], sortie=lignes.append)["vides_avant"] == 0


def test_le_rattrapage_refuse_sans_declencheur(live, monkeypatch):
    monkeypatch.setattr(vs, "SQL_DECLENCHEUR_PRESENT", "SELECT 1 WHERE false")
    with pytest.raises(R.DeclencheurAbsent, match="0061"):
        R.executer(sortie=lambda _: None)


def test_un_lot_nul_est_refuse():
    with pytest.raises(ValueError, match="--lot"):
        R.executer(lot=0, sortie=lambda _: None)


# ── la pose ───────────────────────────────────────────────────────────────────

def test_la_pose_est_idempotente(live):
    """Sur une base à jour, seule la fonction est rejouée : ni `ALTER`, ni
    `CREATE TRIGGER` — leurs verrous ne sont pas repris à chaque démarrage."""
    from oto_mcp.db._conn import _connect
    from oto_mcp.db.index_concurrent import scalaire_de
    with _connect() as conn:
        assert vs.ordres_a_jouer(scalaire_de(conn)) == [vs.DDL_FONCTION]
    assert set(vs.INDEX) <= set(__import__("oto_mcp.db.index_concurrent",
                                           fromlist=["declares"]).declares())


# ── les subs : fusion de comptes et export de périmètre ───────────────────────

def test_la_fusion_de_comptes_garde_le_texte_aligne_sur_data(live):
    """`migrate_sub` repointe les colonnes de sub inventoriées (propriétaire du
    tableau, bail…) ; il ne réécrit JAMAIS `data`, contenu de l'utilisateur. Le texte
    matérialisé, qui ne dépend que de `data`, reste donc celui de la ligne — vérifié
    après une vraie fusion, sur un tableau du compte fusionné dont une valeur EST son
    sub. Une réécriture de `data`, elle, passe par le déclencheur (plus haut)."""
    from oto_mcp import db
    from oto_mcp.db._conn import _connect

    ancien, neuf = (f"sub-fusion-{x}-{uuid.uuid4().hex[:6]}" for x in ("a", "b"))
    for sub in (ancien, neuf):
        db.upsert_user(sub, email=f"{sub}@valeurs.invalid", name=sub)
    ns_id = db.create_datastore("user", ancien, "tvs-fusion-" + uuid.uuid4().hex[:6])
    _sql("INSERT INTO datastore_rows (ns_id, row_id, data) VALUES (%s, 'r', %s::jsonb)",
         ns_id, json.dumps({"responsable": ancien, "note": "suivi"}))
    assert db.migrate_sub(ancien, neuf, operator_source="banc valeurs servies")
    with _connect() as conn:
        assert conn.execute("SELECT owner_id FROM user_datastores WHERE id = %s",
                            (ns_id,)).fetchone()["owner_id"] == neuf
    c = _colonnes(ns_id, "r")
    assert c["data"]["responsable"] == ancien, "data n'est pas réécrit par la fusion"
    assert c["t"] == _texte_attendu(c["data"]) and ancien.lower() in c["t"].lower()


def test_l_export_de_perimetre_laisse_le_texte_vide():
    """Le texte recopie les valeurs de `data` en un seul blob : la règle des comptes
    (valeur ÉGALE à un sub) ne sait pas l'y réécrire. Il part vide, la cible le
    recalcule ; `data`, lui, est dénudé comme avant."""
    from oto_mcp.export_perimetre.transformation import Transformation

    t = Transformation(comptes={"t1:alice": "alice"}, vers_tenant={})
    ligne = {"ns_id": 1, "row_id": "r", "data": {"qui": "t1:alice"},
             vs.COLONNE_TEXTE: "t1:alice suivi", vs.COLONNE_VECTEUR: "'alice':2 't1':1"}
    cible = t.appliquer(vs.TABLE, ligne)
    assert cible["data"] == {"qui": "alice"}
    assert cible[vs.COLONNE_TEXTE] is None and cible[vs.COLONNE_VECTEUR] is None
    assert t.appliquer("docs", {"x": "t1:alice suivi"}) == {"x": "t1:alice suivi"}
