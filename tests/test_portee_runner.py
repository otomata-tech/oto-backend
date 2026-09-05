"""Un jeton porté `runner` ouvre la file, et rien d'autre.

⚠️ Mesuré le 05/09/2026 par la session infra, depuis la machine : le jeton qui
faisait tourner les workers ouvrait `/api/admin/users`,
`/api/admin/platform-keys` et `/api/admin/monitoring/summary` — en 200. Non pas
parce qu'on le voulait, mais **parce que rien ne les fermait** : un jeton sans
portée garde les pleins pouvoirs de son compte.

« Ce jeton ne FAIT que sonder la file » décrivait l'usage qu'en avait le runner.
« Ne PEUT que sonder » était faux. C'est cette distinction que la portée rend
vraie.

⚠️ La granularité s'arrête à la file : les routes du runner portent l'opération
dans le CORPS (le même chemin réserve, prolonge, conclut, enfile), là où les
autres familles nomment leur ressource dans le CHEMIN. Descendre plus bas
demanderait de faire monter l'opération dans l'URL — un changement de contrat
public, écarté ici.
"""
from __future__ import annotations

import pytest

from oto_mcp.auth import token_scopes as ts


@pytest.fixture
def _porte():
    return ts.parse({"runner": True})


# ── le critère d'acceptation, mot pour mot ───────────────────────────────────

@pytest.mark.parametrize("route", [
    "/api/admin/users",
    "/api/admin/platform-keys",
    "/api/admin/monitoring/summary",
])
def test_les_routes_d_administration_sont_FERMEES(_porte, route):
    """La mesure de l'infra, rejouée à l'envers : ces trois-là répondaient 200."""
    assert ts.authorize(_porte, "GET", route) is False


def test_ce_qui_n_est_pas_nomme_reste_refuse(_porte):
    """La propriété qui vaut plus que l'entrée : deny-by-default. Une route
    ajoutée demain est refusée sans que personne n'ait à y penser."""
    for m, r in [("GET", "/api/me"), ("POST", "/api/me/projects"),
                 ("GET", "/api/me/tokens"), ("GET", "/api/connectors"),
                 ("POST", "/api/me/runner/jobs/quelque-chose-de-neuf")]:
        assert ts.authorize(_porte, m, r) is False, f"{m} {r} n'est pas fermé"


# ── et ce sans quoi un worker s'arrête ───────────────────────────────────────

@pytest.mark.parametrize("route", [
    "/api/me/runner/jobs",     # réserver, prolonger le bail, conclure, lier
    "/api/me/runner/fleets",   # déclarer, armer, prendre, battre
    "/api/me/runs/thread",     # le fil : sans lui, pas de reprise après une mort
])
def test_les_trois_routes_du_worker_passent(_porte, route):
    assert ts.authorize(_porte, "POST", route) is True


def test_la_methode_compte_aussi(_porte):
    """Ouvrir un chemin n'ouvre pas ses autres verbes."""
    assert ts.authorize(_porte, "GET", "/api/me/runner/jobs") is False
    assert ts.authorize(_porte, "DELETE", "/api/me/runner/fleets") is False


# ── la saisie de la portée ───────────────────────────────────────────────────

def test_runner_est_un_booleen_STRICT():
    """`"true"`, `1` ou `{}` passeraient un `if` complaisant et donneraient une
    portée que l'émetteur n'a pas écrite."""
    for valeur in ("true", 1, {}, [], "oui"):
        with pytest.raises(ts.ScopeError):
            ts.parse({"runner": valeur})


def test_runner_seul_suffit_a_faire_une_portee():
    assert ts.parse({"runner": True}) == {"runner": True}


def test_il_se_combine_avec_des_tableaux(_porte):
    """Un ordonnanceur compte des lignes : il porte `runner` ET ses tableaux.
    L'un n'annule pas l'autre."""
    p = ts.parse({"runner": True, "namespaces": {"edition-vivier": "read"}})
    assert ts.authorize(p, "POST", "/api/me/runner/jobs") is True
    assert ts.authorize(p, "GET", "/api/datastore/namespaces/edition-vivier/rows") is True
    assert ts.authorize(p, "GET", "/api/datastore/namespaces/autre/rows") is False
    assert ts.authorize(p, "GET", "/api/admin/users") is False


def test_un_jeton_NON_porte_garde_ses_pleins_pouvoirs():
    """L'état d'aujourd'hui, et la raison du lot : sans portée, tout passe —
    y compris l'administration. Ce banc dit ce qu'on quitte."""
    assert ts.authorize(None, "GET", "/api/admin/platform-keys") is True
