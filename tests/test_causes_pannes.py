"""L'empreinte d'une panne d'agent et la copie d'une cause au client (`causes_pannes.py`).

Ce qu'on prouve : deux occurrences d'une même erreur — identifiants, dates, nombres,
adresses et texte cité différents — ont la même empreinte ; une élision française n'est
pas prise pour une citation ; deux erreurs de causes différentes restent distinctes ; et
une copie qui porte un nom d'outil, un code ou une adresse est refusée.
"""
from __future__ import annotations

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
