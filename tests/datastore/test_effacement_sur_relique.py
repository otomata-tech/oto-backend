"""Un effacement qui ne détruit rien, annoncé comme un succès.

`{"contact1_nom": {"link": "@empty"}}` sur une ligne qui porte une relique littérale
`data->>'contact1_nom.link'` était **accepté**, rapporté comme réussi, et **ne touchait
rien** : le geste vise la couche imbriquée, la donnée est ailleurs.

⚠️ Signalé le 08/09/2026 par une campagne qui croyait avoir retiré une adresse de
profil personnel. Elle ne l'a su qu'en relisant par acquit de conscience.

**Un zéro se met en doute ; un succès ne se met pas en doute.** C'est ce qui rend ce
défaut pire que le faux zéro du filtre corrigé le même jour : là on doutait d'un
chiffre, ici on ne doute de rien.

⚠️ **Seul l'EFFACEMENT est refusé, et c'est délibéré.** Refuser toute écriture visant
une couche dont une relique existe casserait le geste normal : 500 lignes d'un tableau
de production portent `qualification.comment` en relique, et leurs agents écrivent
`{"qualification": {"comment": …}}` tous les jours — ce geste-là fonctionne, il écrit
l'imbriqué. Ce qui ne fonctionne pas, c'est de croire DÉTRUIRE.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.datastore import reliques as rq
from oto_mcp.datastore.errors import RowValidationError


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store("sub-test")


def _ligne_avec_relique():
    from oto_mcp import db
    from oto_mcp.db._conn import _connect

    ns = str(db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6]))
    st = _store()
    st.set_schema(ns, {"key": "siren", "fields": [{"key": "siren", "type": "text"},
                                                  {"key": "contact1_nom", "type": "text"}]})
    rid = st.append_row(ns, {"siren": "1"})["_id"]
    with _connect() as c:
        c.execute("UPDATE datastore_rows SET data = data || %s::jsonb WHERE row_id=%s",
                  ('{"contact1_nom.link": "https://profil"}', rid))
        c.commit()
    return st, ns, rid


# ── ce que la garde attrape ──────────────────────────────────────────────────

@pytest.mark.parametrize("payload", [
    {"contact1_nom": {"link": "@empty"}},   # la forme imbriquée : vise à côté
    {"contact1_nom": {"link": None}},       # `null` : même intention
])
def test_l_effacement_qui_ne_detruirait_rien_est_REFUSE(live, payload):
    st, ns, rid = _ligne_avec_relique()
    with pytest.raises(RowValidationError) as e:
        st.update_row(ns, rid, payload)

    msg = str(e.value)
    assert "would destroy NOTHING" in msg
    assert "nothing was written" in msg
    assert "data_drop_column" in msg, "le refus nomme le geste qui ABOUTIT"
    assert "contact1_nom.link" in msg, "avec le nom LITTÉRAL, point compris"


def test_une_ecriture_ORDINAIRE_passe_toujours(live):
    """⚠️ La moitié qui garantit qu'on n'a pas cassé le geste courant. Sur le tableau
    des cinq cents, 500 lignes portent une relique et leurs agents écrivent dans la
    couche imbriquée chaque jour — la garde ne doit pas les arrêter."""
    st, ns, rid = _ligne_avec_relique()
    st.update_row(ns, rid, {"contact1_nom": {"link": "https://nouveau"}})   # ne lève pas


def test_QUAND_LES_DEUX_COEXISTENT_la_lecture_sert_la_RELIQUE(live):
    """⚠️ Fait mesuré en écrivant ce banc, et personne ne l'avait vu : une fois les
    deux formes présentes, la lecture à plat rend la RELIQUE et masque la couche qu'on
    vient d'écrire. Les deux produisent le même nom (`contact1_nom.link`), et c'est la
    clé littérale qui gagne.

    Ce banc ne dit pas que c'est bien — il FIGE ce qui est, pour qu'une migration
    sache ce qu'elle manipule. Écrire l'imbriqué sur une ligne qui porte la relique ne
    change donc rien à ce qu'un lecteur voit : la valeur neuve est invisible tant que
    la relique est là. C'est la raison de l'ordre imposé à la réimbrication — écrire,
    PUIS purger le nom littéral — et la preuve qu'on ne peut pas s'arrêter au milieu."""
    st, ns, rid = _ligne_avec_relique()
    st.update_row(ns, rid, {"contact1_nom": {"link": "https://nouveau"}})

    assert st.list_rows(ns)[0]["contact1_nom.link"] == "https://profil", (
        "la relique masque la couche fraîchement écrite")


def test_un_effacement_SANS_relique_passe(live):
    """La garde ne doit mordre que là où le danger existe — sinon elle empêcherait
    d'effacer une couche normale."""
    from oto_mcp import db

    ns = str(db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6]))
    st = _store()
    st.set_schema(ns, {"key": "siren", "fields": [
        {"key": "siren", "type": "text"}, {"key": "c", "type": "text"}]})
    rid = st.append_row(ns, {"siren": "1", "c": {"valeur": "v", "link": "L"}})["_id"]
    st.update_row(ns, rid, {"c": {"link": "@empty"}})   # ne lève pas


# ── une relique VIDE n'est pas une relique (09/09/2026) ──────────────────────

def test_une_relique_VIDE_ne_fait_pas_refuser_un_effacement_qui_marche():
    """⚠️ La garde regardait le NOM, jamais la valeur. Or l'immense majorité des
    reliques du parc sont des coquilles : mesuré le 09/09/2026, **735 des 764** clés
    littérales pointées valent `None` — 500 `qualification.comment` et 235
    `retraitement.comment` sur un seul tableau.

    Refuser là est un faux refus sur 96 % de ce que la garde rencontre : il n'y a rien
    à détruire, donc rien à protéger, et l'effacement de la couche aurait abouti. Pire,
    le refus envoie vers `data_drop_column`, qui frappe la colonne sur TOUTES les
    lignes du tableau — 500 lignes d'une campagne vivante pour effacer une couche sur
    une seule."""
    avant = {"qualification": {"valeur": "x", "comment": "utile"},
             "qualification.comment": None}
    assert rq.effacements_sur_relique({"qualification": {"comment": None}}, avant) == []


def test_le_marqueur_d_effacement_delibere_ne_compte_pas_non_plus():
    """`@empty` EST le geste qui vide : une relique qui le porte ne contient rien."""
    avant = {"qualification": {"valeur": "x"}, "qualification.comment": "@empty"}
    assert rq.effacements_sur_relique({"qualification": {"comment": None}}, avant) == []


def test_la_relique_qui_porte_VRAIMENT_une_donnee_refuse_toujours():
    """⚠️ La moitié qui garantit qu'on n'a pas désarmé la garde. 29 reliques du parc
    portent une valeur, et sur 23 d'entre elles c'est la SEULE copie de la donnée."""
    avant = {"qualification": {"valeur": "x"}, "qualification.comment": "la donnée"}
    assert rq.effacements_sur_relique(
        {"qualification": {"comment": None}}, avant) == ["qualification.comment"]


# ── une relique VIDE ne masque pas la couche EN LECTURE (09/09/2026) ─────────

def _sert(data: dict) -> dict:
    from oto_mcp.datastore.core import DatastorePg
    return DatastorePg._row_to_dict(
        {"row_id": "r", "created_at": None, "updated_at": None, "data": data})


def test_une_relique_VIDE_ne_masque_plus_la_couche_renseignee():
    """⚠️ Le coût RÉEL des reliques, et il n'avait jamais été mesuré : une clé
    littérale porte exactement le nom que `flat_layers` fabrique pour la couche. Les
    deux atterrissent sur la même clé de la ligne servie, et en JSONB les clés sont
    triées par LONGUEUR — `qualification` est donc parcourue avant
    `qualification.comment`, et la relique écrase toujours.

    Mesuré sur la production : **732 valeurs invisibles à leurs lecteurs** sur un
    tableau de campagne (497 `qualification.comment`, 235 `retraitement.comment`), ces
    dernières portant des traces de retrait pour conformité. Le texte est en base,
    servi `None`. **Une trace qu'on ne peut pas lire ne prouve rien.**"""
    servie = _sert({"qualification": {"valeur": "CEDIA", "comment": "texte utile"},
                    "qualification.comment": None})
    assert servie["qualification.comment"] == "texte utile"


def test_une_relique_qui_PORTE_une_valeur_gagne_toujours():
    """Le comportement figé par `test_QUAND_LES_DEUX_COEXISTENT…` ne bouge pas : sur
    23 cellules du parc la relique est la seule à porter la donnée, et la masquer la
    perdrait. On ne corrige que l'écrasement de quelque chose par RIEN."""
    servie = _sert({"qualification": {"valeur": "x", "comment": "couche"},
                    "qualification.comment": "relique"})
    assert servie["qualification.comment"] == "relique"

    seule = _sert({"qualification": {"valeur": "x"},
                   "qualification.comment": "la seule donnée"})
    assert seule["qualification.comment"] == "la seule donnée"


def test_sans_relique_la_lecture_ne_change_pas():
    """La correction ne coûte rien là où le danger n'existe pas — et le pré-calcul
    qu'elle demande ne se fait même pas : aucune clé pointée dans la ligne."""
    assert _sert({"qualification": {"valeur": "x", "comment": "couche"}}
                 )["qualification.comment"] == "couche"


# ── la primitive, sur ses deux formes et son silence ─────────────────────────

def test_les_deux_formes_d_ecriture_sont_vues():
    avant = {"contact1_nom.link": "https://x"}
    assert rq.effacements_sur_relique({"contact1_nom": {"link": "@empty"}}, avant)
    assert rq.effacements_sur_relique({"contact1_nom.link": None}, avant)


def test_le_chemin_NOMINAL_ne_paie_rien():
    """Aucune relique sur la ligne : la garde sort avant de parcourir le payload."""
    assert rq.effacements_sur_relique({"a": {"comment": "@empty"}}, {"a": "x"}) == []
    assert rq.effacements_sur_relique({"a": None}, None) == []
