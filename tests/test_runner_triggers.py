"""Déclencheurs du runner — le fuseau, la cadence plancher, et le tick qui enfile.

Trois familles : la validation à la pose (un cron d'arrosage est refusé, un fuseau
inconnu aussi, et le message NOMME le fautif), le calcul d'échéance dans le fuseau
déclaré (l'heure d'été ne décale rien en silence), et le tick — qui n'enfile que
s'il GAGNE le compare-and-swap (prod et preprod partagent la base : deux ticks,
un gagnant par échéance).
"""
from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import pytest

from oto_mcp import runner_tick
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


def _ctx(sub="alexis", org_id=2):
    return ResolvedCtx(sub=sub, org_id=org_id)


def _appel(ctx, **kw):
    return RT._triggers(ctx, RT.TriggerInput(**kw))


# ── la validation nomme le fautif ─────────────────────────────────────────────

def test_un_cron_darrosage_est_refuse():
    with pytest.raises(ValueError) as e:
        runner_tick.validate_cron("* * * * *", "Europe/Paris")
    assert "cadence trop serrée" in str(e.value)


def test_un_fuseau_inconnu_est_refuse_et_nomme():
    with pytest.raises(ValueError) as e:
        runner_tick.validate_cron("5 6 * * *", "Paris/France")
    assert "Paris/France" in str(e.value)


def test_une_expression_invalide_est_refusee_et_nommee():
    with pytest.raises(ValueError) as e:
        runner_tick.validate_cron("99 99 * * *", "Europe/Paris")
    assert "99 99" in str(e.value)


# ── l'échéance vit dans le fuseau déclaré ─────────────────────────────────────

def test_lecheance_sevalue_dans_le_fuseau_du_declencheur():
    """8h05 Europe/Paris un jour d'été = 6h05 UTC ; le même cron en UTC = 8h05 UTC.
    Si ces deux-là coïncidaient, le fuseau serait décoratif — et l'heure d'été
    décalerait les veilles en silence."""
    apres = datetime.datetime(2026, 8, 14, 0, 0, tzinfo=datetime.timezone.utc)
    paris = runner_tick.next_due("5 8 * * *", "Europe/Paris", apres=apres)
    utc = runner_tick.next_due("5 8 * * *", "UTC", apres=apres)
    assert paris.astimezone(datetime.timezone.utc).hour == 6
    assert utc.astimezone(datetime.timezone.utc).hour == 8


# ── la capacité : exigences et 404 org-scopé ──────────────────────────────────

def test_create_exige_procedure_cron_et_outils(monkeypatch):
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="create", procedure="veille-linkedin")
    assert e.value.code == "missing_fields" and "tools" in str(e.value.message)


def test_create_valide_puis_pose_avec_le_fuseau_par_defaut(monkeypatch):
    vu = {}
    monkeypatch.setattr(RT.db, "create_trigger",
                        lambda org, sub, **kw: vu.update(kw, org=org) or {"id": 1, **kw})
    out = _appel(_ctx(), op="create", procedure="veille-linkedin",
                 cron="5 6 * * *", tools=["linkedin_post", "data_write"])
    assert vu["tz"] == "Europe/Paris", "le défaut est ÉCRIT, pas supposé"
    assert vu["next_due"] is not None and out["trigger"]["id"] == 1


def test_un_cadencement_invalide_rend_la_cause(monkeypatch):
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="create", procedure="x", cron="* * * * *", tools=["a"])
    assert e.value.code == "invalid_schedule"


def test_update_du_cron_seul_revalide_avec_le_fuseau_effectif(monkeypatch):
    """Changer le cron sans toucher au fuseau doit revalider avec le fuseau STOCKÉ —
    valider l'un sans l'autre laisserait passer un couple incohérent."""
    monkeypatch.setattr(RT.db, "get_trigger",
                        lambda i, o: {"id": i, "cron": "5 6 * * *", "tz": "UTC"})
    vu = {}
    monkeypatch.setattr(RT.db, "update_trigger",
                        lambda i, o, champs: vu.update(champs) or {"id": i, **champs})
    _appel(_ctx(), op="update", trigger_id=3, cron="10 7 * * *")
    assert vu["tz"] == "UTC" and vu["cron"] == "10 7 * * *"
    assert vu["next_due"].astimezone(datetime.timezone.utc).hour == 7


def test_le_404_est_le_meme_pour_autrui_et_pour_linexistant(monkeypatch):
    monkeypatch.setattr(RT.db, "get_trigger", lambda i, o: None)
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="get", trigger_id=999)
    assert (e.value.status, e.value.code) == (404, "trigger_not_found")


# ── le tick : CAS gagné = enfile, CAS perdu = passe ───────────────────────────

def _declencheur(**kw):
    base = {"id": 5, "org_id": 2, "cron": "5 6 * * *", "tz": "Europe/Paris",
            "next_due": "2026-08-14 04:05:00", "procedure": "veille-linkedin",
            "project_id": None, "tools": ["data_write"], "input": None,
            "label": None, "max_steps": None}
    base.update(kw)
    return base


def test_le_tick_enfile_quand_il_gagne_le_cas(monkeypatch):
    enfile = {}
    monkeypatch.setattr(runner_tick.db, "due_triggers", lambda limit=50: [_declencheur()])
    monkeypatch.setattr(runner_tick.db, "consume_due", lambda i, vu, prochaine: True)
    monkeypatch.setattr(runner_tick.db, "enqueue_job",
                        lambda org, kind, payload=None: enfile.update(
                            org=org, kind=kind, payload=payload) or {"id": 9})
    assert runner_tick._tick() == 1
    assert enfile["org"] == 2 and enfile["kind"] == "start"
    assert enfile["payload"]["procedure"] == "veille-linkedin"
    assert enfile["payload"]["trigger_id"] == 5
    assert "input" not in enfile["payload"], "les champs None ne voyagent pas"


def test_le_tick_passe_quand_le_cas_est_perdu(monkeypatch):
    """L'autre environnement (même base) a consommé l'échéance : on n'enfile PAS —
    sinon chaque échéance produirait deux runs, un par tick."""
    monkeypatch.setattr(runner_tick.db, "due_triggers", lambda limit=50: [_declencheur()])
    monkeypatch.setattr(runner_tick.db, "consume_due", lambda i, vu, prochaine: False)
    monkeypatch.setattr(runner_tick.db, "enqueue_job",
                        lambda *a, **k: pytest.fail("CAS perdu ⟹ aucun job"))
    assert runner_tick._tick() == 0


def test_un_cron_devenu_invalide_ne_bloque_pas_les_autres(monkeypatch):
    bons = {}
    monkeypatch.setattr(runner_tick.db, "due_triggers", lambda limit=50: [
        _declencheur(id=1, cron="pas un cron"),
        _declencheur(id=2),
    ])
    monkeypatch.setattr(runner_tick.db, "consume_due", lambda i, vu, p: True)
    monkeypatch.setattr(runner_tick.db, "enqueue_job",
                        lambda org, kind, payload=None: bons.update(t=payload["trigger_id"]))
    assert runner_tick._tick() == 1
    assert bons["t"] == 2, "le déclencheur sain du tour est passé malgré le cassé"
