"""Écrire une couche ne touche que ce qu'elle nomme — dans les DEUX directions (#326).

#322 avait montré que la propriété vit dans la SÉQUENCE : écrire, puis réécrire, puis
regarder. Elle vit aussi dans la DIRECTION — poser une valeur puis une origine, et
poser une origine puis une valeur, sont deux tests. Un seul des deux passait :

    ligne : naf = "58.11Z – Édition de livres"
    row = {"naf": {"origine": "58.11Z – Édition de livres"}}
    relecture → naf: None          ✗ la valeur avait disparu, sans erreur

C'est le geste NOMINAL du rattrapage de socle — poser l'origine par-dessus des valeurs
déjà présentes, le cas de tout tableau qui adopte les couches après coup. Trouvé sur
2 lignes d'essai avant d'en traiter 8 910.

Par `DatastorePg.update_row`, le chemin qu'un agent emprunte : c'est la leçon de #322,
où des tests exerçaient la fonction de fusion — celle qu'on venait d'écrire — et pas la
surface qu'on utilise.
"""
from __future__ import annotations

import pytest

from oto_mcp.datastore.core import DatastorePg


class _Db:
    def __init__(self):
        self.rows: dict = {}

    def datastore_get_row(self, ns_id, row_id):
        return self.rows.get(row_id)

    def datastore_merge_row_locked(self, ns_id, row_id, apply, now, **kw):
        cur = self.rows.get(row_id)
        if cur is None:
            return None
        merged = apply(dict(cur["data"]))
        cur["data"] = merged
        return cur, merged

    def datastore_update_row(self, ns_id, row_id, data, now, **kw):
        self.rows[row_id]["data"] = data
        return self.rows[row_id]


@pytest.fixture()
def store(monkeypatch):
    from oto_mcp.datastore import core as ds
    db = _Db()
    s = DatastorePg("u-1")
    monkeypatch.setattr(s, "_resolve", lambda ns, write=False: 1)
    monkeypatch.setattr(s, "_ns_of", lambda ns_id: {"schema": None, "namespace": "t"})
    monkeypatch.setattr(s, "_schema_of", lambda ns_id: None)
    monkeypatch.setattr(s, "_assert_writable", lambda *a, **k: None)
    monkeypatch.setattr(s, "_trace", lambda *a, **k: None)
    for name in ("datastore_get_row", "datastore_merge_row_locked",
                 "datastore_update_row"):
        monkeypatch.setattr(ds.db, name, getattr(db, name))
    db.rows["r1"] = {"row_id": "r1", "created_at": "t", "updated_at": "t", "data": {}}
    return s, db


def _lu(db, key="naf"):
    return DatastorePg._row_to_dict(db.rows["r1"]).get(key)


def _couche(db, couche, key="naf"):
    return DatastorePg._row_to_dict(db.rows["r1"]).get(f"{key}.{couche}")


def _brut(db, key="naf"):
    return db.rows["r1"]["data"].get(key)


_NAF = "58.11Z – Édition de livres"


# --- direction A : la valeur d'abord, l'origine ensuite ---------------------------

def test_writing_an_origin_over_a_plain_value_keeps_it(store):
    """LE défaut de #326, dans sa forme exacte : le rattrapage de socle."""
    s, db = store
    s.update_row("t", "r1", {"naf": _NAF})
    s.update_row("t", "r1", {"naf": {"origine": _NAF}}, origine_override=True)
    assert _lu(db) == _NAF, "la valeur a été effacée par une écriture d'origine"
    assert _couche(db, "origine") == _NAF


def test_writing_an_origin_over_a_layered_value_keeps_it(store):
    s, db = store
    s.update_row("t", "r1", {"naf": {"valeur": "X", "origine": "vieux"}}, origine_override=True)
    s.update_row("t", "r1", {"naf": {"origine": "neuf"}}, origine_override=True)
    assert _lu(db) == "X"
    assert _couche(db, "origine") == "neuf"


def test_an_origin_write_leaves_the_other_layers_alone(store):
    """La valeur ne change pas ⇒ ce qui la décrit reste vrai, donc reste."""
    s, db = store
    s.update_row("t", "r1", {"naf": {"valeur": "X", "comment": "registre",
                                     "link": "https://x"}})
    s.update_row("t", "r1", {"naf": {"origine": "socle"}}, origine_override=True)
    assert (_lu(db), _couche(db, "comment"), _couche(db, "link")) == (
        "X", "registre", "https://x")


# --- direction B : l'origine d'abord, la valeur ensuite ---------------------------

def test_writing_a_value_over_an_origin_only_column_keeps_the_origin(store):
    """L'acquis de #322, rejoué dans l'autre sens — c'est le fait que les deux
    directions doivent tenir qui rend le test nécessaire, pas la nouveauté du cas."""
    s, db = store
    s.update_row("t", "r1", {"naf": {"origine": "socle"}}, origine_override=True)
    assert _lu(db) is None, "aucune valeur n'a encore été posée"
    s.update_row("t", "r1", {"naf": "renseigné par agent"})
    assert _lu(db) == "renseigné par agent"
    assert _couche(db, "origine") == "socle"


def test_both_orders_reach_the_same_state(store):
    """La propriété, dite en une phrase : l'ORDRE ne doit rien changer à l'état."""
    s, db = store
    s.update_row("t", "r1", {"naf": "X"})
    s.update_row("t", "r1", {"naf": {"origine": "socle"}}, origine_override=True)
    apres_a = dict(db.rows["r1"]["data"])
    db.rows["r1"]["data"] = {}
    s.update_row("t", "r1", {"naf": {"origine": "socle"}}, origine_override=True)
    s.update_row("t", "r1", {"naf": "X"})
    assert dict(db.rows["r1"]["data"]) == apres_a


# --- ce que `null` veut dire ------------------------------------------------------

def test_a_null_erases_the_value_and_leaves_the_origin(store):
    """`null` est une écriture ORDINAIRE : elle vide la valeur. L'origine n'est pas
    visée, donc pas touchée — sinon vider une case perdrait la trace du départ, ce
    qu'aucun agent ne verrait venir."""
    s, db = store
    s.update_row("t", "r1", {"naf": {"valeur": "X", "origine": "socle"}}, origine_override=True)
    s.update_row("t", "r1", {"naf": None})
    assert _lu(db) is None
    assert _couche(db, "origine") == "socle"


def test_erasing_the_origin_is_asked_for(store):
    """Et quand il ne reste que la valeur, la colonne redevient PLATE : une ligne sans
    couches ne doit pas se mettre à porter une enveloppe."""
    s, db = store
    s.update_row("t", "r1", {"naf": {"valeur": "X", "origine": "socle"}}, origine_override=True)
    s.update_row("t", "r1", {"naf": {"origine": None}}, origine_override=True)
    assert _lu(db) == "X"
    assert _couche(db, "origine") is None
    assert _brut(db) == "X", f"enveloppe résiduelle : {_brut(db)!r}"


def test_erasing_everything_empties_the_column(store):
    s, db = store
    s.update_row("t", "r1", {"naf": {"valeur": "X", "origine": "socle"}}, origine_override=True)
    s.update_row("t", "r1", {"naf": {"valeur": None, "origine": None}}, origine_override=True)
    assert _lu(db) is None and _couche(db, "origine") is None


# --- ce qui n'est pas une couche ---------------------------------------------------

def test_a_json_value_that_happens_to_have_an_origin_field_is_refused(store):
    """RETOURNÉ par #329 — ce test affirmait l'acceptation jusqu'au 14/08, et il
    figeait le trou : `{"a": 1, "origine": "x"}` était stocké en donnée métier,
    donc `{"origine": "x", "sourse": "y"}` (une couche mal orthographiée)
    passait par la même porte et ÉCRASAIT la valeur sans erreur.

    L'arbitrage : l'ambiguïté ne s'écrit pas — un dict qui mêle une clé de
    couche connue et des clés étrangères se REFUSE en nommant la sortie. Le
    prix assumé : une donnée métier qui porte `origine` par coïncidence exige
    désormais de déclarer sa colonne en `json` (l'exemption), et le message du
    refus le dit."""
    from oto_mcp.datastore.errors import RowValidationError

    s, db = store
    with pytest.raises(RowValidationError) as e:
        s.update_row("t", "r1", {"naf": {"a": 1, "origine": "x"}}, origine_override=True)
    assert "json" in str(e.value), "le refus PORTE la sortie légitime"
    assert _lu(db) is None, "rien n'est écrit"


def test_a_layer_from_a_newer_version_survives_a_rewrite(store):
    """Lecteur tolérant : une couche écrite par un nœud plus récent doit traverser une
    réécriture par un nœud plus ancien, sinon un déploiement progressif perd des
    données au lieu de les ignorer."""
    s, db = store
    db.rows["r1"]["data"] = {"naf": {"valeur": "X", "origine": "socle",
                                     "couche_du_futur": "z"}}
    s.update_row("t", "r1", {"naf": {"origine": "neuf"}}, origine_override=True)
    assert _brut(db).get("couche_du_futur") == "z"


# --- le chemin par LOT passe par le même point ------------------------------------

def test_the_batch_path_merges_identically(store):
    """`_merge_into_row` (écriture par lot) et `update_row` (patch par `id`) doivent
    fusionner pareil : c'est d'avoir corrigé UN chemin sur deux qui avait fait durer
    #322. Les deux chemins sont ici comparés sur le même couple d'écritures."""
    s, db = store
    s.update_row("t", "r1", {"naf": _NAF})
    s._merge_into_row(1, "r1", {"naf": {"origine": _NAF}}, schema=None)
    assert _lu(db) == _NAF
    assert _couche(db, "origine") == _NAF
