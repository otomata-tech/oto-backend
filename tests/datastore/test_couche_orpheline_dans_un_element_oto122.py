"""Une clé pointée orpheline dans un élément devenait un attribut littéral.

Dans un élément de liste, `champ.couche` n'est rangée en couche que si `champ` est
présent DANS LE MÊME ÉLÉMENT — un élément n'a ni schéma ni identité, il n'y a rien
d'autre à consulter. Écrite seule, elle n'était ni rangée ni refusée : stockée telle
quelle, sous ce nom littéral. Elle devenait alors invisible au filtre et au tri qui
la nomment (ils lisent la couche `data->'champ'->>'couche'`, jamais l'attribut
littéral), et elle passait même sous le mode strict, qui juge les colonnes de
premier niveau.

Même famille que la colonne parasite d'une ligne mal enveloppée (#117) : une
écriture qui RÉUSSIT en fabriquant quelque chose que personne ne cherchera jamais.

⚠️ La fonction avait DEUX silences, pas un : la base absente, et la base présente
sous la forme d'un objet que rien ne déclare fait de couches. Les deux finissaient
en attribut littéral. Le second est déjà refusé au premier niveau, mot pour mot —
c'est la fonction qu'on ferme, pas la branche qui s'est présentée.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.datastore.errors import RowValidationError
from oto_mcp.datastore.points import ranger_les_couches



# ⚠️ `strict: true` : le mode sous lequel la clé passait quand même. Il juge les
# colonnes de PREMIER NIVEAU — l'intérieur d'un élément ne lui est jamais soumis.
SCHEMA = {
    "key": "siren",
    "fields": [
        {"key": "siren", "type": "text"},
        {"key": "contacts", "type": "list", "of": {"type": "object", "fields": [
            {"key": "nom", "type": "text"},
            {"key": "email", "type": "email"}]}},
    ],
}


@pytest.fixture
def table(live):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns_id = db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    st = make_store("sub-test")
    st.set_schema(ns, SCHEMA)
    row = st.append_row(ns, {"siren": "377768379",
                             "contacts": [{"nom": "Ada"}]})
    return st, ns, ns_id, row["_id"]


def _donnees(ns_id: int, row_id: str) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute(
            "SELECT data FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id)).fetchone()
    return dict((r or {}).get("data") or {})


def _refus(st, ns, rid, payload) -> str:
    with pytest.raises((RowValidationError, ValueError)) as exc:
        st.update_row(ns, rid, payload)
    errs = getattr(exc.value, "errors", None)
    return " ".join(errs) if errs else str(exc.value)


# ══ le défaut : refusée, et rien d'écrit ═══════════════════════════════════════

def test_la_couche_orpheline_est_refusee_sous_strict_et_n_ecrit_rien(table):
    """Le geste exact du signal, par le chemin réel — et sous `strict: true`, le
    mode qui ne l'a jamais vue passer."""
    st, ns, ns_id, rid = table

    message = _refus(st, ns, rid, {"contacts": [{"nom.comment": "registre"}]})

    assert "LITERALLY" in message and "Nothing was written" in message
    assert _donnees(ns_id, rid)["contacts"] == [{"nom": "Ada"}], \
        "un refus ne touche à rien"


def test_le_refus_nomme_l_element_fautif(table):
    """Sur une liste de vingt fiches, « une clé est mal placée » se corrige à
    l'aveugle : le rang est ce qui rend le refus actionnable."""
    st, ns, _ns_id, rid = table

    message = _refus(st, ns, rid, {"contacts": [
        {"nom": "Ada"}, {"nom": "Bob"}, {"email.origine": "hunter"}]})

    assert "`contacts[2]`" in message
    assert "`email.origine`" in message


def test_les_deux_gestes_que_le_refus_prescrit_ecrivent_pour_de_bon(table):
    """Une destination nommée ne vaut que si elle est acceptée : les deux formes
    citées par le refus sont enchaînées derrière lui, sur la même ligne."""
    st, ns, ns_id, rid = table

    message = _refus(st, ns, rid, {"contacts": [{"nom.comment": "registre"}]})
    assert 'in the SAME item' in message and '{"nom": {"comment": …}}' in message

    # ① la base à côté, dans le même élément
    st.update_row(ns, rid, {"contacts": [{"nom": "Ada", "nom.comment": "registre"}]})
    assert _donnees(ns_id, rid)["contacts"] == [
        {"nom": {"valeur": "Ada", "comment": "registre"}}]

    # ② la forme imbriquée, pour annoter seule
    st.update_row(ns, rid, {"contacts": [{"nom": {"comment": "greffe"}}]})
    assert _donnees(ns_id, rid)["contacts"] == [{"nom": {"comment": "greffe"}}]


def test_l_autre_silence_de_la_meme_fonction_est_ferme_aussi(table):
    """La base EST là, mais c'est un objet que rien ne déclare fait de couches :
    la clé restait littérale elle aussi. Le premier niveau refuse déjà ce cas."""
    st, ns, _ns_id, rid = table

    message = _refus(st, ns, rid, {"contacts": [
        {"nom": {"prenom": "Ada", "usage": "Lovelace"}, "nom.comment": "registre"}]})

    assert "is not made of layers" in message
    assert "Nothing was written" in message


# ══ ce qui ne doit PAS bouger — le refus est étroit ═════════════════════════════

def test_la_forme_servie_se_reecrit_toujours_telle_quelle(table):
    """La règle qui tient tout le module : ce qu'on SERT doit pouvoir être réécrit
    tel quel. `_served_item` sert `nom` et `nom.comment` côte à côte."""
    st, ns, ns_id, rid = table

    st.update_row(ns, rid, {"contacts": [
        {"nom": "Ada", "nom.comment": "registre", "email": "ada@exemple.fr"}]})

    assert _donnees(ns_id, rid)["contacts"] == [
        {"nom": {"valeur": "Ada", "comment": "registre"},
         "email": "ada@exemple.fr"}]


@pytest.mark.parametrize("cle", ["Tel.mobile", "N.SIREN", "chiffre.affaires"])
def test_un_entete_de_tableur_ordinaire_traverse_intact(cle):
    """Le suffixe n'est pas une couche connue : ce n'est pas une adresse
    d'annotation, et un en-tête de tableur n'a rien à voir avec nos couches. Le
    refus ne doit pas s'élargir à ce qu'il ne comprend pas."""
    assert ranger_les_couches(None, {"contacts": [{cle: "x"}]}) == {
        "contacts": [{cle: "x"}]}


def test_l_echo_de_notre_propre_lecture_est_toujours_absorbe():
    """Un objet métier dont un champ s'appelle `comment` est SERVI comme une colonne
    à couches. Réémis, il ne doit ni être rangé ni être refusé."""
    metier = {"comment": "libre", "a": 1}
    assert ranger_les_couches(None, {"lignes": [{"bloc": metier,
                                                 "bloc.comment": "libre"}]}) == {
        "lignes": [{"bloc": metier}]}


def test_une_colonne_declaree_json_reste_exempte():
    """L'exemption `json` porte sur le CONTENU de l'objet : l'écriture le traverse
    intact, refus compris."""
    schema = {"fields": [{"key": "brut", "type": "json"}]}
    payload = {"brut": [{"nom.comment": "littéral assumé"}]}
    assert ranger_les_couches(schema, payload) == payload
