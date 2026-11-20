"""Une colonne à cycle de vie reste ÉCRIVABLE quand elle porte des couches (#586).

**Le cas qui l'impose, et il a arrêté une campagne à zéro fiche sur cent.** Le
29/08/2026 à 19:20, un tableau de production déclare `origine: "system"` sur toutes
ses colonnes, la colonne d'état comprise. À chaque écriture, dans cet ordre :

1. la plateforme **pose elle-même** `<champ>.origine` = la valeur d'avant sur les
   colonnes `origine: "system"` que le geste vient de modifier ;
2. elle **valide** la ligne ainsi complétée.

La colonne d'état change à chaque fiche — c'est tout l'objet du travail. Le contrôle
de cycle de vie lisait alors la colonne **brute** et voyait
`{'valeur': 'enrichi', 'origine': 'a_enrichir'}` là où il attend un mot de
l'énumération. **Refus.** Sept en trois minutes, deux travaux, zéro ligne écrite.

> **La plateforme enveloppe, puis se refuse elle-même.**

⚠️ **Le défaut n'est pas le cran, c'est l'asymétrie de lecture** : trois lignes plus
haut, la pose d'origine déballe la colonne ; le contrôle d'état, non. *Deux gestes
voisins qui lisent la même colonne doivent la lire pareil* — les contrôles de champ
(`required_when`, borne, motif, énumération) déballaient déjà, chacun après un défaut
du même genre (#329, #347).

Ce banc tourne sur une **vraie base**, par le chemin servi : un tableau `role: status`
avec son cycle de vie et le cran d'origine sur la colonne d'état, et la seule question
qui vaille — **la ligne a-t-elle changé d'état dans la base ?**
"""
from __future__ import annotations

import uuid

import pytest


# Le tableau de l'incident, réduit à ce qui porte la règle : une colonne d'état avec
# son cycle de vie, ET le cran d'origine posé dessus — c'est leur RENCONTRE qui casse.
# ⚠️ Le cran `origine: "system"` n'est plus lu depuis le 08/09/2026 et il est refusé
# depuis le 01/10/2026 : l'origine se déclare par l'appel qui apporte la donnée.
SCHEMA = {
    "key": "siren",
    "fields": [
        {"key": "siren", "type": "text"},
        {"key": "raison_sociale", "type": "text"},
        {"key": "statut", "role": "status", "type": "enum",
         "options": ["a_enrichir", "en_cours", "enrichi", "echec"],
         "lifecycle": {"states": ["a_enrichir", "en_cours", "enrichi", "echec"],
                       "terminal": ["enrichi", "echec"],
                       "transitions": {"a_enrichir": ["en_cours", "enrichi", "echec"],
                                       "en_cours": ["enrichi", "echec"]}}},
    ],
}


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store("sub-test")


@pytest.fixture
def table(live):
    from oto_mcp import db
    ns_id = db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    st = _store()
    st.set_schema(ns, SCHEMA)
    row = st.append_row(ns, {"siren": "552032534", "raison_sociale": "TEMOIN",
                             "statut": "a_enrichir"})
    return st, ns, ns_id, row["_id"]


def _donnees(ns_id: int, row_id: str) -> dict:
    """Ce que porte LA BASE — jamais ce que l'appel a bien voulu rendre."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute(
            "SELECT data FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id)).fetchone()
    return dict((r or {}).get("data") or {})


# ── Le témoin de l'incident ──────────────────────────────────────────────────

def test_une_transition_INTERDITE_reste_refusee_sur_une_colonne_a_couches(table):
    """Le correctif ne doit pas désarmer la garde : elle se juge sur la VALEUR.

    Sans ce cas, « lire la valeur » pourrait se traduire par « ne plus rien lire » —
    et un cycle de vie qui n'interdit plus rien est pire qu'un cycle de vie absent,
    parce qu'il a l'air de protéger."""
    from oto_mcp.datastore.core import RowValidationError
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"statut": "enrichi"})       # pose la couche d'origine

    with pytest.raises(RowValidationError) as e:
        st.update_row(ns, rid, {"statut": "en_cours"})  # terminal → en_cours : interdit

    assert "transition" in str(e.value)
    from oto_mcp.datastore.schema import unwrap
    assert unwrap(_donnees(ns_id, rid)["statut"]) == "enrichi", "rien n'a bougé"


def test_un_etat_INCONNU_reste_refuse_sur_une_colonne_a_couches(table):
    from oto_mcp.datastore.core import RowValidationError
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"statut": "en_cours"})      # pose la couche d'origine

    with pytest.raises(RowValidationError) as e:
        st.update_row(ns, rid, {"statut": "termine"})   # n'existe pas

    assert "unknown state" in str(e.value)


def test_l_etat_PRECEDENT_se_lit_aussi_deballe(table):
    """L'autre lecture brute, et elle mord un cran plus tard : une fois la couche
    posée, l'état d'avant lu brut ferait comparer un objet à un mot — donc juger la
    transition contre `{'valeur': …}`. La ligne portait déjà des couches : c'est le
    cas NORMAL dès la deuxième écriture."""
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"statut": "en_cours"})      # 1ʳᵉ : pose la couche
    st.update_row(ns, rid, {"statut": "enrichi"})       # 2ᵉ : transition autorisée

    from oto_mcp.datastore.schema import unwrap
    assert unwrap(_donnees(ns_id, rid)["statut"]) == "enrichi"


# ── Les deux AUTRES lectures brutes de la même colonne ───────────────────────
# Trouvées en cherchant la classe plutôt que le cas : la règle est « deux gestes
# voisins qui lisent la même colonne doivent la lire pareil », pas « corriger celui
# qui a mordu ». Toutes deux SILENCIEUSES — elles ne refusent rien, elles se trompent.

def test_l_etat_TERMINAL_se_reconnait_sous_ses_couches():
    """Sinon l'avertissement « la libération automatique est retirée » ne part plus :
    l'agent écrit son verdict, garde sa ligne, et personne ne le lui dit."""
    from oto_mcp.datastore.schema import is_terminal_status
    schema = SCHEMA
    assert is_terminal_status(schema, "enrichi") is True
    assert is_terminal_status(schema, {"valeur": "enrichi",
                                       "origine": "a_enrichir"}) is True
    assert is_terminal_status(schema, {"valeur": "en_cours"}) is False
    assert is_terminal_status(schema, None) is False


def test_le_JOURNAL_enregistre_la_valeur_et_pas_la_colonne():
    """Sinon le cockpit affiche `{'valeur': 'enrichi', …}` comme état de la fiche, et
    les transitions du journal deviennent illisibles — sans qu'aucun refus ne le dise."""
    from oto_mcp.datastore import journal
    ctx = journal.NsContext(ns_id=1, name="t", status_key="statut",
                            title_key="raison_sociale")
    assert journal.status_of({"statut": "enrichi"}, ctx) == "enrichi"
    assert journal.status_of({"statut": {"valeur": "enrichi",
                                         "origine": "a_enrichir"}}, ctx) == "enrichi"
