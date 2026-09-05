"""Retirer une clé dit ce que ça casse — au seul moment où quelqu'un écoute (oto#59).

Le 03/09/2026, une clé a disparu d'une org. Une douzaine de passages programmés ont
tourné à l'aveugle pendant **36 heures**. Personne n'avait rien fait de mal : rien ne
signalait que des agents programmés en dépendaient.

⚠️ Et le canal qui aurait annoncé la panne tournait sur le credential tombé : six fois
par jour, un run découvrait qu'il était cassé, l'inscrivait sur une ligne que personne
ne regardait, et se taisait — **correctement, selon ses propres règles**. La panne était
silencieuse *par construction*.

Ce lot ne répare pas l'alerte hors bande (elle reste à faire). Il ferme le trajet le
moins cher : **quand quelqu'un est là, on lui parle.**
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities import me_credentials as MC


class _Ctx:
    sub = "u1"
    org_id = 7
    role = "member"


def _entree(**kw):
    return MC.CredentialClearInput(provider="slack", **kw)


@pytest.fixture()
def retrait(monkeypatch):
    """Le retrait réussit toujours ; seul l'avertissement varie."""
    monkeypatch.setattr(MC.credentials_store, "clear_credential",
                        lambda *a, **k: True)
    monkeypatch.setattr(MC, "_org_of", lambda sub: 7)
    monkeypatch.setattr(MC, "_scoped_entity", lambda ctx, scope, org: ("user", "u1"))
    monkeypatch.setattr(MC.providers, "connector_for_provider", lambda p: object())
    monkeypatch.setattr(MC.providers, "is_byo_user", lambda p: True)
    monkeypatch.setattr(MC.providers, "is_org_shareable", lambda p: True)


def test_les_agents_programmes_qui_en_dependent_sont_NOMMES(retrait, monkeypatch):
    monkeypatch.setattr(MC.db, "triggers_actifs_utilisant", lambda org, c: [
        {"label": "veille du matin", "procedure": "p1"},
        {"label": None, "procedure": "digest-client"},
    ])
    out = MC._clear(_Ctx(), _entree())
    assert out["ok"] is True, "le retrait a lieu : on informe, on n'empêche pas"
    w = out["warning"]
    assert w and "veille du matin" in w and "digest-client" in w
    assert "2 agent" in w


def test_le_retrait_reste_permis_meme_quand_il_casse_tout(retrait, monkeypatch):
    """Retirer sa propre clé est un droit, pas une demande d'autorisation."""
    monkeypatch.setattr(MC.db, "triggers_actifs_utilisant",
                        lambda org, c: [{"label": "x", "procedure": "p"}])
    assert MC._clear(_Ctx(), _entree())["ok"] is True


def test_sans_dependance_aucun_bruit(retrait, monkeypatch):
    """Un avertissement qu'on reçoit toujours cesse d'être lu."""
    monkeypatch.setattr(MC.db, "triggers_actifs_utilisant", lambda org, c: [])
    assert MC._clear(_Ctx(), _entree())["warning"] is None


def test_une_lecture_qui_echoue_ne_casse_PAS_le_retrait(retrait, monkeypatch):
    """Best-effort : l'avertissement est un service rendu, pas une garde. Le retrait
    est déjà fait quand on le calcule — le faire échouer laisserait la clé retirée et
    l'appelant devant une erreur."""
    def _boum(org, c):
        raise RuntimeError("base indisponible")
    monkeypatch.setattr(MC.db, "triggers_actifs_utilisant", _boum)
    out = MC._clear(_Ctx(), _entree())
    assert out["ok"] is True and out.get("warning") is None


def test_le_champ_est_declare_dans_le_modele_servi():
    """Un champ absent du modèle serait filtré à la sérialisation : l'avertissement
    serait calculé et personne ne le verrait."""
    assert "warning" in MC.CredentialCleared.model_fields


def test_la_lecture_ne_voit_que_les_dependances_DECLAREES(monkeypatch):
    """⚠️ Elle sous-estime, et le banc l'écrit pour que personne ne lise son résultat
    comme un inventaire : un agent programmé qui dérive ses outils de sa procédure
    dépend du connecteur sans le déclarer dans `tools`."""
    from oto_mcp.db import runner_triggers as R
    monkeypatch.setattr(R, "list_triggers", lambda org: [
        {"enabled": True, "tools": ["slack_post_message"], "label": "déclaré"},
        {"enabled": True, "tools": [], "label": "dérivé de sa procédure"},
        {"enabled": False, "tools": ["slack_post_message"], "label": "coupé"},
    ])
    vus = R.triggers_actifs_utilisant(7, "slack")
    assert [t["label"] for t in vus] == ["déclaré"], (
        "seuls les déclencheurs ACTIFS aux outils déclarés sont vus — un déclencheur "
        "coupé n'a rien à casser, un déclencheur dérivé n'est pas visible ici")
