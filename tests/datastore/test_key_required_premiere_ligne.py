"""Un tableau FERMÉ (`key_required`) et la ligne qui doit NAÎTRE (#516, suite, #668).

Le cran fait ce que #516 a voulu : sur un tableau fermé, une écriture qui ne désigne
aucune ligne existante est refusée. Ce banc ne conteste pas ce comportement — il
tient les deux bouts que le refus laissait tomber, et que le journal a datés :

**01/09, run `493e624c…`** — la procédure de triage mail écrit son lot de 47 lignes,
se fait refuser, LIT le schéma, et trouve seule la sortie : `key_required=false`,
le lot, `key_required=true`. Les 47 lignes du tableau sont nées de ce contournement.

**02/09, run `a2da6c1e…`** — le même geste, un autre jour : les trois formes
d'écriture (clé inédite, lot, ligne sans clé) sont refusées, l'agent ne retrouve PAS
la manœuvre de la veille, et dépose un signal. 19 messages non journalisés.

Ce que le refus disait : « vise-la par son identifiant ». Sur un tableau fermé où la
ligne n'existe pas — a fortiori VIDE — cette sortie ne mène nulle part : il n'y a
aucun `_id` à viser, et le seul geste qui débloque est un geste de SCHÉMA. Un refus
qui nomme une seule sortie, impraticable dans le cas qui l'a déclenché, laisse à
l'agent le soin de deviner l'autre — ce que l'un a fait et l'autre pas.

⚠️ Le régime ne bouge pas : rien ici ne crée de ligne sur un tableau fermé. On exige
seulement que le refus NOMME le geste qui le lève, comme `RowLocked` nomme la façon
de libérer un bail (#317).
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.datastore.errors import BusinessKeyRequired


# Le tableau du signal : une clé métier `gmail_message_id`, et le cran posé.
FERME = {
    "key": "gmail_message_id",
    "new_rows": "reject",
    "fields": [{"key": "gmail_message_id", "type": "text"},
               {"key": "subject", "type": "text"},
               {"key": "decision", "type": "text"}],
}
# La ligne du 02/09 que le run n'a pas pu journaliser.
INEDIT = {"gmail_message_id": "1a062eeaf8cba958", "subject": "Re: Accès API",
          "decision": "skip"}


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store("sub-test")


@pytest.fixture
def ferme(live):
    """Un tableau fermé et VIDE — l'état d'un tableau de journalisation qui débute."""
    from oto_mcp import db
    ns = str(db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6]))
    st = _store()
    pose = st.set_schema(ns, FERME)
    assert "new_rows" in pose["enforced"]      # le cran est bien appliqué ICI
    return st, ns


def _sortie_de_creation(message: str) -> bool:
    """Le refus dit-il comment faire NAÎTRE la ligne ?

    Pas « contient un mot » : les deux moitiés du geste doivent y être — l'outil de
    schéma, et le cran qu'il faut lever. L'une sans l'autre renvoie l'agent chercher
    dans une description qu'il n'a pas sous les yeux."""
    return "data_patch_schema" in message and 'new_rows="create"' in message


# --- Les trois gestes du signal #668, sur un tableau fermé ---------------------

def test_ligne_seule_cle_inedite(ferme):
    """Geste 1 — `data_write(row=…)` avec une valeur de clé que personne ne porte."""
    st, ns = ferme
    with pytest.raises(BusinessKeyRequired) as exc:
        st.append_row(ns, dict(INEDIT))
    assert _sortie_de_creation(str(exc.value)), str(exc.value)


def test_lot_cle_inedite(ferme):
    """Geste 2 — le même contenu en lot (`rows=`), le chemin des imports."""
    st, ns = ferme
    with pytest.raises(BusinessKeyRequired) as exc:
        st.write_rows(ns, [dict(INEDIT)], key="gmail_message_id")
    assert _sortie_de_creation(str(exc.value)), str(exc.value)


def test_ligne_sans_la_cle(ferme):
    """Geste 3 — la ligne ne porte pas la clé du tout (l'autre forme du refus)."""
    st, ns = ferme
    with pytest.raises(BusinessKeyRequired) as exc:
        st.append_row(ns, {"subject": "sans clé", "decision": "skip"})
    assert _sortie_de_creation(str(exc.value)), str(exc.value)


# --- Ce qui rend l'autre sortie impraticable ----------------------------------

def test_le_tableau_ferme_et_vide_n_a_aucun_id_a_viser(ferme):
    """La sortie que le refus nommait — `data_write(id=…)` — n'a ici aucune cible.

    C'est le cœur du signal : sur un tableau fermé qui n'a pas encore de ligne, le
    conseil « vise-la par son identifiant » est vrai et inapplicable. Le banc le
    prouve plutôt que de le supposer : le tableau est vide, et il le reste après les
    trois refus (aucun d'eux ne crée quoi que ce soit)."""
    st, ns = ferme
    assert st.list_rows(ns) == []
    for geste in (lambda: st.append_row(ns, dict(INEDIT)),
                  lambda: st.write_rows(ns, [dict(INEDIT)], key="gmail_message_id"),
                  lambda: st.append_row(ns, {"subject": "sans clé"})):
        with pytest.raises(BusinessKeyRequired):
            geste()
    assert st.list_rows(ns) == []          # rien n'est né, comme annoncé


def test_le_geste_nomme_par_le_refus_debloque_vraiment(ferme):
    """Rejeu de la manœuvre du 01/09 — celle qui a produit les 47 lignes.

    Le refus peut désormais la nommer parce qu'elle MARCHE : ouvrir par le schéma,
    écrire, refermer. Sans ce test, on documenterait une sortie sans l'avoir prise."""
    st, ns = ferme
    with pytest.raises(BusinessKeyRequired):
        st.append_row(ns, dict(INEDIT))
    st.patch_schema(ns, new_rows="create")
    ligne = st.append_row(ns, dict(INEDIT))
    assert ligne["gmail_message_id"] == INEDIT["gmail_message_id"]
    st.patch_schema(ns, new_rows="reject")
    # Refermé : la ligne SUIVANTE est de nouveau refusée, et celle qui vient de
    # naître se réécrit — le cran retrouve exactement son office.
    with pytest.raises(BusinessKeyRequired):
        st.append_row(ns, {"gmail_message_id": "1a062eeaf8cba959", "subject": "autre"})
    relu = st.append_row(ns, {"gmail_message_id": INEDIT["gmail_message_id"],
                              "decision": "propose_update"})
    assert relu["_id"] == ligne["_id"] and relu["decision"] == "propose_update"


# --- La description SERVIE, celle que l'agent relit à chaque appel -------------

def test_la_description_de_data_write_annonce_le_cran():
    """`data_write` promettait la création sur clé inédite, sans réserve.

    C'est la phrase que le signal #668 cite pour dire que la plateforme se contredit
    (« MERGES onto that row » seulement si la clé existe déjà). Elle est exacte SAUF
    sur un tableau fermé, et c'est le seul endroit où un agent qui écrit peut
    l'apprendre : une description d'outil est relue à chaque appel, la doc interne
    jamais."""
    import asyncio

    from fastmcp import FastMCP

    from oto_mcp.tools import datastore as D

    m = FastMCP("x")
    D.register(m)
    outil = [t for t in asyncio.run(m._list_tools()) if t.name == "data_write"][0]
    assert "new_rows" in outil.description, outil.description
