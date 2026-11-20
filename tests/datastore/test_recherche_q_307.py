"""La recherche `q` des lignes : le sens ET le fragment, et `q_scope` (#307).

Avant : une sous-chaîne stricte. « cahier » trouvait « cahiers », « cahiers » ne
trouvait pas « Le Cahier » ; « corp acme » ne trouvait pas « ACME Corp ». L'asymétrie
faisait conclure à l'absence d'une ligne qui existe.

Maintenant : chaque mot matche par le sens (tsquery `french`, accents repliés) OU par
fragment. ⚠️ Le fragment est NON NÉGOCIABLE : un bout de SIREN ou d'URL collé tel quel
doit continuer à trouver sa ligne — une recherche tokenisée seule le casserait sans un
bruit, et ce banc est ce qui l'empêche.

`q_scope` : `values` = les valeurs servies seules (ni clé, ni couche) ; `all` = la ligne
stockée entière. Valeur inconnue = refus.

Données 100 % fictives ; vrai PostgreSQL (`live`) pour tout ce qui touche au SQL.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from pydantic import ValidationError

from oto_mcp.datastore import recherche as R

SUB = "sub-test-recherche-307"


# ── le vocabulaire et sa validation (pur) ───────────────────────────────────────

def test_une_portee_inconnue_est_refusee_meme_sans_q():
    with pytest.raises(R.PorteeInconnue, match="values"):
        R.recherche("acme", "tout")
    with pytest.raises(R.PorteeInconnue):
        R.recherche(None, "VALUES")


def test_les_mots_et_la_portee_par_defaut():
    assert R.recherche("  corp   acme ") == R.Recherche(("corp", "acme"), R.PORTEE_DEFAUT)
    assert R.recherche("x", "values").portee == "values"
    assert R.recherche("   ") is None and R.recherche(None) is None


@pytest.mark.parametrize("cle", ["me.datastore.list_rows", "me.datastore.aggregate"])
def test_la_face_rest_refuse_une_portee_inconnue_par_le_type(cle):
    import oto_mcp.capabilities  # noqa: F401 — peuple le registre
    from oto_mcp.capabilities import registry

    Input = registry.by_key(cle).Input
    assert Input(datastore="1", q="x", q_scope="values").q_scope == "values"
    with pytest.raises(ValidationError):
        Input(datastore="1", q="x", q_scope="tout")


def test_la_face_noeud_porte_le_meme_parametre():
    from oto_mcp.capabilities.node_rows import NodeRowsInput
    with pytest.raises(ValidationError):
        NodeRowsInput(node_id="n", q="x", q_scope="tout")


def _outil(nom: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import datastore as t_ds

    m = FastMCP("t")
    t_ds.register(m)
    return asyncio.run(m.get_tool(nom))


@pytest.mark.parametrize("nom", ["data_rows", "data_aggregate"])
def test_la_face_mcp_sert_les_phrases_et_le_vocabulaire(nom):
    """Le texte servi pilote l'agent : les deux phrases de `datastore.recherche`, et
    `q_scope` déclaré par son vocabulaire fermé dans le schéma d'entrée."""
    proprietes = _outil(nom).parameters["properties"]
    assert proprietes["q"]["description"].startswith(R.DESCRIPTION_Q)
    assert proprietes["q_scope"]["description"] == R.DESCRIPTION_Q_SCOPE
    assert "<<recherche" not in json.dumps(proprietes)
    enum = [b["enum"] for b in proprietes["q_scope"]["anyOf"] if "enum" in b]
    assert enum == [list(R.PORTEES)]


# ── la fonction des valeurs servies (vrai PostgreSQL) ──────────────────────────

def _valeurs(data: dict) -> str:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute("SELECT datastore_valeurs_texte_v1(%s::jsonb) AS t",
                            (json.dumps(data),)).fetchone()["t"]


def test_la_fonction_ne_rend_que_les_valeurs_servies(live):
    texte = _valeurs({
        "a": "x",
        "b": {"valeur": "y", "comment": "via annuaire", "link": "https://l.example"},
        "c": {"origine": "socle"},                         # couches seules : rien
        "d": [{"n": {"valeur": "p", "comment": "cachee"}}, "s"],
        "e": 42, "f": True, "g": None,
        "h": {"json": {"profond": "z"}},                   # objet métier : ses feuilles
        "i": 'dit "bonjour"',                              # pas d'échappement JSON
    })
    assert texte == 'x y p s 42 true z dit "bonjour"'


def test_la_fonction_est_deterministe_et_vide_sur_vide(live):
    assert _valeurs({}) == ""
    d = {"z": "1", "a": ["2", {"k": "3"}]}
    assert _valeurs(d) == _valeurs(d) == "2 3 1"   # ordre de jsonb (clé courte d'abord)


def test_la_fonction_ne_se_recompile_pas_a_chaque_appel(live):
    """Le JIT recompilait la requête de la fonction À CHAQUE APPEL : 200 ms par ligne,
    donc par écriture (deux index la calculent). `SET jit = off` porté par la fonction
    elle-même — vérifié au catalogue, pas au chronomètre."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conf = conn.execute(
            "SELECT proconfig FROM pg_proc "
            "WHERE oid = 'datastore_valeurs_texte_v1(jsonb)'::regprocedure").fetchone()
    assert "jit=off" in (conf["proconfig"] or [])


# ── la recherche, de bout en bout ──────────────────────────────────────────────

LIGNES = {
    "frag":   {"code": "XCAHIERSY"},                         # « cahiers » en fragment seul
    "cahier": {"titre": "Le Cahier des charges"},
    "acme":   {"societe": "ACME Corp", "siren": "552100554"},
    "url":    {"site": "https://www.exemple-acme.fr/contact?id=42"},
    "evry":   {"ville": "Évry-Courcouronnes", "enseigne": "Cafe du Port"},
    "couche": {"email": {"valeur": "jean@exemple.fr", "comment": "trouvé via hunter",
                         "link": "https://hunter.example/jean"}},
    "cle":    {"departement_xyz": "Lyon"},
    "liste":  {"contacts": [{"nom": "Durand",
                             "role": {"valeur": "DRH", "comment": "selon annuaire"}}]},
}


@pytest.fixture(scope="module")
def tableau(live):
    """Insertion directe, `created_at` échelonné : l'ordre de création est connu, et
    sans rapport avec le classement attendu (`frag` est la plus ancienne)."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    from oto_mcp.db._conn import _connect

    db.upsert_user(SUB, email=f"{SUB}@recherche307.invalid", name=SUB)
    ns_id = db.create_datastore("user", SUB, "t307-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    with _connect() as conn:
        for rang, (rid, data) in enumerate(LIGNES.items()):
            conn.execute(
                "INSERT INTO datastore_rows (ns_id, row_id, data, created_at) "
                "VALUES (%s, %s, %s::jsonb, now() - make_interval(mins => %s))",
                (ns_id, rid, json.dumps(data), 100 - rang))
    return make_store(SUB), ns


def _ids(store, ns, q, q_scope=None, **kw) -> list[str]:
    page = store.page_rows(ns, q=q, q_scope=q_scope, limit=50, **kw)
    return [r["_id"] for r in page["rows"]]


@pytest.mark.parametrize("portee", R.PORTEES)
@pytest.mark.parametrize("q, attendu", [
    ("cahiers", {"cahier", "frag"}),         # pluriel → singulier (sens) ; fragment
    ("CAHIER", {"cahier", "frag"}),          # casse
    ("corp acme", {"acme"}),                 # ordre des mots indifférent
    ("evry", {"evry"}),                      # accent replié dans la ligne
    ("café", {"evry"}),                      # accent replié dans la saisie
])
def test_par_le_sens(tableau, portee, q, attendu):
    store, ns = tableau
    assert set(_ids(store, ns, q, portee)) == attendu


@pytest.mark.parametrize("portee", R.PORTEES)
@pytest.mark.parametrize("q, attendu", [
    ("2100", {"acme"}),                      # bout de SIREN : aucun lexème ne le porte
    ("552100554", {"acme"}),                 # le SIREN entier
    ("acme.fr/cont", {"url"}),               # bout d'URL collé tel quel
    ("id=42", {"url"}),
])
def test_le_fragment_continue_de_marcher(tableau, portee, q, attendu):
    """⚠️ Le verrou du contrat : retirer la moitié « fragment » fait échouer ce test."""
    store, ns = tableau
    assert set(_ids(store, ns, q, portee)) == attendu


@pytest.mark.parametrize("portee", R.PORTEES)
def test_l_union_mot_par_mot(tableau, portee):
    """« corp » par le sens, « 2100 » par fragment : la même ligne, aucun des deux
    moteurs ne la trouverait seul sur la saisie entière."""
    store, ns = tableau
    assert _ids(store, ns, "corp 2100", portee) == ["acme"]
    assert _ids(store, ns, "cahiers 2100", portee) == []     # chaque mot doit matcher


def test_le_classement_sens_d_abord_puis_fragment(tableau):
    """Sans tri demandé : le sens d'abord (rang), le fragment seul ensuite — même si
    la ligne trouvée par fragment est la plus ANCIENNE et qu'on demande l'ordre
    croissant de création. Les deux faces (REST `page_rows`, MCP `cursor_rows`)
    rendent le même ordre."""
    store, ns = tableau
    assert _ids(store, ns, "cahiers", order_dir="asc") == ["cahier", "frag"]
    mcp = store.cursor_rows(ns, q="cahiers", order_dir="asc", limit=50)
    assert [r["_id"] for r in mcp["rows"]] == ["cahier", "frag"]


def test_un_tri_explicite_reste_prioritaire(tableau):
    store, ns = tableau
    assert _ids(store, ns, "cahiers", order_by="_created_at",
                order_dir="asc") == ["frag", "cahier"]


def test_la_pagination_classee_par_offset(tableau):
    store, ns = tableau
    p1 = store.cursor_rows(ns, q="cahiers", order_dir="asc", limit=1)
    p2 = store.cursor_rows(ns, q="cahiers", order_dir="asc", limit=1,
                           cursor=p1["next_cursor"])
    assert [r["_id"] for r in p1["rows"] + p2["rows"]] == ["cahier", "frag"]


@pytest.mark.parametrize("q, attendu_all", [
    ("hunter", {"couche"}),                  # commentaire / lien de provenance
    ("departement_xyz", {"cle"}),            # nom de colonne
    ("annuaire", {"liste"}),                 # couche d'un attribut de fiche
])
def test_values_n_attrape_ni_cle_ni_couche(tableau, q, attendu_all):
    store, ns = tableau
    assert set(_ids(store, ns, q, "all")) == attendu_all
    assert _ids(store, ns, q, "values") == []


def test_values_lit_les_valeurs_des_couches_et_des_fiches(tableau):
    store, ns = tableau
    assert _ids(store, ns, "jean@exemple", "values") == ["couche"]
    assert _ids(store, ns, "durand drh", "values") == ["liste"]


def test_compte_et_agregat_voient_le_meme_jeu(tableau):
    store, ns = tableau
    assert store.count_rows(ns, q="hunter", q_scope="all") == 1
    assert store.count_rows(ns, q="hunter", q_scope="values") == 0
    total = store.aggregate(ns, q="cahiers", q_scope="values")
    assert total[0]["count"] == 2


def test_la_portee_inconnue_est_refusee_par_le_store(tableau):
    store, ns = tableau
    for appel in (lambda: store.page_rows(ns, q="x", q_scope="tout"),
                  lambda: store.count_rows(ns, q="x", q_scope="tout"),
                  lambda: store.aggregate(ns, q="x", q_scope="tout")):
        with pytest.raises(R.PorteeInconnue):
            appel()


# ── les index servent la requête (vrai PostgreSQL) ─────────────────────────────

def test_les_index_des_valeurs_sont_poses_et_valides(live):
    from oto_mcp.db import index_concurrent as ic
    from oto_mcp.db._conn import _connect
    from oto_mcp.db.search import INDEX_VALEURS

    with _connect() as conn:
        for index in INDEX_VALEURS:
            assert ic.scalaire_de(conn)(index.sql_validite) is True, index.nom


def test_values_passe_par_ses_deux_index(live):
    """La source unique index ↔ requête : le prédicat d'un mot de `values` se résout sur
    les DEUX GIN de la portée (le sens, le fragment). Une expression recopiée de
    travers ne casserait rien de visible — elle ferait parcourir la table."""
    from oto_mcp.db._conn import _connect
    from oto_mcp.db.search import INDEX_VALEURS, lignes_trouvees_sql

    clause, params = lignes_trouvees_sql(R.recherche("cahiers", "values"))
    with _connect() as conn:
        conn.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(next(iter(r.values())) for r in conn.execute(
            f"EXPLAIN SELECT 1 FROM datastore_rows WHERE {clause}", params).fetchall())
    for index in INDEX_VALEURS:
        assert index.nom in plan, plan


def test_all_lit_le_vecteur_materialise():
    """`all` filtre ligne à ligne sous `ns_id` (le planificateur n'y prend pas les GIN
    de `data::text`) : sa moitié lexicale lit le vecteur MATÉRIALISÉ (`search_vec`,
    #318), jamais un `to_tsvector` recalculé par ligne — 1,3 s contre 0,55 s mesurés
    sur 5 000 lignes."""
    from oto_mcp.db.search import RANK_VECTOR_COLUMN, lignes_trouvees_sql, rank_expr

    clause, _ = lignes_trouvees_sql(R.recherche("cahiers", "all"))
    assert f"({rank_expr('datastore_rows')} @@" in clause
    assert RANK_VECTOR_COLUMN in clause
