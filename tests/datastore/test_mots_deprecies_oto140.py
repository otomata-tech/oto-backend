"""`@keep` et `@clear` retirés : l'écriture est REFUSÉE, et le refus nomme le geste (oto#140).

Le contrat d'une case tient en deux gestes — `null` efface, `@empty` dit « cherché,
rien ». `@clear` doublait `null`, `@keep` doublait l'omission : une écriture qui les
porte est refusée entière, sans date ni préavis. Sans ce refus, `"@keep"` serait stocké
comme une valeur littérale. Le refus lui-même : `test_regle_finale_j2_j3.py`.
"""
from __future__ import annotations

import inspect
import pathlib
import uuid

import pytest

from oto_mcp.datastore import couches as dsl
from oto_mcp.datastore import mots_deprecies as mdp
from oto_mcp.datastore.errors import RowValidationError


# ── ce que le refus VOIT ─────────────────────────────────────────────────────

def test_les_deux_mots_sont_vus_nus_en_couche_et_dans_une_liste():
    assert mdp.mots_nommes({
        "a": dsl.EFFACEMENT,
        "b": {"valeur": "x", "comment": dsl.GARDE},
        "c": [{"nom": "Alice", "fonction": dsl.GARDE}],
    }) == {dsl.EFFACEMENT: ["a"], dsl.GARDE: ["b", "c"]}


def test_ni_null_ni_empty_ni_une_sous_chaine_ne_declenchent_rien():
    """`null` et `@empty` sont LES deux gestes : les refuser enseignerait le contraire.
    Le mot ne mord qu'entier — `contact@keepcalm.example` n'est pas un `@keep`."""
    assert mdp.mots_nommes({
        "a": None, "b": {"valeur": None}, "c": dsl.VIDE_DELIBERE,
        "d": "contact@keepcalm.example", "e": "@keep ; vu au registre", "f": [],
    }) == {}


# ── ce que le texte DIT ──────────────────────────────────────────────────────

def test_le_refus_dit_le_remplacement_de_chaque_mot_sans_date():
    with pytest.raises(RowValidationError) as e:
        mdp.controler({"a": dsl.EFFACEMENT, "b": {"comment": dsl.GARDE}})
    t = str(e.value)
    assert "`@clear` (`a`)" in t and "`null`" in t
    assert "`@keep` (`b`)" in t and "leave the sub-field out" in t
    assert "`@empty`" in t and "searched, nothing" in t
    assert "octobre" not in t and "déprécié" not in t and "October" not in t and "deprecated" not in t


def test_la_description_de_data_write_porte_la_regle():
    from oto_mcp.tools import datastore as tools_ds

    src = inspect.getsource(tools_ds)
    assert "<<mots_deprecies>>" in src, "la description porte la marque, pas un texte recopié"
    assert "REFUSED whole" in mdp.DESCRIPTION_ECRITURE
    assert "2026" not in mdp.DESCRIPTION_ECRITURE


def test_le_guide_dit_le_refus_au_present():
    guide = pathlib.Path(__file__).parents[2] / "oto_mcp/guides/datastore-semantics.md"
    assert "`@keep` and `@clear` are not accepted" in guide.read_text()


def test_le_texte_servi_ne_prescrit_plus_les_mots_deprecies_dans_les_refus():
    """Un refus prescrit `null` pour effacer et `@empty` pour « cherché, rien »."""
    from oto_mcp.datastore import columns

    src = inspect.getsource(columns)
    assert "comme `@clear` et `@empty` — EFFACE" not in src
    assert "(vide sans rien affirmer)" not in src


def test_le_refus_est_pose_sur_les_quatre_chemins_d_ecriture():
    from oto_mcp import upload_tokens
    from oto_mcp.datastore import ecriture, ecriture_par_id, lots

    for mod in (ecriture_par_id, lots, upload_tokens):
        assert "mdp.controler(" in inspect.getsource(mod), mod.__name__
    assert "mdp.controler(" in inspect.getsource(ecriture.EcritureMixin.append_row)


# ── sur une base réelle ──────────────────────────────────────────────────────

SUB = "usr_mots_deprecies_140"


@pytest.fixture(scope="module")
def store(live):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store

    db.upsert_user(SUB, email=f"{SUB}@deprecies.invalid", name=SUB)
    ns = str(db.create_datastore("user", SUB, "tdep-" + uuid.uuid4().hex[:6]))
    st = make_store(SUB)
    st.set_schema(ns, {"fields": [{"key": "nom", "type": "text"},
                                  {"key": "site", "type": "text"}]})
    return st, ns


def test_keep_n_est_jamais_stocke(store):
    """Le refus est ce qui empêche `"@keep"` d'entrer comme une valeur littérale."""
    from oto_mcp import db

    st, ns = store
    rid = st.append_row(ns, {"nom": "ACME",
                             "site": {"valeur": "acme.test", "comment": "registre"}})["_id"]
    with pytest.raises(RowValidationError, match=r"`@keep` \(`site`\)"):
        st.update_row(ns, rid, {"site": dsl.GARDE})
    with pytest.raises(RowValidationError, match=r"`@keep` \(`nom`\)"):
        st.append_row(ns, {"nom": dsl.GARDE})
    cell = db.datastore_get_row(st._resolve(ns), rid)["data"]["site"]
    assert cell["valeur"] == "acme.test" and cell["comment"] == "registre"


def test_null_efface_sans_refus(store):
    """L'annulation de la fin de `null` : il efface, et rien ne l'annonce comme un geste
    en sursis."""
    st, ns = store
    rid = st.append_row(ns, {"nom": "ACME", "site": "acme.test"})["_id"]
    st.off_notices.clear()
    out = st.update_row(ns, rid, {"site": None})
    assert not out.get("site"), out
    assert not any("null" in n and "REFUSÉ" in n for n in st.off_notices), st.off_notices
