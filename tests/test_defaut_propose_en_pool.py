"""Une org en POOL se voit proposer son modèle d'ABONNEMENT par défaut.

Le 25/11/2026, un client en pool (sept membres prêtant leur compte Claude) a
découvert des crédits de la plateforme dépensés par deux agents : ils avaient été
posés sur `claude-sonnet-5-5`, le défaut proposé — la voie « clé », payée en crédits
faute de clé d'org —, alors que l'équipe croyait tout faire tourner sur ses comptes.

Ce que ces bancs tiennent :

1. **en pool, avec un prêteur, le chemin ouvert** : le défaut est `sub:sonnet` ;
2. **partout ailleurs, rien ne change** : mode personnel ou non choisi, pool vide,
   chemin fermé au demandeur, famille d'abonnement non servie, lecture sans
   personne (la garde `model_is_default`) ;
3. **la capacité** passe bien le demandeur : `op=list` propose l'abonnement en pool.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp.capabilities import _modele
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import ResolvedCtx
from oto_mcp.db import org_subscription_pool

_ETAT = {"armed": True, "workers": 2, "last_seen": None,
         "families": ["anthropic", "claude_subscription"]}


def _defaut(modeles):
    defauts = [m["id"] for m in modeles if m["default"]]
    assert len(defauts) <= 1, "au plus un défaut"
    return defauts[0] if defauts else None


@pytest.fixture
def org(monkeypatch):
    """Une org qui a sa clé (rien d'écarté), aucun modèle éteint ; mode, taille du pool
    et chemin ouvert se règlent par banc."""
    reglage = {"mode": org_subscription_pool.POOL, "taille": 1, "ouvert": True}
    monkeypatch.setattr(_modele._cle_exigee, "manquantes", lambda *a, **k: [])
    monkeypatch.setattr(_modele.db.modeles_eteints, "eteints", lambda org_id: ())
    monkeypatch.setattr(_modele.org_subscription_pool, "mode_regle",
                        lambda org_id, famille: reglage["mode"])
    monkeypatch.setattr(_modele.org_subscription_pool, "taille_du_pool_a_la_pose",
                        lambda org_id, famille: reglage["taille"])
    monkeypatch.setattr(_modele._abonnement, "ouvert", lambda sub: reglage["ouvert"])
    return reglage


# ── 1. le pool ────────────────────────────────────────────────────────────────

def test_en_pool_avec_un_preteur_le_defaut_est_l_abonnement(org):
    """⚠️ LE banc du lot."""
    modeles = _modele.etat_servi(dict(_ETAT), 42, "yann")["models"]
    assert _defaut(modeles) == "sub:sonnet"
    servis = {m["id"]: m["served"] for m in modeles}
    assert servis["claude-sonnet-5-5"] is True, "la voie clé reste servie et choisissable"


def test_un_preteur_au_plafond_compte_comme_a_la_pose(org):
    """La taille lue est celle de la POSE : un plafond tombe à son échéance."""
    org["taille"] = 1   # `taille_du_pool_a_la_pose` compte le prêteur au plafond
    assert _defaut(_modele.etat_servi(dict(_ETAT), 42, "yann")["models"]) == "sub:sonnet"


# ── 2. partout ailleurs, rien ne change ──────────────────────────────────────

@pytest.mark.parametrize("mode", [org_subscription_pool.PERSONNEL, None])
def test_hors_pool_le_defaut_reste_la_cle(org, mode):
    org["mode"] = mode
    assert _defaut(_modele.etat_servi(dict(_ETAT), 42, "yann")["models"]) == "claude-sonnet-5-5"


def test_un_pool_vide_ne_propose_pas_l_abonnement(org):
    """La pose le refuserait (`subscription_pool_empty`)."""
    org["taille"] = 0
    assert _defaut(_modele.etat_servi(dict(_ETAT), 42, "yann")["models"]) == "claude-sonnet-5-5"


def test_un_chemin_ferme_au_demandeur_ne_propose_pas_l_abonnement(org):
    """La pose le refuserait (`subscription_not_enabled`)."""
    org["ouvert"] = False
    assert _defaut(_modele.etat_servi(dict(_ETAT), 42, "yann")["models"]) == "claude-sonnet-5-5"


def test_sans_personne_le_pool_n_est_pas_lu(org, monkeypatch):
    """La garde `model_is_default` (modèles éteints) lit sans personne : rien ne change."""
    monkeypatch.setattr(_modele.org_subscription_pool, "mode_regle",
                        lambda *a: pytest.fail("pool lu sans personne"))
    assert _defaut(_modele.etat_servi(dict(_ETAT), 42)["models"]) == "claude-sonnet-5-5"


def test_sans_abonnement_servi_le_pool_n_est_pas_lu(org, monkeypatch):
    monkeypatch.setattr(_modele.org_subscription_pool, "mode_regle",
                        lambda *a: pytest.fail("pool lu sans famille d'abonnement servie"))
    etat = {**_ETAT, "families": ["anthropic"]}
    assert _defaut(_modele.etat_servi(etat, 42, "yann")["models"]) == "claude-sonnet-5-5"


def test_sans_org_nommee_le_pool_n_est_pas_lu(monkeypatch):
    monkeypatch.setattr(_modele.org_subscription_pool, "mode_regle",
                        lambda *a: pytest.fail("pool lu sans org"))
    assert _defaut(_modele.etat_servi(dict(_ETAT), None, "yann")["models"]) == "claude-sonnet-5-5"


# ── 3. la capacité ────────────────────────────────────────────────────────────

def test_la_liste_d_une_org_en_pool_propose_l_abonnement(org, monkeypatch):
    """⚠️ Réclamé par une épreuve de chute : sans `ctx.sub` passé à `etat_servi`,
    les bancs du dessus restent verts et l'écran propose toujours la clé."""
    monkeypatch.setattr(RT.db, "list_triggers", lambda org_id: [])
    monkeypatch.setattr(RT.db, "runner_arme", lambda org_id: dict(_ETAT))
    out = asyncio.run(RT._triggers(ResolvedCtx(sub="yann", org_id=42),
                                   RT.TriggerInput(op="list")))
    assert _defaut(out["runner"]["models"]) == "sub:sonnet"
