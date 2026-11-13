"""Ne pas PROMETTRE une exécution que personne n'assure — `op=launch`.

`launch` posait `armed` même si aucun worker n'était joignable pour le prendre :
un succès silencieux qui ne fait jamais rien. Mesuré (oto-runner#13, 07/09/2026) :
41 travaux restés en file 13 jours chez un partenaire, aucune unité `oto-fleet*`
sur la box. Le symptôme lu depuis le produit était « l'ordonnanceur est mort » —
un diagnostic faux posé sur une cause invisible, puisque l'armement, lui,
réussissait.

Même garde que `runner.triggers` (`_modele.exige_un_runner`, partagée depuis ce
lot) : la garde suit le VERBE — armer refuse sans runner, `stop` reste ouvert
(un passage mort doit pouvoir être rangé)."""
from __future__ import annotations

import pytest

from oto_mcp.capabilities import runner_fleets as RF
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


@pytest.fixture(autouse=True)
def _cle_de_modele_non_exigee(monkeypatch):
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **k: None)
    # Ni routée vers la ferme : sa garde de pose a son banc (`test_cle_de_modele_exigee`).
    monkeypatch.setattr("oto_mcp.capabilities._cle_exigee.org_routee_ferme",
                        lambda org_id: False)


def _ctx(sub="alexis", org_id=2):
    return ResolvedCtx(sub=sub, org_id=org_id)


def _appel(ctx, **kw):
    return RF._fleets(ctx, RF.FleetInput(**kw))


def _sans_worker(monkeypatch, last_seen=None):
    monkeypatch.setattr(RF.db, "runner_arme", lambda org: {
        "armed": False, "workers": 0, "last_seen": last_seen, "families": ["anthropic"]})


def _avec_worker(monkeypatch):
    monkeypatch.setattr(RF.db, "runner_arme", lambda org: {
        "armed": True, "workers": 1, "last_seen": "2026-09-17 08:00:00",
        "families": ["anthropic"]})


def test_launch_est_refuse_quand_aucun_worker_nest_joignable(monkeypatch):
    from oto_mcp import roles
    monkeypatch.setattr(roles, "is_org_admin", lambda *a, **k: True)
    monkeypatch.setattr(RF, "_run_courant", lambda: None)
    monkeypatch.setattr(RF.db, "get_fleet", lambda *a, **k: {"model": "claude-sonnet-5", 
        "id": 1, "status": "draft", "procedure": "p", "input": "x"})
    _sans_worker(monkeypatch)

    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="launch", fleet_id=1)
    assert (e.value.status, e.value.code) == (400, "no_runner_armed")
    # Le message dit l'EFFET, jamais la machine (oto#222) : ni runner ni worker…
    assert "rien n'exécute les automatisations" in e.value.message
    assert "runner" not in e.value.message.lower()
    assert "worker" not in e.value.message.lower()
    # …et ce qui RESTE ouvert, sur les deux surfaces qui partagent ce refus.
    assert "se supprime" in e.value.message
    assert "s'arrête (`stop`)" in e.value.message


def test_le_refus_narme_rien(monkeypatch):
    """Un refus n'écrit rien — `db.armer` ne doit même pas être appelé."""
    from oto_mcp import roles
    monkeypatch.setattr(roles, "is_org_admin", lambda *a, **k: True)
    monkeypatch.setattr(RF, "_run_courant", lambda: None)
    monkeypatch.setattr(RF.db, "get_fleet", lambda *a, **k: {"model": "claude-sonnet-5", 
        "id": 1, "status": "draft", "procedure": "p", "input": "x"})
    _sans_worker(monkeypatch)
    appele = {}
    monkeypatch.setattr(RF.db, "armer", lambda *a, **k: appele.setdefault("oui", True))

    with pytest.raises(AuthzDenied):
        _appel(_ctx(), op="launch", fleet_id=1)
    assert "oui" not in appele


def test_launch_passe_quand_un_worker_est_joignable(monkeypatch):
    from oto_mcp import roles
    monkeypatch.setattr(roles, "is_org_admin", lambda *a, **k: True)
    monkeypatch.setattr(RF, "_run_courant", lambda: None)
    monkeypatch.setattr(RF.db, "get_fleet", lambda *a, **k: {"model": "claude-sonnet-5", 
        "id": 1, "status": "draft", "procedure": "p", "input": "x"})
    monkeypatch.setattr(RF.db, "armer", lambda *a, **k: {
        "id": 1, "max_rows": None, "max_tokens_per_row": None})
    _avec_worker(monkeypatch)

    out = _appel(_ctx(), op="launch", fleet_id=1)
    assert out["fleet"]["id"] == 1


def test_stop_reste_ouvert_sans_worker(monkeypatch):
    """LA garantie du lot : un passage mort doit pouvoir être arrêté — le
    refuser enfermerait l'utilisateur avec un objet qui lui ment, sans même
    lui laisser le geste qui range.

    Un VRAI appel `op=stop`, et `db.runner_arme` n'est PAS doublé : il reste
    la fonction servie. Le magasin est rendu injoignable (ni pool ni
    `DATABASE_URL`) — aucun worker ne peut y être lu, donc aucun n'est présent.
    Si `stop` consultait la présence d'un runner, l'appel lèverait ici au lieu
    d'arrêter."""
    from oto_mcp.db import _conn
    monkeypatch.setattr(_conn, "_pool", None)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(RF, "_run_courant", lambda: None)
    demande = {}

    def _demander_arret(fleet_id, org_id, raison):
        demande.update(fleet_id=fleet_id, org_id=org_id, raison=raison)
        return {"id": fleet_id, "status": "stopping", "stop_reason": raison}

    monkeypatch.setattr(RF.db, "demander_arret", _demander_arret)
    # Le droit par objet lit la ligne, pas la présence d'un runner : c'est la sienne.
    monkeypatch.setattr(RF.db, "get_fleet", lambda fid, org: {"id": fid, "sub": "alexis"})

    out = _appel(_ctx(), op="stop", fleet_id=1, reason="plus aucun runner")
    assert out["fleet"]["status"] == "stopping"
    assert demande == {"fleet_id": 1, "org_id": 2, "raison": "plus aucun runner"}


def test_la_garde_partage_la_meme_fonction_que_les_declencheurs():
    """DRY attendu par ce lot : une seule définition du motif, partagée par
    `runner.triggers` et `runner.fleets` — pas deux copies qui divergent."""
    from oto_mcp.capabilities import _modele, runner_triggers
    assert hasattr(_modele, "exige_un_runner")
    assert not hasattr(runner_triggers, "_exige_un_runner")
