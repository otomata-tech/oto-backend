"""Les lentilles paginées bornent leur `limit` — écrêter, pas refuser (#300).

Six capacités acceptaient un `limit` sans aucune contrainte : une valeur énorme
partait telle quelle au SQL, une valeur négative faisait échouer la requête en 500.
La plus exposée est l'export d'audit, parce que c'est justement l'endroit où un grand
nombre paraît légitime.

Le choix : **écrêter** comme le fait déjà la recherche, plutôt que refuser — le client
qui demande trop reçoit le maximum servable, pas une erreur à apprendre à éviter.

⚠️ Le plafond de chaque lentille est son DÉFAUT ACTUEL : ces tests figent donc aussi
le fait que le comportement nominal n'a pas bougé.
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities import audit_log, monitoring, org_monitoring, usage
from oto_mcp.capabilities._types import cap_limit


# (modèle, kwargs requis, plafond)
LENTILLES = [
    # Le plafond du store, partagé (usage signal 1553) : la lentille d'org passe par
    # `CallsInput`, qui écrêtait à 200 en silence sous l'org qui annonçait plus.
    (monitoring.CallsInput, {}, 1000),
    (org_monitoring.OrgCallsInput, {"org_id": 1}, 1000),
    (org_monitoring.OrgRunsInput, {"org_id": 1}, 500),
    (usage.RunsInput, {}, 100),
    (usage.SignalsInput, {}, 200),
    (audit_log.AuditExportInput, {"org_id": 1}, 1000),
]


@pytest.mark.parametrize("model,base,cap", LENTILLES)
def test_une_valeur_enorme_est_ecretee(model, base, cap):
    """Sans ça, le nombre part au SQL tel quel."""
    assert model(**base, limit=10**9).limit == cap


@pytest.mark.parametrize("model,base,cap", LENTILLES)
def test_une_valeur_negative_ne_casse_plus_la_requete(model, base, cap):
    """Un `limit` négatif faisait échouer la requête en 500 — un refus d'entrée
    déguisé en panne serveur."""
    assert model(**base, limit=-5).limit == 1


#: Les lentilles dont le défaut est PLUS BAS que le plafond : la liste se pagine par
#: curseur, le défaut reste la page nominale d'avant (usage signal 1553).
DEFAUTS = {monitoring.CallsInput: 200, org_monitoring.OrgCallsInput: 200,
           org_monitoring.OrgRunsInput: 100}


@pytest.mark.parametrize("model,base,cap", LENTILLES)
def test_le_defaut_est_inchange(model, base, cap):
    """Le défaut n'a pas bougé : borner ne devait rien changer au nominal. C'est le
    plafond, sauf pour les listes à curseur, dont le plafond a été relevé."""
    assert model(**base).limit == DEFAUTS.get(model, cap)


@pytest.mark.parametrize("model,base,cap", LENTILLES)
def test_une_valeur_raisonnable_passe_intacte(model, base, cap):
    """Écrêter ne veut pas dire tout ramener au plafond."""
    assert model(**base, limit=7).limit == 7


def test_les_consoles_op_aware_gardent_leur_absence_de_valeur():
    """Leur `limit` est optionnel parce qu'il dépend du verbe (un export ne se pagine
    pas comme une timeline) : `None` doit rester `None`, sinon on écrase le défaut
    que le handler choisit selon l'op."""
    assert monitoring.MonitoringInput(op="calls").limit is None
    assert org_monitoring.OrgMonitoringInput(op="export", org_id=1).limit is None


def test_les_consoles_op_aware_ecretent_au_plus_large_de_leurs_ops():
    """Borner une console au plus PETIT de ses ops écrêterait un export légitime."""
    assert monitoring.MonitoringInput(op="calls", limit=10**6).limit == 200
    assert org_monitoring.OrgMonitoringInput(op="export", org_id=1, limit=10**6).limit == 1000


def test_le_helper_absorbe_une_valeur_illisible():
    """Le helper est partagé : une entrée non numérique ne doit pas remonter en 500
    depuis un validateur."""
    assert cap_limit("beaucoup", 50) == 50
    assert cap_limit(None, 50, default=10) == 10
    assert cap_limit(None, 50) == 50
