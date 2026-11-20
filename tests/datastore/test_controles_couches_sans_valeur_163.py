"""Une case sans valeur, faite de couches seules, sur les lectures et les contrôles réels (oto#163).

La matrice (`test_valeur_sql_jumelle_unwrap_163.py`) prouve la règle case par case. Ce
banc prouve ce qu'elle change pour ceux qui la consomment, sur une base initialisée par
`init_db` : la pose et le patch d'un schéma (enum, `required`, borne), les filtres
`empty`/`not_empty`/`eq`/`contains`, le regroupement et le tri typé.

Les cases sont posées en base DIRECTEMENT : c'est la forme que la production porte
(marqueur `origine: ""` de l'ancienne capture, commentaires posés seuls), et aucun
chemin d'écriture n'a à être rejoué pour la lire.
"""
from __future__ import annotations

import os
import uuid

import pytest

SUB = "usr_couches_163"
OPTIONS = ["oui", "non"]

#: Trois cases SANS valeur, une valeur hors options, une valeur conforme à couches.
LIGNES = {
    "commentaire": {"etat": {"comment": "à vérifier"}, "n": 1},
    "marqueur": {"etat": {"origine": ""}, "n": 2},
    "valeur_nulle": {"etat": {"valeur": None, "origine": "x"}, "n": 3},
    "hors_options": {"etat": "peut-etre", "n": 4},
    "conforme": {"etat": {"valeur": "oui", "comment": "registre"}, "n": 5},
}
SANS_VALEUR = {"commentaire", "marqueur", "valeur_nulle"}


@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn
    name = "oto_couches163_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    previous_url, previous_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        from oto_mcp import db
        db.upsert_user(SUB, email=f"{SUB}@couches.invalid", name=SUB)
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = previous_pool
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


@pytest.fixture
def tableau(live):
    """Un tableau neuf, sans schéma, portant les cinq lignes — `(store, adresse, ns_id, ids)`,
    l'adresse étant le numéro en texte."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns_id = db.create_datastore("user", SUB, "t163-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    ids = {}
    for nom, data in LIGNES.items():
        ids[nom] = "r-" + nom
        db.datastore_insert_row(ns_id, ids[nom], data)
    return make_store(SUB), ns, ns_id, ids


def _compte(ns_id: int, filtre: dict) -> int:
    from oto_mcp.db import datastore as dsdb
    return dsdb.datastore_count_rows(ns_id, filters=[filtre])


# ── la pose et le patch d'un schéma ───────────────────────────────────────────

def test_la_pose_d_un_enum_ne_signale_que_la_vraie_valeur_hors_options(
        tableau):
    st, ns, ns_id, _ = tableau
    out = st.set_schema(ns, {"fields": [
        {"key": "etat", "type": "enum", "options": OPTIONS},
        {"key": "n", "type": "number"}]})
    avertissement = out.get("warning") or ""
    assert "`etat` : 1 ligne(s) hors options" in avertissement, avertissement
    assert "« peut-etre » (1)" in avertissement
    assert "comment" not in avertissement and "origine" not in avertissement, (
        "une enveloppe sans valeur a été prise pour une valeur hors options")


def test_le_patch_d_un_enum_ne_signale_que_la_vraie_valeur_hors_options(
        tableau):
    st, ns, _, _ = tableau
    st.set_schema(ns, {"fields": [
        {"key": "etat", "type": "text"}, {"key": "n", "type": "number"}]})
    out = st.patch_schema(ns, fields=[{"key": "etat", "type": "enum", "options": OPTIONS}])
    avertissement = out.get("warning") or ""
    assert "`etat` : 1 ligne(s) hors options" in avertissement, avertissement
    assert "comment" not in avertissement and "origine" not in avertissement


def test_le_releve_des_valeurs_hors_options_ignore_les_cases_sans_valeur(tableau):
    from oto_mcp.db import datastore as dsdb
    _, _, ns_id, _ = tableau
    assert dsdb.datastore_offending_enum_values(ns_id, {"etat": OPTIONS}) == [
        {"field": "etat", "rows": 1, "distinct": 1,
         "values": [{"value": "peut-etre", "rows": 1}]}]


def test_le_compte_required_inclut_les_cases_sans_valeur(tableau):
    """LE compte qui change (oto#163) : il annonçait 0 ligne bloquée, il en annonce 3."""
    from oto_mcp.db import datastore as dsdb
    st, ns, ns_id, _ = tableau
    assert dsdb.datastore_rows_missing_required(ns_id, ["etat"]) == [
        {"field": "etat", "rows": len(SANS_VALEUR)}]
    out = st.set_schema(ns, {"fields": [{"key": "etat", "type": "text", "required": True},
                                        {"key": "n", "type": "number"}]})
    assert f"`etat` : {len(SANS_VALEUR)} ligne(s)" in (out.get("warning") or "")


@pytest.mark.parametrize("nom", sorted(SANS_VALEUR))
def test_ce_compte_dit_vrai_la_ligne_refuse_deja_une_autre_ecriture(tableau, nom):
    """Le compte ne décrit pas un changement de la validation : `unwrap` jugeait déjà ces
    cases vides. Écrire une AUTRE colonne sur ces lignes est refusé au nom du requis."""
    from oto_mcp.datastore.errors import RowValidationError
    st, ns, _, ids = tableau
    st.set_schema(ns, {"fields": [{"key": "etat", "type": "text", "required": True},
                                  {"key": "n", "type": "number"}]})
    with pytest.raises(RowValidationError) as e:
        st.update_row(ns, ids[nom], {"n": 9})
    assert "etat" in str(e.value)


def test_une_borne_ne_mesure_pas_le_texte_d_une_enveloppe(tableau):
    from oto_mcp.db import datastore as dsdb
    _, _, ns_id, _ = tableau
    assert dsdb.datastore_overlong_fields(ns_id, {"etat": 3}) == [
        {"field": "etat", "max_length": 3, "rows": 1, "longest": len("peut-etre")}]


def test_le_releve_des_valeurs_pour_un_motif_ignore_les_enveloppes(tableau):
    from oto_mcp.db import datastore as dsdb
    _, _, ns_id, _ = tableau
    valeurs = dsdb.datastore_field_values(ns_id, ["etat"])["etat"]["values"]
    assert sorted(v["value"] for v in valeurs) == ["oui", "peut-etre"]


# ── les lectures ───────────────────────────────────────────────────────────────

def test_empty_trouve_les_cases_sans_valeur_et_not_empty_les_ignore(tableau):
    _, _, ns_id, _ = tableau
    assert _compte(ns_id, {"field": "etat", "op": "empty"}) == len(SANS_VALEUR)
    assert _compte(ns_id, {"field": "etat", "op": "not_empty"}) == len(LIGNES) - len(SANS_VALEUR)


def test_eq_et_contains_ne_comparent_plus_le_texte_de_l_enveloppe(tableau):
    _, _, ns_id, _ = tableau
    assert _compte(ns_id, {"field": "etat", "op": "contains", "value": "origine"}) == 0
    assert _compte(ns_id, {"field": "etat", "op": "contains", "value": "vérifier"}) == 0
    assert _compte(ns_id, {"field": "etat", "op": "eq", "value": '{"origine": ""}'}) == 0
    # Témoins : la même lecture trouve toujours ce qui est une valeur.
    assert _compte(ns_id, {"field": "etat", "op": "contains", "value": "peut"}) == 1
    assert _compte(ns_id, {"field": "etat", "op": "eq", "value": "oui"}) == 1


def test_le_commentaire_reste_lisible_par_sa_couche(tableau):
    st, ns, ns_id, ids = tableau
    assert _compte(ns_id, {"field": "etat.comment", "op": "not_empty"}) == 2
    servie = st.get_row(ns, ids["commentaire"])
    assert servie["etat"] is None and servie["etat.comment"] == "à vérifier"


def test_le_regroupement_range_les_cases_sans_valeur_avec_le_vide(tableau):
    from oto_mcp.db import datastore as dsdb
    _, _, ns_id, _ = tableau
    groupes = {g["etat"]: g["count"] for g in dsdb.datastore_aggregate(ns_id, group_by="etat")}
    assert groupes == {None: len(SANS_VALEUR), "peut-etre": 1, "oui": 1}


def test_le_tri_type_compte_les_cases_sans_valeur_comme_vides(tableau):
    from oto_mcp.db import datastore as dsdb
    _, _, ns_id, _ = tableau
    sante = dsdb.datastore_order_health(ns_id, order_by="etat", order_type="enum",
                                        order_options=OPTIONS)
    assert sante == {"off_type": 1, "empty": len(SANS_VALEUR)}
    lignes = dsdb.datastore_list_rows(ns_id, order_by="etat", order_dir="asc",
                                      order_type="enum", order_options=OPTIONS)
    assert [r["row_id"] for r in lignes][-len(SANS_VALEUR):] == sorted(
        f"r-{nom}" for nom in SANS_VALEUR), "les cases sans valeur ferment la marche"
