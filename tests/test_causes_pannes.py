"""L'empreinte d'une panne d'agent et la copie d'une cause au client (`causes_pannes.py`).

Ce qu'on prouve : deux occurrences d'une même erreur — identifiants, dates, nombres,
adresses et texte cité différents — ont la même empreinte ; une élision française n'est
pas prise pour une citation ; deux erreurs de causes différentes restent distinctes ; et
une copie qui porte un nom d'outil, un code ou une adresse est refusée.
"""
from __future__ import annotations

import re

import pytest

from oto_mcp import causes_pannes as C


@pytest.mark.parametrize("a, b", [
    ("HTTP 529 overloaded_error req_011CXaaaa1 at 2026-11-09T08:51:38Z",
     "HTTP 529 overloaded_error req_022DYbbbb2 at 2026-11-10T09:00:01Z"),
    ("outil 'serper_search' absent du travail 4512", "outil 'web_read' absent du travail 7"),
    ("invalid x-api-key 0a1b2c3d4e5f6a7b8c9d", "invalid x-api-key ffee00112233445566"),
    ("POST https://api.exemple.test/v1/leads?id=12 : 403",
     "POST https://api.autre.test/x : 403"),
    ("compte jane@exemple.test refusé", "compte bob@autre.test refusé"),
    ("Unit sandbox-run-uaa80697e26c9be24d030.service was already loaded",
     "Unit sandbox-run-u913b4c62cb21d16ed719.service was already loaded"),
    ("Aucun worker ne sert la famille `mistral` (aucun worker vu)",
     "Aucun worker ne sert la famille `anthropic` (aucun worker vu)"),
    ("Run  too   big:\n 812 rows", "run too big: 9 rows"),
])
def test_deux_occurrences_d_une_meme_erreur_ont_la_meme_empreinte(a, b):
    assert C.de(a) == C.de(b)
    assert C.cle(C.de(a)) == C.cle(C.de(b))


def test_une_elision_n_est_pas_une_citation():
    assert C.de("L'organisation n'a pas déposé sa clé de modèle") == \
        "l'organisation n'a pas déposé sa clé de modèle"


def test_deux_causes_differentes_restent_distinctes():
    assert C.de("HTTP 529 overloaded_error") != C.de("HTTP 401 invalid x-api-key")
    assert C.de("message_too_large") != C.de("run too big")


def test_un_motif_vide_n_a_pas_d_empreinte():
    assert C.de(None) is None and C.de("   ") is None


def test_l_empreinte_est_bornee_et_sa_cle_courte():
    e = C.de("x " * 500)
    assert len(e) <= C.LONGUEUR_MAX and len(C.cle(e)) == 16


@pytest.mark.parametrize("texte, refus", [
    ("La clé de l'outil d'emailing a été refusée.", None),
    ("{marque} a relancé l'agent après une panne de son côté.", None),
    ("Erreur sur serper_search.", "nom d'outil"),
    ("Le fournisseur a répondu 401.", "code"),
    ("Ouvrez https://exemple.test pour la refaire.", "adresse"),
    ("Lancez `oto_trigger`.", "outil"),
    ("Bonjour {prenom}.", "variable"),
])
def test_la_copie_au_client_refuse_ce_qu_il_n_a_pas_a_lire(texte, refus):
    r = C.copie_refusee(texte)
    assert (r is None) if refus is None else (refus in r)


# --- Ce que l'empreinte ne garde JAMAIS : le catalogue est commun à toutes les orgs. ---

#: Des motifs qui portent ce qu'un client a de propre, et ce qui ne doit en rester.
_MOTIFS_D_UN_CLIENT = [
    ("refusé pour l'org 4512 et le compte 88231", ["4512", "88231"]),
    ("jane.doe@exemple-client.test n'a pas accès", ["jane", "doe", "exemple"]),
    ("GET https://crm.exemple-client.test/api/v2/deals/991 : 404",
     ["crm", "exemple", "deals", "991"]),
    ("hôte api.exemple-client.fr injoignable", ["api.", "exemple", ".fr"]),
    ("fichier /srv/exemple/clients/liste.csv illisible", ["srv", "clients", "liste", "csv"]),
    ("ligne rec7Hk2Lm9Pq absente de la table", ["rec7hk2lm9pq"]),
    ("objet AB12 introuvable, id=77", ["ab12", "77"]),
    ("tableau tbl_x9 verrouillé", ["tbl_x9"]),
    ("le canal de Dupont Immobilier est archivé", ["dupont", "immobilier"]),
    ("Error: Martin Durand refused the call", ["martin", "durand"]),
    ("page « Rapport annuel Exemple » introuvable", ["rapport", "annuel", "exemple"]),
    ("slug exemple-client-42 inconnu", ["exemple", "42"]),
    ("travail du 2026-11-09 08:51 relancé", ["2026", "08:51"]),
]


@pytest.mark.parametrize("motif, absents", _MOTIFS_D_UN_CLIENT)
def test_l_empreinte_ne_garde_ni_adresse_ni_identifiant_ni_nom(motif, absents):
    e = C.de(motif)
    for absent in absents:
        assert absent not in e, (motif, e)


_JETON_PERMIS = re.compile(
    r"^[^\w<>]*(?:<(?:url|email|id|date|texte|n|nom)>"
    r"|[a-zà-ÿœæç]+(?:['’_-][a-zà-ÿœæç]+)*)?[^\w<>]*$")


@pytest.mark.parametrize("motif", [m for m, _ in _MOTIFS_D_UN_CLIENT] + [
    "HTTP 529 overloaded_error req_011CXaaaa1 at 2026-11-09T08:51:38Z",
    "Unit sandbox-run-uaa80697e26c9be24d030.service was already loaded",
    "OpenRouter: x-api-key invalide (clé AbC123dEf)",
])
def test_l_empreinte_n_a_que_la_forme_de_la_liste_blanche(motif):
    """La garantie de FORME, quel que soit le motif : aucun chiffre, aucun point ni barre
    à l'intérieur d'un mot, aucun `@` — seulement des repères, des mots en lettres et de
    la ponctuation autour."""
    e = C.de(motif)
    assert not re.search(r"\d|@", e), e
    for jeton in e.split():
        assert _JETON_PERMIS.match(jeton), (jeton, e)


def test_une_majuscule_hors_tete_de_phrase_est_un_nom():
    assert C.de("Le compte Dupont est fermé") == "le compte <nom> est fermé"
    assert C.de("Fermé. Le compte est clos") == "fermé. le compte est clos"
