"""L'origine survit à une RÉÉCRITURE — la propriété vit dans la séquence (#322).

Le défaut que ces tests figent n'était pas dans `_merge_column` : il était dans le
fait que `update_row` avait sa PROPRE fusion. Mes tests d'alors exerçaient la
fonction que j'avais écrite, pas le chemin qu'un agent emprunte — donc « l'origine
survit » était vrai de ce que je testais et faux de ce qu'on utilise.

D'où la forme de ces tests : ils passent par `DatastorePg.update_row` et
`append_row`, ils écrivent DEUX FOIS, et ils vérifient l'état APRÈS la seconde.
Une écriture seule ne prouve rien de la survie.

Les quatre cas sont ceux de la session de campagne, rejoués ici après l'avoir été sur la
vraie surface (`data_write`, préprod) — le store est ce que la surface appelle.
"""
from __future__ import annotations

import pytest

from oto_mcp.datastore.core import DatastorePg


class _Db:
    """Base en mémoire : le round-trip suffit, et c'est LUI qu'on veut exercer."""

    def __init__(self):
        self.rows: dict = {}

    # --- ce que le store appelle
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


def _val(db, key="contact1_nom"):
    return DatastorePg._row_to_dict(db.rows["r1"]).get(key)


def _origine(db, key="contact1_nom"):
    return DatastorePg._row_to_dict(db.rows["r1"]).get(f"{key}.origine")


# --- les quatre cas de la campagne, en SÉQUENCE ------------------------------------

def test_a_flat_rewrite_keeps_the_origin(store):
    """LE cas qui échouait. `update_row` est le patch par `id` — le geste le plus
    courant d'un agent, et celui que ma correction initiale avait manqué."""
    s, db = store
    s.update_row("t", "r1", {"contact1_nom": {"valeur": "DUPONT Jean",
                                              "origine": "DUPONT Jean"}},
                  origine_override=True)
    s.update_row("t", "r1", {"contact1_nom": "MARTIN Claire"})
    assert _val(db) == "MARTIN Claire"
    assert _origine(db) == "DUPONT Jean", "l'origine ne doit pas suivre la valeur"


def test_a_layered_rewrite_without_origin_keeps_it(store):
    s, db = store
    s.update_row("t", "r1", {"contact1_nom": {"valeur": "DUPONT Jean",
                                              "origine": "DUPONT Jean"}},
                  origine_override=True)
    s.update_row("t", "r1", {"contact1_nom": {"valeur": "DURAND Paul",
                                              "comment": "registre"}})
    assert _val(db) == "DURAND Paul"
    assert _origine(db) == "DUPONT Jean"


def test_an_explicit_origin_replaces_it(store):
    """Pas de verrou : un ré-import repose une nouvelle valeur de départ."""
    s, db = store
    s.update_row("t", "r1", {"contact1_nom": {"valeur": "x", "origine": "vieux"}}, origine_override=True)
    s.update_row("t", "r1", {"contact1_nom": {"valeur": "y", "origine": "neuf"}}, origine_override=True)
    assert _origine(db) == "neuf"


def test_the_socle_import_then_the_agent(store):
    """Le flux de la campagne de bout en bout : le socle client pose l'origine sur un
    champ qu'aucun agent n'a renseigné, puis l'agent le renseigne. C'est le cas
    NOMINAL, et c'est celui où la lecture rendait l'enveloppe."""
    s, db = store
    s.update_row("t", "r1", {"contact1_nom": {"origine": "SOCLE Client"}}, origine_override=True)
    assert _val(db) is None, "pas d'objet rendu : la valeur n'est pas encore posée"
    assert _origine(db) == "SOCLE Client"
    s.update_row("t", "r1", {"contact1_nom": "Renseigné par agent"})
    assert _val(db) == "Renseigné par agent"
    assert _origine(db) == "SOCLE Client"


# --- le croisement que personne ne couvrait -----------------------------------

def test_the_write_protection_still_applies_to_a_layered_column(monkeypatch, store):
    """Les tests du verrou portent sur le bail, les miens sur les couches — le
    CROISEMENT n'était couvert par personne. Une colonne à couches ne doit pas
    échapper à la protection d'écriture parce qu'elle passe par une autre fusion."""
    from oto_mcp.datastore import core as ds
    s, db = store
    appels = []
    monkeypatch.setattr(s, "_assert_writable",
                        lambda ns_id, row_id: appels.append(row_id))
    s.update_row("t", "r1", {"contact1_nom": {"valeur": "x", "origine": "o"}}, origine_override=True)
    assert appels == ["r1"], "la garde de bail doit s'appliquer AUSSI sur une couche"


# --- #695 : l'origine VIDE ne doit pas laisser de coquille ------------------------

def test_un_null_sur_une_origine_VIDE_ne_laisse_pas_de_coquille(store):
    """Signal #695, mesuré sur trois lignes remises à zéro : effacer un champ enum par
    `null` laissait `{"origine": ""}` — une enveloppe SANS valeur, qui n'est plus une
    valeur d'énumération valide et rend la ligne INVISIBLE au filtrage et aux facettes.

    Les quatre champs touchés étaient exactement ceux qui portaient une couche
    `origine` ; les champs texte nullés au même appel n'avaient pas ce résidu.

    La cause : `_merge_column` lisait le vide avec `is None`, alors que la pose système
    écrit `""` quand il n'y avait pas de valeur d'avant. C'est le défaut de #608 un
    cran plus loin — deux notions de vide qui divergent sur la chaîne vide — et c'est
    ce que l'alias public `est_vide` existe pour empêcher.

    ⚠️ Par le CHEMIN, pas par `_merge_column` : c'est la leçon de l'en-tête de ce
    fichier, et elle vaut deux fois ici."""
    s, db = store
    s.update_row("t", "r1", {"qualification": {"valeur": "qualifie", "origine": ""}}, origine_override=True)
    s.update_row("t", "r1", {"qualification": None})
    ligne = DatastorePg._row_to_dict(db.rows["r1"])
    assert ligne.get("qualification") is None
    assert "qualification.origine" not in ligne, (
        "la coquille est de retour : la ligne redevient invisible au filtrage")
    # Et la cellule est bien VIDE en base, pas une enveloppe déguisée.
    assert db.rows["r1"]["data"].get("qualification") in (None, ""), \
        db.rows["r1"]["data"].get("qualification")


def test_un_null_sur_une_origine_PLEINE_la_preserve(store):
    """Le versant qu'il ne faut PAS casser en corrigeant l'autre : une origine réelle
    est le point de départ, parfois l'unique copie de la valeur remise. Elle survit à
    l'effacement de la valeur — c'est écrit dans `schema.py` (« elle décrit le point de
    DÉPART, pas la valeur courante, et c'est pourquoi elle est la seule à survivre »).

    Sans ce test, la correction de la coquille aurait pu être écrite comme « on efface
    tout », ce que le signalement proposait en première option — et qui détruirait une
    donnée que personne ne peut reconstituer."""
    s, db = store
    s.update_row("t", "r1", {"qualification": {"valeur": "qualifie",
                                               "origine": "brut-source"}},
                  origine_override=True)
    s.update_row("t", "r1", {"qualification": None})
    ligne = DatastorePg._row_to_dict(db.rows["r1"])
    assert ligne.get("qualification") is None          # la valeur est bien effacée
    assert ligne.get("qualification.origine") == "brut-source", \
        "l'origine réelle a été détruite — c'était peut-être son unique copie"
