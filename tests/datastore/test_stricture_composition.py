"""Datastore — les crans de stricture COMPOSENT (#614/#678, #606, #586).

Trois déclarations, et le risque n'est pas qu'aucune ne marche : c'est qu'elles
marchent chacune dans son coin et se contredisent sur un tableau réel. Ce banc
décrit l'axe qui en fait une famille — **une seule question, posée dans un ordre
fixe, à chaque écriture** :

| # | la question | ce qui répond |
|---|---|---|
| 1 | la destination EXISTE-t-elle ? | `unknown_fields: "reject"` (#614/#678) |
| 2 | est-elle À MOI ? | `readonly` (#606), `origine: "system"` (#586) |
| 3 | la valeur est-elle recevable ? | types, bornes, options, cycle de vie |

Trois propriétés en découlent, et ce sont elles qu'on vérifie ici :

- **les trois étages sont DISJOINTS** — une colonne non déclarée n'est jamais une
  colonne réservée, et réciproquement : aucun couple ne peut se disputer la même
  écriture, donc l'ordre ne peut pas produire deux verdicts contradictoires ;
- **un cran qui ne pourrait pas s'appliquer est refusé à la POSE**, pas accepté
  puis inerte. Un cran inerte est PIRE que son absence : on cesse de surveiller ce
  qu'on croit gardé, et la lecture du schéma devient une devinette — exactement ce
  que cette famille existe pour supprimer ;
- **le geste dominant du terrain traverse les trois** : réémettre la fiche entière
  telle qu'on l'a lue. Il a déjà arrêté une campagne une fois (#623), et il la
  traverserait encore si un seul des crans refusait l'identique.
"""
from __future__ import annotations

import pytest

from oto_mcp.datastore import core as dsm
from oto_mcp.datastore import schema as dsv2
from oto_mcp.datastore.core import DatastorePg
from oto_mcp.datastore.errors import RowValidationError


# Un tableau qui porte les TROIS crans à la fois.
SCHEMA = {
    "strict": True, "key": "siren", "key_required": True,
    "unknown_fields": "reject",
    "fields": [
        {"key": "siren", "type": "text"},
        {"key": "adresse", "type": "text", "readonly": True},          # #606
        {"key": "naf", "type": "text"},
        {"key": "note", "type": "text"},
    ],
}
LIGNE = {"siren": "552081317", "adresse": "1 rue A", "naf": "62.01Z"}


def test_le_tableau_aux_trois_crans_est_un_schema_VALIDE():
    """Le premier fait à établir : rien dans la famille ne s'exclut mutuellement
    par construction. Les trois tiennent sur une même déclaration."""
    assert dsv2.validate_schema_def(SCHEMA) == []


def test_les_deux_etages_sont_disjoints():
    """Une colonne réservée est DÉCLARÉE ; une colonne refusée par
    `unknown_fields` ne l'est pas. Aucune écriture ne peut donc recevoir deux
    verdicts opposés — c'est ce qui rend l'ordre des étages sans conséquence."""
    reservees = (dsv2.readonly_fields(SCHEMA)
                 | dsv2.system_origin_fields(SCHEMA))
    declarees = {f["key"] for f in SCHEMA["fields"]}
    assert reservees <= declarees
    assert dsv2.off_schema_keys(SCHEMA, {k: "v" for k in reservees}) == []


# ── les crans qui ne pourraient pas s'appliquer, refusés à la POSE ──────────

@pytest.mark.parametrize("schema,attendu", [
    ({"key": "k", "fields": [{"key": "k", "readonly": True}]}, "clé métier"),
    ({"unknown_fields": "reject", "fields": [{"key": "k"}]}, "strict"),
    ({"strict": True, "unknown_fields": "reject", "fields": []}, "référentiel"),
])
def test_un_cran_qui_ne_pourrait_pas_s_appliquer_est_refuse(schema, attendu):
    """Un cran inerte est PIRE que son absence : on cesse de surveiller ce qu'on
    croit gardé. C'est le défaut que #614 rapporte sur `strict` lui-même."""
    errs = dsv2.validate_schema_def(schema)
    assert any(attendu in e for e in errs), errs


# ── le geste, sur le banc ────────────────────────────────────────────────────

def _fake_merge_locked(rows):
    def merge_locked(ns_id, row_id, apply_fn, updated_at, **k):
        if row_id not in rows:
            return None
        merged = apply_fn(dict(rows[row_id]))
        rows[row_id] = dict(merged)
        return ({"row_id": row_id, "created_at": "t0", "updated_at": updated_at,
                 "data": dict(merged)}, merged)
    return merge_locked


@pytest.fixture()
def banc(monkeypatch):
    st = DatastorePg("u", acting_org=35)
    etat = {"lignes": {"r1": dict(LIGNE)}, "creees": [], "maj": []}
    monkeypatch.setattr(st, "_resolve", lambda ns, write=False: 7)
    monkeypatch.setattr(dsm.db, "get_datastore_by_id",
                        lambda ns_id: {"id": ns_id, "datastore": "viviers",
                                       "schema": SCHEMA})

    def find(ns_id, key, kv):
        for rid, data in etat["lignes"].items():
            if key and str(data.get(key)) == str(kv):
                return rid
        return None

    monkeypatch.setattr(dsm.db, "datastore_find_row_id_by_key", find)
    monkeypatch.setattr(dsm.db, "datastore_get_row",
                        lambda ns_id, rid: (
                            {"row_id": rid, "created_at": "t", "updated_at": "t",
                             "data": dict(etat["lignes"][rid])}
                            if rid in etat["lignes"] else None))
    monkeypatch.setattr(dsm.db, "datastore_insert_row",
                        lambda ns_id, rid, d, *a, **k: etat["creees"].append(d))
    monkeypatch.setattr(dsm.db, "datastore_active_lease", lambda ns_id, rid: None)
    fusion = _fake_merge_locked(etat["lignes"])

    def fusion_relevee(ns_id, rid, apply_fn, updated_at, **k):
        # Le patch par `id` écrit par la fusion sous verrou depuis le 12/09/2026 : une
        # fusion ABOUTIE est une écriture, un refus levé dans `apply_fn` n'en est pas une.
        sortie = fusion(ns_id, rid, apply_fn, updated_at, **k)
        if sortie is not None:
            etat["maj"].append(rid)
        return sortie

    monkeypatch.setattr(dsm.db, "datastore_merge_row_locked", fusion_relevee)
    return st, etat


def test_LE_GESTE_DOMINANT_traverse_les_trois(banc):
    """La fiche ENTIÈRE réémise telle qu'elle a été lue — colonnes verrouillées et
    couche d'origine comprises. C'est 8 charges d'écriture sur 8 du terrain, et le
    refuser arrêterait la flotte : #623 l'a fait une fois."""
    store, etat = banc
    store.update_row("viviers", "r1", {
        "siren": "552081317", "adresse": "1 rue A", "naf": "62.01Z",
        "note": "vu"})
    assert etat["lignes"]["r1"]["note"] == "vu"


@pytest.mark.parametrize("patch,attendu", [
    # étage 1 : la destination n'existe pas
    ({"_liberation": "x"}, "aucune colonne déclarée"),
    # étage 2 : elle existe, elle n'est pas à moi — deux façons
    ({"adresse": "2 rue B"}, "adresse.comment"),
])
def test_chaque_etage_refuse_ET_N_ECRIT_RIEN(banc, patch, attendu):
    """Le refus arrive au moment où l'appelant peut encore corriger, il nomme la
    destination quand il y en a une — et surtout la ligne n'a pas bougé."""
    store, etat = banc
    avant = dict(etat["lignes"]["r1"])
    with pytest.raises(RowValidationError) as e:
        store.update_row("viviers", "r1", patch)
    assert attendu in str(e.value)
    assert etat["maj"] == []
    assert etat["lignes"]["r1"] == avant


def test_la_colonne_inventee_ne_se_deguise_pas_en_couche(banc):
    """La porte de côté à fermer : `adresse` est verrouillée, donc l'agent range
    sa valeur dans `adresse_bis`. Sur un tableau fermé, elle ne s'ouvre pas."""
    store, etat = banc
    with pytest.raises(RowValidationError):
        store.update_row("viviers", "r1", {"adresse_bis": "2 rue B"})
    assert "adresse_bis" not in etat["lignes"]["r1"]


def test_la_destination_QUE_LE_REFUS_NOMME_est_ouverte(banc):
    """Un refus qui envoie quelque part doit y envoyer pour de bon : `readonly`
    nomme `adresse.comment`, et `adresse.comment` s'écrit — cran de destination
    compris, qui pourrait le prendre pour une colonne inventée."""
    store, etat = banc
    store.update_row("viviers", "r1", {"adresse": {"comment": "registre — 20 B AV"}})
    assert dsv2.layer_value(etat["lignes"]["r1"]["adresse"], "comment") \
        == "registre — 20 B AV"
    assert dsv2.unwrap(etat["lignes"]["r1"]["adresse"]) == "1 rue A"
