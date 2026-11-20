"""Le tri d'une colonne SANS type de tri résolu (formule, texte, non déclarée)
doit trier, quelle que soit la face et que des filtres soient posés ou non.

Défaut trouvé sur une colonne `type: "formula"` (oto-backend#1008) : `GET
/api/datastores/{ds}/rows?order_by=<colonne formule>` rendait un ordre sans rapport
avec la colonne — ni croissant ni décroissant, l'ordre de CRÉATION des lignes (ou
son inverse). Ce n'est pas propre aux formules : `dsv2.order_spec` ne résout un type
de tri que pour `number`/`date`/`enum` ; tout le reste (texte, colonne non déclarée,
formule) trie sur la valeur lue, en texte, et c'est ce que fait `datastore_list_rows`
(le chemin MCP `data_rows`). La face REST passe, elle, par `datastore_page_with_stats`
(la requête unique page+total+compteurs, #1013) qui n'avait de branche que pour le
tri TYPÉ et pour les colonnes méta : toute autre colonne retombait en silence sur
`created_at`. Le défaut ne se produit que lorsque la CTE mince est construite, donc
dès que des FILTRES sont posés — sans filtre, `page_with_stats` rend `None` et l'ancien
chemin, juste, reprend la main. Deux faces qui répondent pareil est l'invariant
posé par #336 : ce banc le tient.

⚠️ Données 100 % fictives ; vrai PostgreSQL (`live`), jamais un double : ce qu'on
vérifie, c'est l'ordre que rend la BASE.
"""
from __future__ import annotations

import uuid

import pytest

pytest.importorskip("psycopg")

SUB = "sub-test-tri-sans-type"

# (code, nom, niveau, pays) — l'ordre de CRÉATION est volontairement sans rapport
# avec l'ordre de `rang` (formule), de `nom` (texte) et de `niveau` (enum).
LIGNES = [
    ("a", "marc", "moyen", "fr"),
    ("d", "alice", "bas", "fr"),
    ("b", "zoe", "haut", "fr"),
    ("c", "hugo", "moyen", "fr"),
    ("d", "jane", "haut", "be"),
    ("b", "adam", "bas", "fr"),
    ("a", "lea", "haut", "fr"),
    ("c", "tom", "bas", "be"),
]

# a→3, b→1, c→2, tout le reste (d)→5 : un barème, pas une copie de `code`.
FORMULE = 'IFS(code="a"; "3"; code="b"; "1"; code="c"; "2"; TRUE(); "5")'
RANG = {"a": "3", "b": "1", "c": "2", "d": "5"}
ENUM_OPTIONS = ["moyen", "haut", "bas"]      # ni alphabétique ni inverse


@pytest.fixture(scope="module")
def tableau(live):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store

    ns = str(db.create_datastore("user", SUB, "t-tri-" + uuid.uuid4().hex[:6]))
    st = make_store(SUB)
    st.set_schema(ns, {"fields": [
        {"key": "code", "type": "text"},
        {"key": "nom", "type": "text"},
        {"key": "niveau", "type": "enum", "options": ENUM_OPTIONS},
        {"key": "pays", "type": "text"},
        {"key": "rang", "type": "formula", "formula": FORMULE},
    ]})
    ids: dict[str, dict] = {}
    for code, nom, niveau, pays in LIGNES:
        r = st.append_row(ns, {"code": code, "nom": nom, "niveau": niveau, "pays": pays})
        ids[r["_id"]] = {"code": code, "nom": nom, "niveau": niveau, "pays": pays,
                         "rang": RANG[code]}
    return st, ns, ids


def _attendu(ids: dict, colonne: str, sens: str, pays: str | None) -> list[str]:
    """L'ordre juste, calculé HORS base : (valeur, row_id) puis inversé pour `desc`
    — `row_id` (uuid7) suit l'ordre de création, c'est le départage de la base."""
    if colonne == "niveau":
        def valeur(i):
            return ENUM_OPTIONS.index(ids[i]["niveau"])
    else:
        def valeur(i):
            return ids[i][colonne]
    retenus = [i for i in ids if pays is None or ids[i]["pays"] == pays]
    return sorted(retenus, key=lambda i: (valeur(i), i), reverse=(sens == "desc"))


def _ids(page: dict) -> list[str]:
    return [r["_id"] for r in page["rows"]]


COLONNES = ["rang", "nom", "niveau"]        # formule, texte, enum (témoin)
SENS = ["asc", "desc"]
FILTRES = [None, "fr"]                       # sans filtre / un filtre sur `pays`


@pytest.mark.parametrize("pays", FILTRES, ids=["sans_filtre", "avec_filtre"])
@pytest.mark.parametrize("sens", SENS)
@pytest.mark.parametrize("colonne", COLONNES)
def test_face_rest_page_rows_trie_juste(tableau, colonne, sens, pays):
    st, ns, ids = tableau
    filtre = None if pays is None else [{"field": "pays", "op": "eq", "value": pays}]
    page = st.page_rows(ns, order_by=colonne, order_dir=sens, limit=50, filters=filtre)
    assert _ids(page) == _attendu(ids, colonne, sens, pays)


@pytest.mark.parametrize("pays", FILTRES, ids=["sans_filtre", "avec_filtre"])
@pytest.mark.parametrize("sens", SENS)
@pytest.mark.parametrize("colonne", COLONNES)
def test_face_mcp_cursor_rows_trie_juste(tableau, colonne, sens, pays):
    st, ns, ids = tableau
    filtre = None if pays is None else [{"field": "pays", "op": "eq", "value": pays}]
    page = st.cursor_rows(ns, order_by=colonne, order_dir=sens, limit=50, filters=filtre)
    assert _ids(page) == _attendu(ids, colonne, sens, pays)


@pytest.mark.parametrize("pays", FILTRES, ids=["sans_filtre", "avec_filtre"])
@pytest.mark.parametrize("sens", SENS)
@pytest.mark.parametrize("colonne", COLONNES)
def test_les_deux_faces_rendent_le_meme_ordre(tableau, colonne, sens, pays):
    """L'invariant de #336, tenu explicitement : le tri ne répond pas juste sur une
    face et faux sur l'autre."""
    st, ns, ids = tableau
    filtre = None if pays is None else [{"field": "pays", "op": "eq", "value": pays}]
    rest = st.page_rows(ns, order_by=colonne, order_dir=sens, limit=50, filters=filtre)
    mcp = st.cursor_rows(ns, order_by=colonne, order_dir=sens, limit=50, filters=filtre)
    assert _ids(rest) == _ids(mcp)


def test_le_total_et_la_pagination_restent_justes_avec_tri_et_filtre(tableau):
    """`page_with_stats` rend page ET total : corriger le tri ne doit pas les
    décaler. Deux pages de 3 recollent l'ordre entier, le total décrit le jeu filtré."""
    st, ns, ids = tableau
    filtre = [{"field": "pays", "op": "eq", "value": "fr"}]
    p1 = st.page_rows(ns, order_by="rang", order_dir="asc", limit=3, offset=0, filters=filtre)
    p2 = st.page_rows(ns, order_by="rang", order_dir="asc", limit=3, offset=3, filters=filtre)
    attendu = _attendu(ids, "rang", "asc", "fr")
    assert p1["total"] == p2["total"] == len(attendu)
    assert _ids(p1) + _ids(p2) == attendu[:6]


def test_une_colonne_sans_type_ne_produit_pas_de_compteur_d_ecart(tableau):
    """Le compteur `order_health` n'existe que pour un tri TYPÉ (number/enum/date) :
    une colonne formule ou texte n'en porte pas, avec ou sans filtre (comme avant)."""
    st, ns, _ = tableau
    filtre = [{"field": "pays", "op": "eq", "value": "fr"}]
    for colonne in ("rang", "nom"):
        assert "order_health" not in st.page_rows(
            ns, order_by=colonne, order_dir="asc", limit=50, filters=filtre)


def test_le_tri_d_une_colonne_formule_pose_par_la_face_rest_ignore_created_at(tableau):
    """La signature exacte du défaut : avec un filtre, l'ordre rendu ÉTAIT l'ordre de
    création. Une suite qui redevient l'ordre de création (ou son inverse) sur une
    colonne qui n'a aucune raison d'y ressembler est le symptôme à ne plus revoir."""
    st, ns, ids = tableau
    filtre = [{"field": "pays", "op": "eq", "value": "fr"}]
    creation = sorted(i for i in ids if ids[i]["pays"] == "fr")
    for sens in SENS:
        rendu = _ids(st.page_rows(ns, order_by="rang", order_dir=sens, limit=50,
                                  filters=filtre))
        assert rendu != creation and rendu != list(reversed(creation))


def test_agregation_par_colonne_formule_regroupe_sur_la_valeur(tableau):
    """Autre chemin de lecture d'une colonne formule (`group_by`) : il lit la même
    valeur que le tri, et n'est pas affecté."""
    st, ns, ids = tableau
    groupes = {g["rang"]: g["count"] for g in st.aggregate(ns, group_by="rang")}
    attendu: dict[str, int] = {}
    for i in ids:
        attendu[ids[i]["rang"]] = attendu.get(ids[i]["rang"], 0) + 1
    assert groupes == attendu


# ── cas limites : valeurs absentes, tri sur une couche, recherche plein texte ──
# Pour ces formes, l'ordre exact est celui que rend `datastore_list_rows` (la face
# MCP) : le banc n'invente pas de sémantique, il exige que la face REST y colle.

@pytest.fixture(scope="module")
def tableau_lacunaire(live):
    """Des lignes SANS la colonne triée (`nom` absent) : la place d'une absence dans
    l'ordre est celle de la base, la même sur les deux faces."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store

    ns = str(db.create_datastore("user", SUB, "t-tri-lac-" + uuid.uuid4().hex[:6]))
    st = make_store(SUB)
    st.set_schema(ns, {"fields": [
        {"key": "code", "type": "text"},
        {"key": "nom", "type": "text"},
        {"key": "pays", "type": "text"},
        {"key": "rang", "type": "formula", "formula": FORMULE},
    ]})
    for code, nom, pays in [("a", "marc", "fr"), ("b", None, "fr"), ("c", "alice", "fr"),
                            ("d", None, "fr"), ("a", "zoe", "fr"), ("b", "hugo", "be")]:
        ligne = {"code": code, "pays": pays}
        if nom is not None:
            ligne["nom"] = nom
        st.append_row(ns, ligne)
    return st, ns


@pytest.mark.parametrize("sens", SENS)
def test_valeurs_absentes_meme_ordre_sur_les_deux_faces(tableau_lacunaire, sens):
    st, ns = tableau_lacunaire
    filtre = [{"field": "pays", "op": "eq", "value": "fr"}]
    rest = st.page_rows(ns, order_by="nom", order_dir=sens, limit=50, filters=filtre)
    mcp = st.cursor_rows(ns, order_by="nom", order_dir=sens, limit=50, filters=filtre)
    assert _ids(rest) == _ids(mcp)
    # Les cinq lignes du filtre sont toutes là : rien perdu, rien dupliqué.
    assert len(_ids(rest)) == len(set(_ids(rest))) == rest["total"] == 5


@pytest.mark.parametrize("sens", SENS)
def test_tri_sur_une_couche_avec_filtre_meme_ordre_sur_les_deux_faces(tableau, sens):
    """`rang.comment` (la couche de provenance) n'est pas amincissable : la face REST
    rend la main à l'ancien chemin, qui sait le lire — elle ne retombe plus sur
    `created_at`."""
    st, ns, _ = tableau
    filtre = [{"field": "pays", "op": "eq", "value": "fr"}]
    rest = st.page_rows(ns, order_by="rang.comment", order_dir=sens, limit=50,
                        filters=filtre)
    mcp = st.cursor_rows(ns, order_by="rang.comment", order_dir=sens, limit=50,
                         filters=filtre)
    assert _ids(rest) == _ids(mcp)


@pytest.mark.parametrize("sens", SENS)
def test_recherche_plein_texte_et_tri_meme_ordre_sur_les_deux_faces(tableau, sens):
    st, ns, _ = tableau
    rest = st.page_rows(ns, order_by="rang", order_dir=sens, limit=50, q="a")
    mcp = st.cursor_rows(ns, order_by="rang", order_dir=sens, limit=50, q="a")
    assert _ids(rest) == _ids(mcp)


def test_sans_tri_l_ordre_de_creation_reste_le_defaut(tableau):
    """Le repli sur `created_at` est LÉGITIME sans `order_by` : ce banc ne le touche pas."""
    st, ns, ids = tableau
    filtre = [{"field": "pays", "op": "eq", "value": "fr"}]
    page = st.page_rows(ns, limit=50, filters=filtre, order_dir="asc")
    assert _ids(page) == sorted(i for i in ids if ids[i]["pays"] == "fr")
