"""Un filtre `in` à liste vide est REFUSÉ, il ne rend plus tout (oto-backend#353).

⚠️ Le défaut : `if not vals: return None, []` — la clause DISPARAISSAIT, et la requête
rendait alors **tout le tableau** au lieu de rien. C'est l'inverse exact de ce que
`IN ()` veut dire, et ça a moissonné un namespace entier les 15-16/08/2026.

⚠️ **Pourquoi refuser plutôt que rendre zéro ligne**, alors que zéro serait la
sémantique SQL juste — arbitré par Alexis le 05/09 : une liste vide est presque
toujours un accident de l'appelant (une variable non remplie, une liste filtrée à zéro
juste avant). Rendre zéro le laisserait conclure « cette donnée n'existe pas », ce qui
est faux et silencieux. Refuser l'arrête là où il peut encore corriger.

⚠️ Le refus vit dans les DEUX branches — colonne système et colonne de données — parce
que le défaut y vivait deux fois. Une seule corrigée aurait laissé la porte ouverte sur
l'autre, et rien ne l'aurait dit.
"""
from __future__ import annotations

import pytest

from oto_mcp.db.query import _ds_one_field_clause


@pytest.mark.parametrize("champ", ["statut", "_id"])
@pytest.mark.parametrize("vide", [[], [None], [""], ["", None]])
def test_une_liste_sans_valeur_utilisable_est_REFUSEE(champ, vide):
    """Les deux branches, et les quatre façons d'être vide : une liste de `None` ou de
    chaînes vides est filtrée à zéro par le nettoyage — donc vide, elle aussi."""
    with pytest.raises(ValueError) as e:
        _ds_one_field_clause(champ, "in", vide)
    assert champ in str(e.value)


@pytest.mark.parametrize("champ", ["statut", "_id"])
def test_le_refus_NOMME_ce_qu_il_a_recu_et_la_sortie(champ):
    """⚠️ Ce qui fait perdre du temps ici, c'est de soupçonner ses DONNÉES au lieu de
    sa variable. Le refus doit donc montrer la liste reçue — et nommer le geste pour
    qui voulait vraiment les lignes sans valeur."""
    with pytest.raises(ValueError) as e:
        _ds_one_field_clause(champ, "in", [])
    message = str(e.value)
    assert "[]" in message, "la liste reçue n'est pas montrée"
    assert "empty" in message, "la sortie pour « les lignes sans valeur » n'est pas dite"
    assert "TOUT" in message


@pytest.mark.parametrize("champ", ["statut", "_id"])
def test_une_liste_PLEINE_passe_toujours(champ):
    """L'autre moitié : un refus qui mangerait les appels légitimes serait pire que le
    défaut qu'il corrige."""
    clause, params = _ds_one_field_clause(champ, "in", ["a", "b"])
    assert clause and "ANY" in clause
    assert ["a", "b"] in params


@pytest.mark.parametrize("champ", ["statut", "_id"])
def test_une_valeur_SEULE_non_listee_passe(champ):
    """`in` accepte aussi une valeur nue — elle ne doit pas être prise pour une liste
    vide."""
    clause, _ = _ds_one_field_clause(champ, "in", "a")
    assert clause and "ANY" in clause
