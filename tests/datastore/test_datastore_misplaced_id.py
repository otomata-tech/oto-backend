"""`_id` posé dans `row` au lieu du paramètre `id` (oto-backend#390, signal).

`_id` est géré par le datastore : il vit dans la colonne `row_id`, jamais dans le
blob. Il était donc filtré des données écrites — en silence. Un agent qui avait
enrichi une fiche correctement (dix-sept appels, trois contacts, deux actualités
sourcées) a écrit `row={"_id": "019f…", "statut": "enrichi", …}` sans `id=` : la
ligne visée est restée vide, une 501ᵉ ligne sans SIREN a été INSÉRÉE avec tout le
travail, aucune erreur. 28 champs repris à la main.

`hors_schema` ne pouvait rien dire : tous les noms de champs étaient bons, c'est la
DESTINATION qui était fausse. D'où une garde distincte, et au même endroit — le
store, pour qu'aucune face ne puisse l'oublier.

⚠️ AMENDÉ par #354 (16/08) — ce banc a affirmé le REFUS de `_id` dans `row`
jusqu'à cette date. La nuit de flotte du 15/08 a montré que le refus était le
deuxième meilleur remède : les agents refusés visaient tous la BONNE ligne, et
un tour de correction se payait à chaque fois. `_id` dans `row` est désormais
PROMU : c'est l'adresse de la ligne, le write-back symétrique du claim — avec le
garde-fou indissociable qu'un `_id` inconnu ne crée JAMAIS (erreur nommée).
S'y ajoute la garde du trou resté ouvert : `id` NU non déclaré au schéma, la
variante qui a réellement produit 4 lignes fantômes cette nuit-là.
"""
from __future__ import annotations

import pytest

from oto_mcp.datastore import core as dsm
from oto_mcp.datastore.core import DatastorePg

ROW = "019fdba0-dd7b-7be2-ec51-37adae1cbfa4"


@pytest.fixture()
def store(monkeypatch):
    st = DatastorePg("u", acting_org=35)
    monkeypatch.setattr(st, "_resolve", lambda ns, write=False: 7)
    calls = {"insert": [], "update": []}
    monkeypatch.setattr(dsm.db, "get_datastore_by_id",
                        lambda ns_id: {"id": ns_id, "schema": None})
    monkeypatch.setattr(dsm.db, "datastore_insert_row",
                        lambda ns_id, rid, data, *a, **k: (
                            calls["insert"].append(data) or
                            {"row_id": rid, "created_at": "t", "updated_at": "t",
                             "data": data}))
    monkeypatch.setattr(dsm.db, "datastore_get_row",
                        lambda ns_id, rid: {"row_id": rid, "created_at": "t",
                                            "updated_at": "t",
                                            "data": {"siren": "1"}})
    # #317 : l'écriture ciblée contrôle le bail avant d'écrire. Ces tests portent
    # sur l'identité de la ligne visée, pas sur le verrou — elle y est libre.
    monkeypatch.setattr(dsm.db, "datastore_active_lease", lambda ns_id, rid: None)
    def fusion(ns_id, rid, apply_fn, updated_at, **k):
        # Le patch par `id` écrit par la fusion sous verrou depuis le 12/09/2026 : elle lit
        # la ligne que le test a posée (`datastore_get_row`, résolu à l'appel), et la
        # fusion aboutie est relevée comme l'était l'UPDATE.
        ligne = dsm.db.datastore_get_row(ns_id, rid)
        if ligne is None:
            return None
        data = apply_fn(dict(ligne.get("data") or {}))
        calls["update"].append((rid, data))
        return ({"row_id": rid, "created_at": "t", "updated_at": updated_at,
                 "data": data}, data)

    monkeypatch.setattr(dsm.db, "datastore_merge_row_locked", fusion)
    return st, calls


def test_append_with_id_in_the_body_updates_that_row(store):
    """La PROMOTION (#354) : `_id` dans `row` est l'adresse de la ligne — le
    write-back tel que le claim l'a servie fusionne sur ELLE. Rien d'inséré,
    rien de refusé : le chemin paresseux est devenu le chemin juste."""
    st, calls = store
    out = st.append_row("v", {"_id": ROW, "statut": "enrichi"})
    assert calls["insert"] == []
    assert calls["update"] and calls["update"][0][0] == ROW
    assert out["statut"] == "enrichi"
    assert "_id" not in calls["update"][0][1]   # jamais écrit dans le blob


def test_a_promoted_unknown_id_never_creates(store, monkeypatch):
    """LE garde-fou indissociable de la promotion : un `_id` qui ne matche
    aucune ligne (tronqué, halluciné, purgé entre claim et write) rend une
    erreur nommée — re-fabriquer le fantôme par cette porte est interdit."""
    st, calls = store
    monkeypatch.setattr(dsm.db, "datastore_get_row", lambda ns_id, rid: None)
    with pytest.raises(ValueError, match="aucune ligne"):
        st.append_row("v", {"_id": "019f-inconnu", "statut": "enrichi"})
    assert calls["insert"] == [] and calls["update"] == []


def test_a_naked_undeclared_id_is_refused(store):
    """LE fantôme de la nuit du 15/08 : `id` (sans underscore) recopié du claim,
    sans clé métier — la fusion ne matchait rien, 4 lignes fantômes portant tout
    l'enrichissement, le tableau de la cliente pollué. Refus nommé, sur le seam
    que TOUS les chemins d'écriture traversent."""
    st, calls = store
    with pytest.raises(ValueError, match="fantôme"):
        st.append_row("v", {"id": ROW, "statut": "enrichi"})
    assert calls["insert"] == []


def test_a_declared_id_column_is_plain_data(store, monkeypatch):
    """Reconnaissance par DÉCLARATION : un CSV importé porte souvent une vraie
    colonne `id` — déclarée au schéma, elle s'écrit comme n'importe quelle
    donnée."""
    st, calls = store
    monkeypatch.setattr(
        dsm.db, "get_datastore_by_id",
        lambda ns_id: {"id": ns_id,
                       "schema": {"fields": [{"key": "id"}, {"key": "siren"}]}})
    st.append_row("v", {"id": "ext-42", "siren": "2"})
    assert calls["insert"] == [{"id": "ext-42", "siren": "2"}]


def test_a_naked_undeclared_id_in_a_batch_is_refused_too(store):
    """Le lot passe par le même seam : un `id` égaré dans une row de batch est
    refusé pareil — pas de fantôme au volume."""
    st, calls = store
    with pytest.raises(ValueError, match="fantôme"):
        st.write_rows("v", [{"siren": "1"}, {"id": ROW, "siren": "2"}])


def test_patch_with_a_coherent_id_passes(store):
    """Round-trip normal : relire une ligne entière, la modifier, la repousser avec
    son `id`. Le refuser n'apprendrait rien à personne."""
    st, calls = store
    out = st.update_row("v", ROW, {"_id": ROW, "statut": "enrichi"})
    assert calls["update"] and out["statut"] == "enrichi"
    assert "_id" not in calls["update"][0][1]   # jamais écrit dans le blob


def test_patch_with_a_divergent_id_is_refused(store):
    """Deux cibles pour une écriture : on ne devine pas laquelle."""
    st, calls = store
    with pytest.raises(ValueError, match="ne correspond pas"):
        st.update_row("v", ROW, {"_id": "019f-autre", "statut": "enrichi"})
    assert calls["update"] == []


def test_batch_row_carrying_an_id_is_refused(store):
    """Un lot dédouble par clé métier ; il ne cible pas une ligne par `_id`."""
    st, calls = store
    with pytest.raises(ValueError, match="dédouble par clé"):
        st.write_rows("v", [{"siren": "1"}, {"_id": ROW, "siren": "2"}])
    assert len(calls["insert"]) == 1       # la 1ʳᵉ passe, le lot s'arrête net


def test_other_platform_columns_stay_silently_ignored(store):
    """`_created_at`/`_claimed_by` dans un round-trip sont bénins : ils ne DÉSIGNENT
    pas la cible de l'écriture. Les refuser casserait le relire-modifier-réécrire."""
    st, calls = store
    st.append_row("v", {"siren": "1", "_created_at": "t", "_claimed_by": "agent-A"})
    assert calls["insert"][0] == {"siren": "1"}
