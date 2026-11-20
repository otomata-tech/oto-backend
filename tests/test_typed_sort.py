"""#336 — le tri honore le TYPE déclaré au schéma, ou ce n'est pas un tri.

Mesuré avant ce lot : le tri croissant d'un champ number rendait `10, 100, 2, 9`
(rangement de chaînes), une échelle d'effectifs mettait « 200 et plus » au milieu,
et 89 % des énumérations en production servaient un ordre faux — invisible parce
que l'écran est plein et groupé, donc l'ordre a l'air délibéré. Le FILTRE, lui,
castait déjà : le même champ répondait juste à une question et faux à l'autre.

Avec le type déclaré, on trie pour de vrai : numériquement un number, par rang
d'option un enum — et les dates ISO trient juste par l'alphabet, ce qu'un test
FIGE (c'est une chance, pas une conception). Deux décisions rendues avant
d'écrire, testées ici :

  ① une valeur NON CONFORME (hors type, hors options) va en QUEUE quel que soit
    le sens du tri, en bloc alphabétique — et la réponse le DIT (compteur
    `order_health`, compté sur le jeu filtré ENTIER, jamais sur la page) ;
  ② une case VIDE (champ absent, NULL, chaîne vide) est un cas distinct : TOUT
    AU BOUT, après les non conformes, compteur séparé.

Un champ text ou non déclaré garde l'ordre historique à l'identique — figé ici
aussi, pour que le lot ne change que ce qu'il annonce.

Contre un vrai PostgreSQL : le banc de tri stubbé a déjà menti une fois (il
vérifiait quel chemin de code est pris, jamais que la requête s'exécute —
cf. le commentaire dans `datastore_list_rows`).
"""
from __future__ import annotations

import uuid

import pytest


def _monte(schema_fields, rows):
    """Un datastore neuf, son schéma, ses lignes — rend (store, datastore)."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store

    st = make_store("sub-test")
    ns = str(db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6]))

    # ⚠️ Les lignes AVANT le schéma, et l'ordre est le sujet même de ce banc.
    # Depuis le 08/09/2026, un `type` déclaré s'arme lui-même : `{"ca": "abc"}` sur une
    # colonne `number` est REFUSÉ à l'écriture. Or ce banc éprouve le TRI de valeurs non
    # conformes — un cas qui existe pour de bon (118 valeurs mesurées dans le parc). Il
    # ne peut donc plus les poser par la porte d'écriture, et c'est cohérent : ces
    # valeurs-là sont entrées quand le type n'était pas encore armé. Poser les lignes
    # d'abord reproduit exactement cette histoire, au lieu de désarmer le contrôle.
    # Une colonne non déclarée étant refusée (oto#124), elles s'écrivent sous un
    # schéma qui les déclare SANS type ; le format du banc se pose ensuite, et une
    # colonne qu'il ne déclare pas redevient une colonne non déclarée des données.
    colonnes = sorted({k for r in rows for k in r})
    st.set_schema(ns, {"fields": [{"key": k} for k in colonnes]})
    for r in rows:
        st.append_row(ns, r)
    st.set_schema(ns, {"fields": schema_fields or []})
    return st, ns


def _valeurs(page, col):
    return [r.get(col) for r in page["rows"]]


# ── number : le cœur de la requalification ───────────────────────────────────

def test_un_nombre_se_trie_numeriquement(live):
    st, ns = _monte([{"key": "ca", "type": "number"}],
                    [{"ca": "10"}, {"ca": "100"}, {"ca": 2}, {"ca": "9"}])
    page = st.page_rows(ns, order_by="ca", order_dir="asc")
    assert [str(v) for v in _valeurs(page, "ca")] == ["2", "9", "10", "100"], \
        "le rangement de chaînes rendait 10, 100, 2, 9 (#336)"
    page = st.page_rows(ns, order_by="ca", order_dir="desc")
    assert [str(v) for v in _valeurs(page, "ca")] == ["100", "10", "9", "2"]


def test_hors_type_en_queue_dans_les_deux_sens_et_compte(live):
    st, ns = _monte([{"key": "ca", "type": "number"}],
                    [{"ca": "abc"}, {"ca": "10"}, {"ca": "2"}])
    asc = st.page_rows(ns, order_by="ca", order_dir="asc")
    assert _valeurs(asc, "ca") == ["2", "10", "abc"]
    desc = st.page_rows(ns, order_by="ca", order_dir="desc")
    assert _valeurs(desc, "ca") == ["10", "2", "abc"], \
        "le rebut reste en queue même en DESC — on lit la tête de liste"
    assert asc["order_health"] == {"off_type": 1, "empty": 0}
    # …et la requête n'a pas échoué : le cast est gardé, jamais subi.


def test_les_vides_tout_au_bout_apres_les_non_conformes(live):
    st, ns = _monte([{"key": "ca", "type": "number"}],
                    [{"ca": ""}, {"ca": "5"}, {"autre": "x"}, {"ca": "zut"}])
    page = st.page_rows(ns, order_by="ca", order_dir="asc")
    assert _valeurs(page, "ca") == ["5", "zut", "", None], \
        "conformes, puis non conformes, puis vides — trois blocs, dans cet ordre"
    assert page["order_health"] == {"off_type": 1, "empty": 2}


# ── enum : le défaut d'origine ───────────────────────────────────────────────

def test_une_enum_se_trie_dans_lordre_declare(live):
    st, ns = _monte(
        [{"key": "etat", "type": "enum",
          "options": ["a_traiter", "en_cours", "cloturee"]}],
        [{"etat": "cloturee"}, {"etat": "a_traiter"}, {"etat": "en_cours"}])
    page = st.page_rows(ns, order_by="etat", order_dir="asc")
    assert _valeurs(page, "etat") == ["a_traiter", "en_cours", "cloturee"], \
        "l'ordre déclaré EST le sens — l'alphabet rendait a_traiter, cloturee, en_cours"


def test_hors_options_en_queue_et_compte(live):
    # Le cas réel des 504 lignes : « Oui » posé sur une énumération « oui »/« non ».
    st, ns = _monte(
        [{"key": "ok", "type": "enum", "options": ["oui", "non", "inconnu"]}],
        [{"ok": "Oui"}, {"ok": "non"}, {"ok": "oui"}, {"ok": "inconnu"}])
    page = st.page_rows(ns, order_by="ok", order_dir="asc")
    assert _valeurs(page, "ok") == ["oui", "non", "inconnu", "Oui"], \
        "« Oui » ne se range pas en silence : bloc distinct, et le compteur le dit"
    assert page["order_health"] == {"off_type": 1, "empty": 0}


# ── date : juste par chance, figé par test ───────────────────────────────────

def test_les_dates_iso_trient_chronologiquement_et_les_vides_en_queue(live):
    st, ns = _monte([{"key": "cloture", "type": "date"}],
                    [{"cloture": "2026-02-01"}, {"cloture": "2025-12-31"},
                     {"cloture": ""}, {"cloture": "2026-01-15"}])
    page = st.page_rows(ns, order_by="cloture", order_dir="asc")
    assert _valeurs(page, "cloture") == \
        ["2025-12-31", "2026-01-15", "2026-02-01", ""], \
        "ISO trie juste par l'alphabet — une CHANCE que ce test transforme en contrat"
    assert page["order_health"] == {"off_type": 0, "empty": 1}


# ── ce qui ne change PAS ─────────────────────────────────────────────────────

def test_un_texte_garde_lordre_historique_a_lidentique(live):
    st, ns = _monte([{"key": "nom", "type": "text"}],
                    [{"nom": "b"}, {"nom": ""}, {"nom": "a"}])
    page = st.page_rows(ns, order_by="nom", order_dir="asc")
    assert _valeurs(page, "nom") == ["", "a", "b"], \
        "text = comportement historique intact (chaîne vide en tête en ASC)"
    assert "order_health" not in page


def test_une_colonne_non_declaree_garde_lordre_historique(live):
    st, ns = _monte(None, [{"v": "10"}, {"v": "2"}])
    page = st.page_rows(ns, order_by="v", order_dir="asc")
    assert _valeurs(page, "v") == ["10", "2"], \
        "sans type déclaré, rien ne change — le lot ne devine pas"
    assert "order_health" not in page


# ── le compteur dit la vérité du JEU, pas de la page ─────────────────────────

def test_le_compteur_compte_le_jeu_filtre_entier(live):
    st, ns = _monte([{"key": "ca", "type": "number"}],
                    [{"ca": "3"}, {"ca": "1"}, {"ca": "abc"}, {"ca": "xyz"}])
    page = st.page_rows(ns, order_by="ca", order_dir="asc", limit=1)
    assert len(page["rows"]) == 1
    assert page["order_health"] == {"off_type": 2, "empty": 0}, \
        "compté comme `total` : sur le jeu filtré entier, sinon il ment dès la page 2"


def test_tout_conforme_pas_de_compteur(live):
    st, ns = _monte([{"key": "ca", "type": "number"}],
                    [{"ca": "3"}, {"ca": "1"}])
    page = st.page_rows(ns, order_by="ca", order_dir="asc")
    assert "order_health" not in page, \
        "prévenir là où tout est conforme ferait cesser de lire l'avertissement"


# ── la face MCP rend le même ordre — jamais juste sur une face seulement ─────

def test_la_face_mcp_rend_le_meme_ordre_et_le_compteur(live):
    st, ns = _monte([{"key": "ca", "type": "number"}],
                    [{"ca": "10"}, {"ca": "2"}, {"ca": "abc"}])
    page = st.cursor_rows(ns, order_by="ca", order_dir="asc")
    assert _valeurs(page, "ca") == ["2", "10", "abc"]
    assert page["order_health"] == {"off_type": 1, "empty": 0}
