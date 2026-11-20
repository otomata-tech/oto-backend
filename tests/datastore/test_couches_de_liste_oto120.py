"""Reposer une liste détruit les couches de ses éléments — et le disait nulle part.

La fusion se fait au grain de la COLONNE, jamais de l'élément : reposer une liste la
remplace en bloc, donc les couches de provenance de TOUS ses éléments tombent, y
compris celles des éléments dont la valeur n'a pas changé.

**Ce comportement n'est pas ce qu'on corrige ici** — une liste n'a pas d'identité
d'élément, rien ne permettrait d'aligner l'ancien et le neuf sans inventer une
convention. Ce qu'on corrige, c'est le SILENCE : les trois relevés d'écriture
(`valeurs_effacees`, `valeurs_ignorees`, `valeurs_ecartees`) ne nomment que des
colonnes de premier niveau, et cette destruction-là n'apparaissait dans aucun.

Ampleur au 06/09/2026 : plus de mille couches vivent dans des éléments de listes sur
les tableaux en production. Une équipe a failli en effacer soixante-dix en croyant
enrichir, et ne s'en est sortie que parce que son script réimbriquait chaque élément
par hasard.

⚠️ Le banc éprouve le CHEMIN d'écriture, pas `_merge_column` appelée en direct : un
relevé se prouve par un appel qui l'atteint, et cette famille de règles a déjà
divergé une fois entre le patch par `id` et la fusion par clé métier (#322). Les deux
portes sont donc exercées.
"""
from __future__ import annotations

import uuid

import pytest


SCHEMA = {
    "key": "siren",
    "fields": [
        {"key": "siren", "type": "text"},
        {"key": "contacts", "type": "list", "of": {"type": "object", "fields": [
            {"key": "nom", "type": "text"},
            {"key": "email", "type": "email"},
            {"key": "role", "type": "text"}]}},
    ],
}

# La forme SERVIE d'une liste porteuse de couches (`_served_item` aplatit
# `nom.comment` dans l'élément) : c'est celle qu'un agent relit, donc celle qu'il
# doit pouvoir réécrire telle quelle.
CONTACTS_SERVIS = [
    {"nom": "Ada", "nom.comment": "registre", "email": "ada@exemple.fr",
     "email.origine": "folk"},
    {"nom": "Bob", "nom.origine": "import-client"},
]

# Le geste qui détruit : la même liste, reconstruite sans les couches. C'est ce
# qu'écrit un agent qui enrichit ses éléments sans recopier leur provenance.
CONTACTS_SANS_COUCHES = [
    {"nom": "Ada", "email": "ada@exemple.fr"},
    {"nom": "Bob", "email": "bob@exemple.fr"},
]


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store("sub-test")


@pytest.fixture
def table(live):
    """Un tableau à colonne-liste, et UNE ligne dont trois attributs d'éléments
    portent une couche."""
    from oto_mcp import db
    ns_id = db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    st = _store()
    st.set_schema(ns, SCHEMA)
    row = st.append_row(ns, {"siren": "377768379",
                             "contacts": [dict(c) for c in CONTACTS_SERVIS]})
    return st, ns, ns_id, row["_id"]


def _donnees(ns_id: int, row_id: str) -> dict:
    """Ce que porte la BASE."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute(
            "SELECT data FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id)).fetchone()
    return dict((r or {}).get("data") or {})


def _releve(st) -> list:
    """Les couches tombées, en `(adresse, couche, valeur perdue)`."""
    return [(r["champ"], r["couche"], r["valeur"])
            for r in st.off_schema_report().get("couches_effacees") or []]


# ══ le socle : les couches sont bien là, sous la forme imbriquée ════════════════

def test_les_couches_d_elements_sont_bien_rangees_a_l_ecriture(table):
    """Sans ça, le reste du banc mesurerait une destruction qui n'a rien détruit."""
    _st, _ns, ns_id, rid = table
    contacts = _donnees(ns_id, rid)["contacts"]
    assert contacts[0]["nom"] == {"valeur": "Ada", "comment": "registre"}
    assert contacts[0]["email"] == {"valeur": "ada@exemple.fr", "origine": "folk"}
    assert contacts[1]["nom"] == {"valeur": "Bob", "origine": "import-client"}


# ══ le défaut : la destruction, désormais NOMMÉE ════════════════════════════════

def test_reposer_la_liste_detruit_les_couches_et_le_dit_par_id(table):
    """Le geste de l'incident : reconstruire ses éléments sans recopier leurs
    couches. Il reste exécuté — c'est le comportement défendu — mais il ne peut
    plus être muet."""
    st, ns, ns_id, rid = table

    st.update_row(ns, rid, {"contacts": [dict(c) for c in CONTACTS_SANS_COUCHES]})

    contacts = _donnees(ns_id, rid)["contacts"]
    assert contacts[0]["nom"] == "Ada", "la destruction est le comportement défendu"
    assert _releve(st) == [
        ("contacts[0].nom", "comment", "registre"),
        ("contacts[0].email", "origine", "folk"),
        ("contacts[1].nom", "origine", "import-client"),
    ], "combien, sur quels sous-champs, à quels rangs — sans quoi rien à rétablir"


def test_le_releve_nomme_le_rang_et_dit_le_geste_qui_sauve(table):
    """Trois couches sur trois adresses distinctes : le rang est ce qui permet de
    rétablir. Et la phrase prescrit le SEUL geste qui marche aujourd'hui."""
    st, ns, _ns_id, rid = table

    st.update_row(ns, rid, {"contacts": [dict(c) for c in CONTACTS_SANS_COUCHES]})
    releve = st.off_schema_report()

    assert all(r["ligne"] == rid for r in releve["couches_effacees"])
    hint = releve["couches_effacees_hint"]
    assert "AS A WHOLE" in hint and "set the whole list again" in hint
    assert "nom.comment" in hint, "la forme à réémettre, pas seulement le principe"


def test_la_meme_destruction_se_dit_par_la_fusion_de_lot(table):
    """L'autre porte d'écriture. Les deux ont déjà divergé sur cette famille de
    règles (#322) : un relevé branché sur une seule sous-compte exactement la
    population qu'il existe pour trouver."""
    st, ns, ns_id, _rid = table

    st.write_rows(ns, [{"siren": "377768379",
                        "contacts": [dict(c) for c in CONTACTS_SANS_COUCHES]}],
                  key="siren")

    assert [(c, l) for c, l, _v in _releve(st)] == [
        ("contacts[0].nom", "comment"),
        ("contacts[0].email", "origine"),
        ("contacts[1].nom", "origine"),
    ]


# ══ ce qui ne doit RIEN relever : le bruit tue un relevé ════════════════════════

def test_reposer_la_liste_telle_qu_elle_est_servie_ne_releve_rien(table):
    """L'aller-retour exact — le seul filet qui existait. Il ne détruit rien, donc
    il ne doit rien annoncer : un relevé qui crie sur le geste sûr n'est plus lu."""
    st, ns, ns_id, rid = table

    st.update_row(ns, rid, {"contacts": [dict(c) for c in CONTACTS_SERVIS]})

    assert _releve(st) == []
    contacts = _donnees(ns_id, rid)["contacts"]
    assert contacts[0]["nom"] == {"valeur": "Ada", "comment": "registre"}


def test_reemettre_les_couches_en_enrichissant_ne_releve_rien(table):
    """Le geste que le relevé PRESCRIT : enrichir en réémettant les couches
    servies. Il doit passer sans un mot — sinon le conseil serait faux."""
    st, ns, ns_id, rid = table

    st.update_row(ns, rid, {"contacts": [
        {"nom": "Ada", "nom.comment": "registre", "email": "ada@exemple.fr",
         "email.origine": "folk", "role": "DG"},
        {"nom": "Bob", "nom.origine": "import-client",
         "email": "bob@exemple.fr"}]})

    assert _releve(st) == []
    contacts = _donnees(ns_id, rid)["contacts"]
    assert contacts[0]["nom"] == {"valeur": "Ada", "comment": "registre"}
    assert contacts[0]["role"] == "DG", "l'enrichissement a bien eu lieu"


def test_une_ecriture_ailleurs_ne_touche_pas_la_liste_et_ne_releve_rien(table):
    """Une écriture qui ne nomme pas la liste ne la remplace pas : le relevé ne
    doit pas se déclencher sur une colonne que le geste ignore."""
    st, ns, ns_id, rid = table

    st.update_row(ns, rid, {"siren": "377768379"})

    assert _releve(st) == []
    assert _donnees(ns_id, rid)["contacts"][0]["nom"]["comment"] == "registre"


def test_effacer_la_liste_avec_null_reste_un_effacement_de_VALEUR(table):
    """`null` sur la colonne entière est déjà nommé par `valeurs_effacees`, qui rend
    la valeur COMPLÈTE — couches comprises. Le relever deux fois ferait chercher
    deux dégâts là où il n'y en a qu'un, et prescrirait deux gestes contradictoires."""
    st, ns, _ns_id, rid = table

    st.update_row(ns, rid, {"contacts": None, "siren": "377768379"})
    releve = st.off_schema_report()

    assert "couches_effacees" not in releve
    perdue = releve["valeurs_effacees"][0]
    assert perdue["champ"] == "contacts"
    assert perdue["valeur"][0]["nom"] == {"valeur": "Ada", "comment": "registre"}
