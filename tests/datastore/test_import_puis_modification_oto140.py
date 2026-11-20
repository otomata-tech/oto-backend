"""Ce qu'un import laisse derrière lui quand on modifie ensuite — deux défauts réels.

Trouvés par une revue le 08/09/2026, sur le code du jour, **avant la production**. Les
deux se déclenchent sur le geste le plus banal d'une campagne : importer la donnée de
la cliente, puis corriger un seul champ.

⚠️ Le premier est né de la correction elle-même. `donnees_d_origine` enveloppe chaque
case dans ses couches (`{"valeur": …, "origine": …}`) ; la fusion par créneau, elle,
attendait une liste nue. Elle lisait donc « rien en place » et remplaçait tout — la
version d'origine comprise. **Le mécanisme posé pour conserver l'origine la détruisait
sur les listes.** Deux pièces justes séparément, fausses ensemble : c'est l'axe que ni
mon banc ni ma relecture ne traversaient, parce que chacun regardait une pièce.

Le second préexistait, mais l'import déclaré le rend atteignable : annoter la clé
métier — le geste que ce lot ENCOURAGE — changeait l'identité de la ligne.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.datastore import schema as dsv2


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store("sub-test")


def _blob(ns_id: int, row_id: str) -> dict:
    """Ce que porte la BASE, jamais ce que le store a bien voulu rendre."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute("SELECT data FROM datastore_rows WHERE ns_id=%s AND row_id=%s",
                         (ns_id, row_id)).fetchone()
    return dict((r or {}).get("data") or {})


def _table_contacts():
    from oto_mcp import db
    ns_id = db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    st = _store()
    st.set_schema(ns, {"key": "siren", "fields": [
        {"key": "siren", "type": "text"},
        {"key": "contacts", "type": "list", "of": {"key": "role", "fields": [
            {"key": "role", "type": "text"},
            {"key": "nom", "type": "text"},
            {"key": "email", "type": "text"}]}}]})
    return st, ns, ns_id


# ── ① la liste importée survit à une modification partielle ──────────────────

def test_corriger_UN_champ_ne_perd_ni_les_autres_ni_l_origine(live):
    """Le cas exact de la revue : « RH / Alice / ancien email », puis on ne corrige
    QUE l'email. Avant le correctif : Alice disparaissait et l'origine avec."""
    st, ns, ns_id = _table_contacts()
    ligne = st.append_row(ns, {"siren": "1", "contacts": [
        {"role": "RH", "nom": "Alice", "email": "ancien@x"}]},
        donnees_d_origine=True)

    st.update_row(ns, ligne["_id"], {"contacts": [{"role": "RH", "email": "nouveau@x"}]})
    col = _blob(ns_id, ligne["_id"])["contacts"]

    item = col[dsv2.VALUE_LAYER][0]
    assert item["email"] == "nouveau@x", "la correction doit passer"
    assert item["nom"] == "Alice", "les autres attributs du créneau doivent survivre"

    origine = col[dsv2.ORIGIN_LAYER][dsv2.VALUE_LAYER][0]
    assert origine["email"] == "ancien@x", (
        "la version d'origine ne bouge JAMAIS — c'est ce que la cliente a remis")
    assert origine["nom"] == "Alice"


def test_la_colonne_SANS_couches_reste_une_liste_nue(live):
    """⚠️ L'autre moitié de l'axe. Le correctif repose le résultat dans les couches
    quand il y en avait ; il ne doit pas en FABRIQUER là où la colonne était plate,
    sinon chaque écriture ordinaire changerait la forme stockée sous les lecteurs."""
    st, ns, ns_id = _table_contacts()
    ligne = st.append_row(ns, {"siren": "1", "contacts": [
        {"role": "RH", "nom": "Alice"}]})          # pas d'import déclaré

    st.update_row(ns, ligne["_id"], {"contacts": [{"role": "RH", "email": "e@x"}]})
    col = _blob(ns_id, ligne["_id"])["contacts"]

    assert isinstance(col, list), "une colonne plate doit rester plate"
    assert col[0]["nom"] == "Alice" and col[0]["email"] == "e@x"


def test_l_origine_survit_a_la_DISPARITION_d_un_creneau(live):
    """⚠️ Une liste réémise fait foi : un créneau non repris DISPARAÎT de la version
    courante — c'est le contrat, et il est délibéré (cf. `test_fusion_par_creneau`).

    Ce banc fige l'autre moitié, celle que l'import change : **la version d'origine,
    elle, garde ce que la cliente avait remis**. Sans quoi réémettre une liste sans un
    contact effacerait la preuve qu'il avait été fourni — et la restitution ne pourrait
    plus dire « voici ce que vous nous aviez donné ».

    ⚠️ J'attendais d'abord que le créneau absent survive. Il ne survit pas, et c'est
    spécifié depuis ce matin ; c'est mon attente qui était inventée, pas le code qui
    était faux. Une supposition écrite en assertion aurait gravé le bug à l'envers."""
    st, ns, ns_id = _table_contacts()
    ligne = st.append_row(ns, {"siren": "1", "contacts": [
        {"role": "RH", "nom": "Alice"}]}, donnees_d_origine=True)

    st.update_row(ns, ligne["_id"], {"contacts": [{"role": "DAF", "nom": "Bob"}]})
    col = _blob(ns_id, ligne["_id"])["contacts"]

    assert [i["role"] for i in col[dsv2.VALUE_LAYER]] == ["DAF"], (
        "la liste réémise fait foi sur la version courante")
    assert [i["role"] for i in col[dsv2.ORIGIN_LAYER][dsv2.VALUE_LAYER]] == ["RH"], (
        "ce que la cliente avait remis ne disparaît pas avec le créneau")


# ── ② la clé métier identifie par sa VALEUR, jamais par son enveloppe ────────

def test_une_cle_metier_ANNOTEE_designe_la_MEME_ligne(live):
    """⚠️ Enrichir la provenance ne change pas ce qu'une donnée EST.

    `{"code": "A"}` et `{"code": {"valeur": "A", "comment": "fichier source"}}` sont la
    même identité. Avant le correctif, le lookup cherchait l'objet entier : ligne
    « introuvable », puis insertion, puis `UniqueViolation` sur l'index de clé — un
    500 opaque sur le geste que ce lot encourage."""
    from oto_mcp import db
    st = _store()
    ns = str(db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6]))
    st.set_schema(ns, {"key": "code", "fields": [
        {"key": "code", "type": "text"}, {"key": "v", "type": "text"}]})

    st.append_row(ns, {"code": "A", "v": "1"})
    st.append_row(ns, {"code": {"valeur": "A", "comment": "fichier source"}, "v": "2"},
                  upsert=True)

    lignes = st.list_rows(ns)
    assert len(lignes) == 1, "une annotation ne crée pas une seconde identité"
    assert lignes[0]["v"] == "2", "la seconde écriture a bien fusionné"


def test_le_LOT_aussi_apparie_sur_une_cle_annotee(live):
    """Le chemin des imports — celui qui porte huit mille lignes, donc celui où un
    doublon coûte le plus cher."""
    from oto_mcp import db
    st = _store()
    ns = str(db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6]))
    st.set_schema(ns, {"key": "code", "fields": [
        {"key": "code", "type": "text"}, {"key": "v", "type": "text"}]})

    st.write_rows(ns, [{"code": "A", "v": "1"}], key="code")
    st.write_rows(ns, [{"code": {"valeur": "A", "comment": "src"}, "v": "2"}],
                  key="code", donnees_d_origine=True)

    assert len(st.list_rows(ns)) == 1
