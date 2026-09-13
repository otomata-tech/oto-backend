"""Le relevé des valeurs écartées doit ATTEINDRE son lecteur, sur les deux faces.

Trouvé le 08/09/2026 par la campagne, sur un tableau `strict` dont la colonne d'étape
est un `enum` :

    POST /rows   {"passe": "A"}   →  201, et la colonne vaut `null`
    PATCH /rows  {"passe": "A"}   →  400, refusé

L'asymétrie est DÉLIBÉRÉE et défendable : à la création, écarter une valeur fautive
laisse entrer les quatre-vingts autres colonnes de la ligne, là où un refus les
perdrait toutes ; à la modification, l'appelant vise un champ précis, donc le refus est
le bon geste.

⚠️ **Ce qui ne l'était pas, c'est que le relevé n'arrivait pas.** Le store produit
`valeurs_ecartees` — champ, motif, valeur rejetée — et la face MCP le sert. La face
REST ne le DÉCLARAIT pas, donc Pydantic le retirait de la réponse : l'intégrateur
voyait une création réussie, sans un mot, avec une colonne vide.

Ce que ça a coûté : une ligne d'essai née avec son étape à `null`, six passes qui l'ont
trouvée hors de leur file — « zéro travail, zéro jeton, zéro erreur » — et vingt
minutes à chercher une panne de chaîne dont la cause était une valeur avalée deux
minutes plus tôt.

**Un relevé produit mais non déclaré est un relevé qui n'existe pas** pour qui lit par
cette face. C'est la même famille que la promesse fausse de ce matin, en négatif : là
un texte disait ce que le code ne faisait pas ; ici le code dit ce qu'aucun texte ne
transporte.
"""
from __future__ import annotations

import uuid

import pytest


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store("sub-test")


def _table():
    from oto_mcp import db
    ns = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", "sub-test", ns)
    st = _store()
    st.set_schema(ns, {"key": "siren", "strict": True, "unknown_fields": "reject",
                       "fields": [
                           {"key": "siren", "type": "text"},
                           {"key": "autre", "type": "text"},
                           {"key": "passe", "type": "enum",
                            "options": ["1", "2", "finie"]}]})
    return st, ns, ns_id


def test_la_creation_ECARTE_et_le_dit(live):
    """La ligne entre, la valeur fautive non, et le relevé nomme les trois choses
    dont l'appelant a besoin : le champ, le motif, la valeur rejetée."""
    st, ns, _ = _table()
    st.append_row(ns, {"siren": "1", "autre": "gardé", "passe": "A"})
    rapport = st.off_schema_report()

    assert "valeurs_ecartees" in rapport
    (ecart,) = rapport["valeurs_ecartees"]
    assert ecart["champ"] == "passe"
    assert ecart["valeur_rejetee"] == "A"
    assert "hors options" in ecart["motif"]
    assert "ÉCARTÉES" in rapport["valeurs_ecartees_hint"]


def test_le_RESTE_de_la_ligne_est_ecrit(live):
    """La raison d'être de l'asymétrie : refuser la ligne entière ferait perdre les
    quatre-vingts autres colonnes pour une valeur d'énumération."""
    st, ns, ns_id = _table()
    st.append_row(ns, {"siren": "1", "autre": "gardé", "passe": "A"})

    ligne = st.list_rows(ns)[0]
    assert ligne["autre"] == "gardé"
    # La valeur fautive n'entre pas : rien n'est STOCKÉ pour `passe`. Depuis oto#182, la
    # ligne servie porte pourtant la colonne déclarée, à `null` — la clé présente dit
    # « aucune valeur en place », plus « cette colonne n'existe pas ». La ligne seule ne
    # dit toujours pas ce qui s'est passé : c'est pourquoi le relevé doit arriver.
    assert ligne["passe"] is None, "la valeur fautive a été servie"
    from oto_mcp import db
    assert "passe" not in db.datastore_get_row(ns_id, ligne["_id"])["data"], (
        "la valeur fautive est entrée en base")


def test_la_modification_REFUSE(live):
    """L'autre moitié de l'asymétrie, et elle est délibérée : un patch vise un champ
    précis, donc l'écarter reviendrait à ne rien faire en répondant 200."""
    from oto_mcp.datastore.errors import RowValidationError

    st, ns, _ = _table()
    rid = st.append_row(ns, {"siren": "1", "passe": "1"})["_id"]
    with pytest.raises(RowValidationError) as e:
        st.update_row(ns, rid, {"passe": "A"})
    assert "hors options" in str(e.value)


def test_la_face_REST_DECLARE_le_releve():
    """⚠️ Le cœur du défaut. Le store produisait le relevé, la face MCP le servait, et
    la face REST ne le déclarait pas — donc Pydantic le retirait. Un relevé produit
    mais non déclaré n'existe pas pour qui lit par cette face."""
    from oto_mcp.capabilities.datastore.rows import WrittenRow

    champs = WrittenRow.model_fields
    assert "valeurs_ecartees" in champs
    assert "valeurs_ecartees_hint" in champs


def test_les_DEUX_faces_le_portent():
    """Une clé servie d'un seul côté fait diverger ce que chaque face promet — et
    c'est la face REST qui porte les intégrations."""
    import inspect

    from oto_mcp.capabilities.datastore import rows as face_rest
    from oto_mcp.tools import datastore as face_mcp

    assert "off_schema_report()" in inspect.getsource(face_mcp)
    assert "valeurs_ecartees" in inspect.getsource(face_rest)
