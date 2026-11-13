"""Le modèle PROPOSÉ par défaut à une org est un modèle qu'elle peut faire tourner.

Le 14/09/2026, le premier worker Anthropic « clés clients seules » a fait de
`claude-sonnet-5` — premier du catalogue d'alors — le défaut proposé à TOUTES les orgs, alors
qu'aucune n'avait déposé de clé Anthropic et que la plateforme l'exigeait. Un agent
qui suivait la proposition se faisait refuser `model_key_required` à la pose : le
catalogue proposait exactement ce que la garde refusait.

Ce que ces bancs tiennent :

1. **le catalogue** écarte du défaut les familles sans clé, sans les rendre « non
   servies » — l'org peut déposer sa clé et choisir le modèle ;
2. **l'état servi** ne lit la clé que pour une org nommée et des familles servies ;
3. **la capacité** passe bien l'org : sans clé déposée le défaut est Mistral, avec
   la clé il redevient Claude — la même lecture que la garde de pose.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import runner_models
from oto_mcp.capabilities import _modele
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import ResolvedCtx


def _defaut(modeles):
    defauts = [m["id"] for m in modeles if m["default"]]
    assert len(defauts) <= 1, "au plus un défaut"
    return defauts[0] if defauts else None


# ── 1. le catalogue ───────────────────────────────────────────────────────────

def test_une_famille_sans_cle_n_est_PAS_proposee_mais_reste_servie():
    """⚠️ LE banc du lot. Claude est servi et en tête du catalogue ; l'org n'a pas
    sa clé : le défaut est le premier modèle qu'elle peut faire tourner."""
    modeles = runner_models.catalogue(["anthropic", "mistral"], ["anthropic"])
    assert _defaut(modeles) == "mistral-large-2512"
    servis = {m["id"]: m["served"] for m in modeles}
    assert servis["claude-sonnet-5-5"] is True, (
        "toujours servi : l'org peut déposer sa clé et le choisir")


def test_sans_famille_ecartee_le_defaut_d_avant_est_intact():
    assert _defaut(runner_models.catalogue(["anthropic", "mistral"])) == "claude-sonnet-5-5"
    assert _defaut(runner_models.catalogue(["anthropic", "mistral"], [])) == "claude-sonnet-5-5"


def test_toutes_les_familles_servies_ecartees_ne_proposent_RIEN():
    """Jamais de repli sur un modèle que la pose refuserait."""
    assert _defaut(runner_models.catalogue(["anthropic"], ["anthropic"])) is None


def test_ecarter_une_famille_non_servie_ne_change_rien():
    assert _defaut(runner_models.catalogue(["mistral"], ["anthropic"])) == "mistral-large-2512"


# ── 2. l'état servi ───────────────────────────────────────────────────────────

_ETAT = {"armed": True, "workers": 1, "last_seen": None,
         "families": ["anthropic", "mistral"]}


def test_l_etat_d_une_org_SANS_cle_propose_mistral(monkeypatch):
    vus = []
    monkeypatch.setattr(_modele._cle_exigee, "manquantes",
                        lambda org, familles, routee=None:
                        vus.append((org, list(familles))) or ["anthropic"])
    assert _defaut(_modele.etat_servi(dict(_ETAT), 226)["models"]) == "mistral-large-2512"
    assert vus == [(226, ["anthropic", "mistral"])], "la clé se lit pour CETTE org et ses familles servies"


def test_sans_org_nommee_la_cle_n_est_pas_lue(monkeypatch):
    monkeypatch.setattr(_modele._cle_exigee, "manquantes",
                        lambda *a, **k: pytest.fail("clé lue sans org"))
    assert _defaut(_modele.etat_servi(dict(_ETAT))["models"]) == "claude-sonnet-5-5"


def test_sans_famille_servie_la_cle_n_est_pas_lue(monkeypatch):
    """La lecture du réglage est froide : un état vide n'a rien à écarter."""
    monkeypatch.setattr(_modele._cle_exigee, "manquantes",
                        lambda *a, **k: pytest.fail("clé lue sans famille servie"))
    etat = _modele.etat_servi({"armed": True, "workers": 1, "last_seen": None,
                               "families": []}, 226)
    assert _defaut(etat["models"]) is None


# ── 3. la capacité — la vraie lecture de la garde, doublée à la base ─────────

@pytest.fixture
def plateforme(monkeypatch):
    """Anthropic exigé au niveau plateforme ; la présence de la clé se règle par banc."""
    monkeypatch.setattr(
        "oto_mcp.db.connector_settings.get_connector_setting",
        lambda portee, ident, fournisseur, cle:
            "true" if (portee, fournisseur) == ("platform", "anthropic") else None)
    monkeypatch.setattr(RT.db, "list_triggers", lambda org: [])
    monkeypatch.setattr(RT.db, "runner_arme", lambda org: dict(_ETAT))
    deposees = set()
    monkeypatch.setattr("oto_mcp.credentials_store.has_credential",
                        lambda portee, ident, fournisseur, account="":
                            (ident, fournisseur) in deposees)
    return deposees


def _liste(org_id):
    return asyncio.run(RT._triggers(ResolvedCtx(sub="alexis", org_id=org_id),
                        RT.TriggerInput(op="list")))


def test_la_liste_d_une_org_SANS_cle_anthropic_propose_mistral(plateforme):
    """⚠️ Réclamé par une épreuve de chute : sans `ctx.org_id` passé à `etat_servi`,
    les bancs du dessus restent verts et l'écran propose toujours Claude."""
    assert _defaut(_liste(226)["runner"]["models"]) == "mistral-large-2512"


def test_la_liste_d_une_org_AVEC_sa_cle_propose_claude(plateforme):
    plateforme.add(("2", "anthropic"))
    assert _defaut(_liste(2)["runner"]["models"]) == "claude-sonnet-5-5"
