"""`donnees_d_origine=True` — l'appel qui APPORTE la donnée telle qu'elle a été remise.

Un import n'est pas une mécanique à part : c'est écrire des lignes. Ce qui le distingue
n'est ni le volume, ni le format, ni le chemin — **c'est qu'il apporte de la donnée
d'origine**. Avant ce paramètre, la distinction dépendait d'un ORDRE DE GESTES auquel
personne ne pensait au bon moment : le cran `origine: "system"` devait avoir été
déclaré avant que la ligne n'existe, sinon la capture paresseuse n'avait plus rien à
figer.

⚠️ **Ce silence a coûté 837 cellules sur 846** sur un tableau de campagne — cran
déclaré après coup, valeurs de la cliente déjà écrasées par des agents, et un balayage
qui n'a pu poser qu'un aveu. Un geste explicite ne se trompe pas d'ordre.

Ce banc mesure ce que porte la BASE, jamais ce que le store a bien voulu rendre : c'est
le stockage qui décide de ce qu'une restitution pourra dire dans six mois.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.datastore import schema as dsv2
# `live` vient de `conftest.py` — pytest la découvre, rien à importer.

def _store():
    from oto_mcp.datastore.core import make_store
    return make_store("sub-test")


def _blob(ns_id: int, row_id: str) -> dict:
    """Ce que porte la BASE, jamais ce que le store a bien voulu rendre."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute("SELECT data FROM datastore_rows WHERE ns_id=%s AND row_id=%s",
                         (ns_id, row_id)).fetchone()
    return dict((r or {}).get("data") or {})


SCHEMA = {"key": "siren", "fields": [
    {"key": "siren", "type": "text"},
    {"key": "raison_sociale", "type": "text"},
    {"key": "effectif", "type": "text"},
    {"key": "site_web", "type": "text"},
]}


def _table(schema=SCHEMA):
    from oto_mcp import db
    ns_id = db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    st = _store()
    st.set_schema(ns, schema)
    return st, ns, ns_id


# ── ce que l'import écrit ─────────────────────────────────────────────────────

def test_l_import_fige_les_DEUX_versions(live):
    """⚠️ Les deux, pas seulement l'origine — et c'est une mesure, pas une préférence.

    Une case qui ne porterait QUE son origine se lit `None` (`unwrap` : « pas de
    `valeur`, mais que des couches connues ⟹ la valeur n'est pas encore posée »), et
    cette règle a un JUMEAU SQL dont dépendent les filtres, les agrégats et l'index de
    clé. Les lignes d'un import paraîtraient vides."""
    st, ns, ns_id = _table()
    ligne = st.append_row(ns, {"siren": "123456789",
                               "raison_sociale": {"valeur": "DUPONT",
                                                  "comment": "fichier cliente 05/08"}},
                          donnees_d_origine=True)
    col = _blob(ns_id, ligne["_id"])["raison_sociale"]

    assert col["valeur"] == "DUPONT", "la valeur COURANTE doit exister, sinon ligne muette"
    assert col[dsv2.ORIGIN_LAYER] == {"valeur": "DUPONT",
                                      "comment": "fichier cliente 05/08"}


def test_la_provenance_voyage_dans_comment_et_atterrit_dans_les_deux(live):
    """Pas de paramètre `provenance` : le commentaire est le mécanisme qui existe déjà.
    Il décrit le même fait des deux côtés le jour de l'import — et ne le poser que sur
    l'origine ferait disparaître le commentaire de la lecture par défaut."""
    st, ns, ns_id = _table()
    ligne = st.append_row(ns, {"siren": "1", "raison_sociale":
                               {"valeur": "X", "comment": "fichier cliente 05/08"}},
                          donnees_d_origine=True)
    col = _blob(ns_id, ligne["_id"])["raison_sociale"]

    assert col["comment"] == "fichier cliente 05/08"
    assert col[dsv2.ORIGIN_LAYER]["comment"] == "fichier cliente 05/08"


def test_une_case_VIDE_ne_recoit_rien(live):
    """« La cliente n'a rien remis » et « la cliente a remis du vide » sont deux faits
    différents, et l'écart se lit à la restitution. Demandé par la campagne."""
    st, ns, ns_id = _table()
    ligne = st.append_row(ns, {"siren": "1", "raison_sociale": "X", "site_web": ""},
                          donnees_d_origine=True)
    blob = _blob(ns_id, ligne["_id"])

    assert blob["site_web"] == "", "la case reste plate"
    assert dsv2.layer_value(blob["site_web"], dsv2.ORIGIN_LAYER) is None


def test_un_ZERO_est_une_valeur_remise(live):
    """⚠️ L'axe que « vide » rate si on le teste avec `if not valeur` : un effectif nul
    ou un booléen faux SONT des données de la cliente. Les confondre avec l'absence
    effacerait leur origine, et personne ne le verrait."""
    st, ns, ns_id = _table({"key": "siren", "fields": [
        {"key": "siren", "type": "text"}, {"key": "effectif", "type": "number"}]})
    ligne = st.append_row(ns, {"siren": "1", "effectif": 0}, donnees_d_origine=True)

    assert _blob(ns_id, ligne["_id"])["effectif"][dsv2.ORIGIN_LAYER] == {"valeur": 0}


# ── ce que l'import NE fait PAS ───────────────────────────────────────────────

def test_sans_le_drapeau_RIEN_ne_change(live):
    """Le défaut est l'écriture ordinaire — et il le reste. Un paramètre qui changerait
    le comportement par défaut ferait de chaque écriture d'agent une fausse donnée
    cliente."""
    st, ns, ns_id = _table()
    ligne = st.append_row(ns, {"siren": "1", "raison_sociale": "DUPONT"})

    assert _blob(ns_id, ligne["_id"])["raison_sociale"] == "DUPONT"


def test_un_RE_IMPORT_ne_reecrit_JAMAIS_l_origine(live):
    """L'origine est figée par nature — c'est ce qui la rend fiable, et pourquoi elle
    n'a pas besoin d'être déclarée `readonly`. Une ligne retrouvée par sa clé métier
    est une mise à jour : le courant bouge, l'origine non."""
    st, ns, ns_id = _table()
    ligne = st.append_row(ns, {"siren": "123", "raison_sociale":
                               {"valeur": "DUPONT", "comment": "fichier du 05/08"}},
                          donnees_d_origine=True)
    st.write_rows(ns, [{"siren": "123", "raison_sociale":
                        {"valeur": "DUPONT SAS", "comment": "fichier du 08/09"}}],
                  key="siren", donnees_d_origine=True)
    col = _blob(ns_id, ligne["_id"])["raison_sociale"]

    assert col["valeur"] == "DUPONT SAS", "le courant doit bouger"
    assert col[dsv2.ORIGIN_LAYER] == {"valeur": "DUPONT",
                                      "comment": "fichier du 05/08"}, (
        "l'origine du premier import doit survivre au second")


def test_un_ENRICHISSEMENT_ordinaire_ne_touche_pas_l_origine(live):
    """Le geste normal après l'import : un agent écrit sa trouvaille, sans drapeau."""
    st, ns, ns_id = _table()
    ligne = st.append_row(ns, {"siren": "1", "raison_sociale": "DUPONT"},
                          donnees_d_origine=True)
    st.update_row(ns, ligne["_id"], {"raison_sociale":
                                     {"valeur": "Dupont SAS", "comment": "INSEE"}})
    col = _blob(ns_id, ligne["_id"])["raison_sociale"]

    assert col["valeur"] == "Dupont SAS" and col["comment"] == "INSEE"
    assert col[dsv2.ORIGIN_LAYER] == {"valeur": "DUPONT"}


# ── les QUATRE chemins d'écriture, pas seulement celui que j'ai regardé ───────

def test_le_LOT_pose_l_origine(live):
    """Le chemin de la campagne : `data_write(rows=[…], key="siren")`."""
    st, ns, ns_id = _table()
    st.write_rows(ns, [{"siren": "1", "raison_sociale": "A"},
                       {"siren": "2", "raison_sociale": "B"}],
                  key="siren", donnees_d_origine=True)
    for siren in ("1", "2"):
        ligne = st.list_rows(ns, filter={"siren": siren})[0]
        col = _blob(ns_id, ligne["_id"])["raison_sociale"]
        assert col[dsv2.ORIGIN_LAYER] == {"valeur": col["valeur"]}


def test_le_PATCH_par_id_pose_l_origine(live):
    """⚠️ Le chemin qu'on oublie, et le code le dit deux fois : `update_row` ne passe
    pas par `_merge_into_row`, il a son propre corps. Il a déjà été oublié pour la
    survie de l'origine, puis pour son relevé. C'est aussi « le geste le plus courant
    d'un agent »."""
    st, ns, ns_id = _table()
    ligne = st.append_row(ns, {"siren": "1"})
    st.update_row(ns, ligne["_id"], {"raison_sociale": "DUPONT"},
                  donnees_d_origine=True)

    assert _blob(ns_id, ligne["_id"])["raison_sociale"][dsv2.ORIGIN_LAYER] == {
        "valeur": "DUPONT"}


def test_les_DEUX_faces_et_l_upload_declarent_le_parametre():
    """Une déclaration servie sur une seule face fait diverger les deux — et l'upload
    est le chemin des gros volumes, celui où le silence coûte le plus cher. Le PUT ne
    portant aucun paramètre, la déclaration DOIT exister au mint."""
    import inspect
    from oto_mcp.capabilities import uploads
    from oto_mcp.capabilities.datastore import rows as face_rest
    from oto_mcp.tools import datastore as face_mcp

    for module in (face_mcp, face_rest, uploads):
        assert dsv2.PARAMETRE_DONNEES_D_ORIGINE in inspect.getsource(module), (
            f"{module.__name__} ne déclare pas le paramètre")


def test_le_texte_servi_dit_import_ET_pas_enrichissement():
    """La seule erreur qui coûte cher : marquer d'origine ce qu'un agent a établi
    présenterait son travail comme la donnée de la cliente."""
    for en in (False, True):
        t = dsv2.description_donnees_d_origine(en=en)
        assert ("enrichissement" in t) or ("enrichment" in t)
        assert ("comment" in t)


# ── la collision avec le cran `origine: "system"` ────────────────────────────
# Mesurée le 08/09/2026 sur le schéma réel d'une campagne, qui porte les DEUX
# mécanismes : le cran ancien et le geste déclaré.
#
# ⚠️ Le cran interdit à QUICONQUE d'écrire la couche d'origine (#586) — y compris à
# ce geste-ci, qui n'est pas moins un appelant que les autres. Un ré-import sur une
# ligne existante partait donc en refus, et la promesse « un ré-import est rejouable »
# était fausse dès qu'une colonne portait le cran.
#
# Les écarter ne perd rien : sur ces colonnes le cran fait déjà le travail. **Deux
# mécanismes pour le même fait — celui qui était là d'abord garde la main.**

def _table_a_cran():
    from oto_mcp import db
    ns_id = db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    st = _store()
    st.set_schema(ns, {"key": "siren", "fields": [
        {"key": "siren", "type": "text"},
        {"key": "libre", "type": "text"}]})
    return st, ns, ns_id


def test_un_RE_IMPORT_passe_sur_une_colonne_a_CRAN(live):
    """Le cas de la campagne : ligne créée SANS le paramètre, puis ré-import AVEC.
    Avant le correctif, le second appel partait en 400 sur `siren.origine`."""
    st, ns, _ = _table_a_cran()
    st.append_row(ns, {"siren": "1", "libre": "a"})
    st.append_row(ns, {"siren": "1", "libre": "b"}, donnees_d_origine=True, upsert=True)

    assert st.list_rows(ns)[0]["libre"] == "b"


def test_les_colonnes_SANS_cran_gardent_le_geste(live):
    """⚠️ L'autre moitié : l'écart vise les colonnes à cran, pas la ligne entière. Une
    correction trop large aurait désarmé le geste partout dès qu'une seule colonne
    portait le cran."""
    st, ns, ns_id = _table_a_cran()
    ligne = st.append_row(ns, {"siren": "1", "libre": "a"}, donnees_d_origine=True)

    assert _blob(ns_id, ligne["_id"])["libre"][dsv2.ORIGIN_LAYER] == {"valeur": "a"}
