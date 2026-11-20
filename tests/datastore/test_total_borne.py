"""Le total borné de `/rows` (`total_max`) : une barre de recherche ne paie plus le
compte exact d'une recherche `q`.

Avec `q`, chaque ligne trouvée coûte au compte : l'index trigramme de la portée
`values` revérifie chaque candidat, donc recalcule ses valeurs servies. Un compte exact
paie autant d'appels que de lignes trouvées (19-22 s mesurés en prod pour ~1 200
lignes d'un tableau large), pour une barre qui n'en affiche que huit.

Le contrat : `total_max` absent = total exact, sans `total_capped` (le dashboard pagine
et exporte sur ce total) ; posé = compté au plus jusque-là, et `total_capped` DIT quand
le vrai total est au-delà — jamais un total tronqué qui se lirait exact.

Données 100 % fictives ; vrai PostgreSQL (`live`) pour tout ce qui touche au SQL.
"""
from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

SUB = "sub-test-total-borne"


@pytest.fixture(scope="module")
def tableau(live):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    from oto_mcp.db._conn import _connect

    db.upsert_user(SUB, email=f"{SUB}@total-borne.invalid", name=SUB)
    ns_id = db.create_datastore("user", SUB, "tborne-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    with _connect() as conn:
        for i in range(8):
            mot = "edition" if i < 5 else "autre"
            conn.execute(
                "INSERT INTO datastore_rows (ns_id, row_id, data) VALUES (%s, %s, %s::jsonb)",
                (ns_id, f"r{i}", json.dumps({"nom": f"ligne {i}", "note": f"maison d'{mot}"})))
    return make_store(SUB), ns, ns_id


def test_sans_borne_le_total_est_exact_et_le_contrat_inchange(tableau):
    store, ns, _ = tableau
    page = store.page_rows(ns, q="edition", limit=2)
    assert page["total"] == 5 and len(page["rows"]) == 2
    assert "total_capped" not in page


def test_au_dela_de_la_borne_le_total_le_dit(tableau):
    store, ns, _ = tableau
    page = store.page_rows(ns, q="edition", limit=2, total_max=3)
    assert page["total"] == 3 and page["total_capped"] is True
    assert len(page["rows"]) == 2, "la page ne dépend pas de la borne"


@pytest.mark.parametrize("borne", [5, 50])
def test_sous_la_borne_le_total_est_exact(tableau, borne):
    store, ns, _ = tableau
    page = store.page_rows(ns, q="edition", limit=2, total_max=borne)
    assert page["total"] == 5 and page["total_capped"] is False


def test_la_borne_vaut_aussi_sans_recherche(tableau):
    """Chemin mince (filtres seuls, total calculé avec la page) : même contrat."""
    store, ns, _ = tableau
    page = store.page_rows(ns, limit=2, total_max=3)
    assert page["total"] == 3 and page["total_capped"] is True
    assert store.page_rows(ns, limit=2, total_max=8)["total_capped"] is False


def test_le_compte_s_arrete_a_la_borne(tableau):
    from oto_mcp import db
    from oto_mcp.datastore.recherche import recherche
    _, _, ns_id = tableau
    q = recherche("edition", "values")
    assert db.datastore_count_rows(ns_id, q=q) == 5
    assert db.datastore_count_rows(ns_id, q=q, jusqua=2) == 2
    assert db.datastore_count_rows(ns_id, q=q, jusqua=9) == 5
    assert db.datastore_count_rows(ns_id, jusqua=3) == 3


def test_une_borne_nulle_est_refusee_par_le_store(tableau):
    store, ns, _ = tableau
    with pytest.raises(ValueError, match="total_max"):
        store.page_rows(ns, q="edition", total_max=0)


def test_la_face_rest_type_la_borne_sans_repli():
    """Une borne illisible ne retombe pas en silence sur le compte exact qu'elle
    devait éviter : refus, pas repli (à la différence de `limit`)."""
    import oto_mcp.capabilities  # noqa: F401 — peuple le registre
    from oto_mcp.capabilities import registry

    Input = registry.by_key("me.datastore.list_rows").Input
    assert Input(datastore="1", total_max="20").total_max == 20
    assert Input(datastore="1").total_max is None
    for mauvaise in ("beaucoup", "0", -3):
        with pytest.raises(ValidationError):
            Input(datastore="1", total_max=mauvaise)
