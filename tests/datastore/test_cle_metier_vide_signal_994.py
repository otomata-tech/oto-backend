"""Signal feedback 994 — une clé métier DÉCLARÉE écrite à vide, et son voisin muet.

L'index `ds_bkey_<ns>` est partiel sur `IS NOT NULL` : `""` y entre. `@empty` pose `""`
à la création (`@clear`, qui le faisait aussi, est refusé à l'entrée depuis oto#140 J3), donc deux lignes « vidées » sur la clé se heurtent
à l'index — et la convergence cherchait le mot `"@empty"`, jamais la valeur stockée :
`UniqueViolation` brute, 500. Le voisin : une clé `""` en clair retrouvait l'AUTRE ligne
sans clé par la convergence, et les fusionnait sans un mot.

Correctif à la cause : refus nommé à l'ENTRÉE, avant toute recherche et tout insert.
Les bancs `live` jouent la vraie base — l'index réel, la vraie convergence.
"""
from __future__ import annotations

import uuid

import pytest
from psycopg.errors import UniqueViolation

from oto_mcp.datastore import core as dsm
from oto_mcp.datastore.core import DatastorePg

SCHEMA = {"key": "siren", "fields": [{"key": "siren", "type": "text"},
                                     {"key": "nom", "type": "text"}]}
VIDES = ["@empty", "", {"valeur": ""}]
REFUS = "empty business key designates no entity"


def _table():
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", "sub-test", ns)
    ns = str(ns_id)
    st = make_store("sub-test")
    st.set_schema(ns, SCHEMA)
    return st, ns, ns_id


def _lignes(ns_id: int) -> dict:
    """Ce que porte la BASE, jamais ce que le store a bien voulu rendre."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        rs = conn.execute("SELECT row_id, data FROM datastore_rows WHERE ns_id=%s",
                          (ns_id,)).fetchall()
    return {r["row_id"]: dict(r["data"]) for r in rs}


# ── la cause : refusée à l'entrée, écriture unitaire et lot ──────────────────

@pytest.mark.parametrize("vide", VIDES)
def test_ecriture_unitaire_d_une_cle_vide_refusee_avant_tout_insert(live, vide):
    st, ns, ns_id = _table()
    with pytest.raises(ValueError, match=REFUS) as refus:
        st.append_row(ns, {"siren": vide, "nom": "A"})
    assert not isinstance(refus.value, UniqueViolation)
    assert "leave the column out" in str(refus.value)
    assert _lignes(ns_id) == {}, "rien ne doit avoir été écrit"


@pytest.mark.parametrize("vide", VIDES)
def test_lot_d_une_cle_vide_refuse_en_nommant_la_ligne(live, vide):
    st, ns, ns_id = _table()
    with pytest.raises(ValueError, match=REFUS) as refus:
        st.write_rows(ns, [{"siren": "1", "nom": "ok"}, {"siren": vide, "nom": "B"}])
    assert "row 2/2" in str(refus.value)


def test_la_seconde_cle_empty_n_est_plus_une_violation_brute(live):
    """Le cas du signal : une ligne déjà vidée par `@empty` en base (posée comme une
    version antérieure l'écrivait), puis une seconde `@empty`. Refus nommé, pas 500."""
    from oto_mcp import db
    st, ns, ns_id = _table()
    db.datastore_insert_row(ns_id, "deja-videe",
                            {"siren": {"valeur": "", "oto.vide_assume": True}, "nom": "A"})
    with pytest.raises(ValueError, match=REFUS):
        st.append_row(ns, {"siren": "@empty", "nom": "B"})
    assert set(_lignes(ns_id)) == {"deja-videe"}


# ── le voisin : une clé `""` en clair ne fusionne plus deux entités ──────────

@pytest.mark.parametrize("geste", ["unitaire", "lot"])
def test_une_cle_vide_ne_fusionne_pas_avec_l_autre_ligne_sans_cle(live, geste):
    """Une ligne `siren=""` existe (écrite avant ce correctif). Sans le refus d'entrée,
    `""` ne cherche rien, l'insert heurte l'index, la convergence cherche `""` et
    TROUVE cette ligne : « B » écrasait « A », deux entités fusionnées en silence."""
    from oto_mcp import db
    st, ns, ns_id = _table()
    db.datastore_insert_row(ns_id, "sans-cle-a", {"siren": "", "nom": "A"})
    with pytest.raises(ValueError, match=REFUS):
        if geste == "unitaire":
            st.append_row(ns, {"siren": "", "nom": "B"})
        else:
            st.write_rows(ns, [{"siren": "", "nom": "B"}])
    assert _lignes(ns_id) == {"sans-cle-a": {"siren": "", "nom": "A"}}


def test_une_cle_absente_reste_permise(live):
    """Omettre la clé (le geste que le refus conseille) crée toujours la ligne : NULL
    n'entre pas dans l'index, deux lignes sans clé coexistent."""
    st, ns, ns_id = _table()
    st.append_row(ns, {"nom": "A"})
    st.write_rows(ns, [{"nom": "B"}])
    assert sorted(d["nom"] for d in _lignes(ns_id).values()) == ["A", "B"]


# ── le filet résiduel : UN endroit, pour l'unitaire comme pour le lot ────────

@pytest.fixture()
def course_sans_gagnante(monkeypatch):
    """Une vraie course dont la gagnante a disparu : l'insert viole l'index, et la
    ligne qui tenait la clé n'est plus trouvable."""
    st = DatastorePg("u", acting_org=35)
    monkeypatch.setattr(st, "_resolve", lambda ns, write=False: 7)
    monkeypatch.setattr(st, "declared_key", lambda ns: "member_id")
    monkeypatch.setattr(dsm.db, "get_datastore_by_id",
                        lambda ns_id: {"id": ns_id, "schema": {
                            "key": "member_id", "fields": [{"key": "member_id"}]}})
    monkeypatch.setattr(dsm.db, "datastore_find_row_id_by_key", lambda *a: None)
    # Le lot juge ses doublons de clé sur le texte que la base rend (oto#141) : sans PG, `str`.
    monkeypatch.setattr(dsm.db, "datastore_textes_de_cle",
                        lambda valeurs: [str(v) for v in valeurs])
    monkeypatch.setattr(dsm.db, "datastore_insert_row",
                        lambda ns_id, rid, data: (_ for _ in ()).throw(
                            UniqueViolation("duplicate key ds_bkey_7")))
    return st


def test_filet_unitaire_refus_nomme(course_sans_gagnante):
    with pytest.raises(ValueError, match="could no longer be found") as refus:
        course_sans_gagnante.append_row("t", {"member_id": "A"})
    assert not isinstance(refus.value, UniqueViolation)


def test_filet_lot_refus_nomme(course_sans_gagnante):
    with pytest.raises(ValueError, match="could no longer be found") as refus:
        course_sans_gagnante._write_rows_to_ns(7, [{"member_id": "A"}], key="member_id")
    assert not isinstance(refus.value, UniqueViolation)
    assert "row 1/1" in str(refus.value)
