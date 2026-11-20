"""`list_rows` honore les opérateurs de filtre, comme `cursor_rows` (oto#74).

La surface d'affichage (`data_app`, l'app de revue) lisait par `list_rows`, qui
filtrait en Python par égalité de chaînes : `{"statut": {"in": [...]}}` y était
comparé au TEXTE d'un dictionnaire. Zéro ligne, aucune erreur — une réponse bien
formée, plausible, et fausse, indistinguable d'un tableau vide. Pendant ce temps
`count_rows` et `cursor_rows`, par le moteur SQL, rendaient le bon compte : l'app de
revue annonçait « N restantes » et ne trouvait jamais la suivante.

Le filtre passe désormais par le MÊME moteur (`_filter_clauses` → SQL) : il n'y a
plus qu'une grammaire, et un opérateur inconnu est refusé nommément.
"""
from __future__ import annotations

import uuid

import pytest


def _table():
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store

    ns = str(db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6]))
    st = make_store("sub-test")
    st.set_schema(ns, {"fields": [{"key": "ref"}, {"key": "statut"},
                                  {"key": "score", "type": "number"}]})
    for ref, statut, score in (("a", "actif", 10), ("b", "actif", 30),
                               ("c", "clos", 25), ("d", "clos", 40),
                               ("e", "clos", 5)):
        st.append_row(ns, {"ref": ref, "statut": statut, "score": score})
    return st, ns


@pytest.mark.parametrize("filtre, attendu", [
    ({"statut": "actif"}, 2),
    ({"statut": {"in": ["actif", "clos"]}}, 5),
    ({"statut": {"ne": "clos"}}, 2),
    ({"statut": {"contains": "act"}}, 2),
    ({"score": {"gt": 20}}, 3),
])
def test_list_rows_rend_ce_que_rend_le_moteur_sql(live, filtre, attendu):
    """Le tableau de l'issue, côte à côte : l'affichage et l'agent comptent pareil."""
    st, ns = _table()
    assert len(st.list_rows(ns, filter=filtre)) == attendu
    assert len(st.cursor_rows(ns, filter=filtre)["rows"]) == attendu
    assert st.count_rows(ns, filter=filtre) == attendu


def test_un_operateur_inconnu_est_refuse_pas_un_zero(live):
    st, ns = _table()
    with pytest.raises(ValueError):
        st.list_rows(ns, filter={"statut": {"pareil_que": "actif"}})


def test_ordre_et_limite_inchanges(live):
    """Plus ancienne d'abord, `limit` respectée — le contrat historique."""
    st, ns = _table()
    assert [r["ref"] for r in st.list_rows(ns, limit=3)] == ["a", "b", "c"]
    assert [r["ref"] for r in st.list_rows(ns, filter={"statut": "clos"}, limit=2)] \
        == ["c", "d"]
