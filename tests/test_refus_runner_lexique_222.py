"""Les refus des automatisations parlent le lexique produit (oto#222).

Le front affiche le texte d'un refus MOT POUR MOT (« Refusé : {reason} ») : un refus
qui dit « flotte inconnue » ou « déclencheur inconnu » sert à l'utilisateur un
vocabulaire que le produit a quitté. Lexique arrêté le 17/09/2026 : ce qu'on déclare
est une **automatisation** (genres horaire, webhook, file), ce qui tourne une
**exécution** ; et la machine (runner, worker) n'est **jamais nommée** dans un refus,
qui dit l'effet.

Chaque refus listé par l'issue est levé ici : son STATUT et son CODE ne bougent pas
(le contrat technique), son texte suit le lexique. Les noms de champs, d'outils et de
routes — entre backticks (`fleet_id`, `runner.models`) — restent ce qu'ils sont.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from oto_mcp.capabilities import _ordonnanceur_de_campagne as ORD
from oto_mcp.capabilities import runner_fleets as RF
from oto_mcp.capabilities import runner_jobs as RJ
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.registry import by_key

# Ce banc ne parle pas de l'existence de la procédure d'un agent (#1499, conftest).
pytestmark = pytest.mark.usefixtures("procedures_connues")

_ANCIEN = re.compile(r"flotte|campagne|passage|déclencheur|programm|runner|worker|"
                     r"déroulé", re.I)


def _sans_backticks(texte: str) -> str:
    """Un nom de champ ou d'outil (`fleet_id`, `runner.models`) reste un nom."""
    return re.sub(r"`[^`]*`", "", texte)


def _lexique(texte: str) -> None:
    ancien = _ANCIEN.findall(_sans_backticks(texte))
    assert not ancien, f"ancien vocabulaire {ancien} dans : {texte}"


def _refus(status: int, code: str, appel) -> str:
    with pytest.raises(AuthzDenied) as e:
        appel()
    assert (e.value.status, e.value.code) == (status, code)
    _lexique(e.value.message)
    return e.value.message


@pytest.fixture(autouse=True)
def _socle(monkeypatch):
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **k: None)
    # Ni routée vers la ferme : sa garde de pose a son banc (`test_cle_de_modele_exigee`).
    monkeypatch.setattr("oto_mcp.capabilities._cle_exigee.org_routee_ferme",
                        lambda org_id: False)
    monkeypatch.setattr(RF.db, "runner_arme", lambda org: {
        "armed": True, "workers": 1, "last_seen": "2026-09-23 08:00:00",
        "families": ["anthropic"]})


def _ctx(org_id=2):
    return ResolvedCtx(sub="alexis", org_id=org_id)


def _flotte(**kw):
    return lambda: RF._fleets(_ctx(kw.pop("org_id", 2)), RF.FleetInput(**kw))


def _declencheur(**kw):
    if kw.get("op") == "create":
        kw.setdefault("model", "claude-sonnet-5")
    return lambda: asyncio.run(RT._triggers(_ctx(kw.pop("org_id", 2)),
                                            RT.TriggerInput(**kw)))


def _pour_lancer(monkeypatch, fleet):
    from oto_mcp import roles
    monkeypatch.setattr(roles, "is_org_admin", lambda *a, **k: True)
    monkeypatch.setattr(RF, "_run_courant", lambda: None)
    monkeypatch.setattr(RF._outils_manquants, "manquants", lambda *a, **k: [])
    monkeypatch.setattr(RF.db, "get_fleet", lambda *a, **k:
                        fleet and {"model": "claude-sonnet-5", **fleet})
    monkeypatch.setattr(RF.db, "armer", lambda *a, **k: None)


# ── les deux refus d'objet inconnu ────────────────────────────────────────────

def test_fleet_not_found_dit_automatisation_inconnue(monkeypatch):
    monkeypatch.setattr(RF.db, "get_fleet", lambda *a, **k: None)
    monkeypatch.setattr(RF.db, "fleet_state", lambda *a, **k: None)
    monkeypatch.setattr(RF.db, "update_fleet", lambda *a, **k: None)
    monkeypatch.setattr(RF.db, "demander_arret", lambda *a, **k: None)
    monkeypatch.setattr(RF, "_run_courant", lambda: None)
    for appel in (_flotte(op="get", fleet_id=1), _flotte(op="state", fleet_id=1),
                  _flotte(op="update", fleet_id=1, label="x"),
                  _flotte(op="stop", fleet_id=1)):
        assert _refus(404, "fleet_not_found", appel) == "automatisation inconnue"


def test_fleet_not_found_au_lancement_et_chez_l_ordonnanceur(monkeypatch):
    _pour_lancer(monkeypatch, None)
    assert _refus(404, "fleet_not_found", _flotte(op="launch", fleet_id=1)) \
        == "automatisation inconnue"
    monkeypatch.setattr(ORD.db, "prendre", lambda *a, **k: None)
    assert _refus(404, "fleet_not_found",
                  _flotte(op="take", fleet_id=1, taken_by="ord-1")) \
        == "automatisation inconnue"


def test_fleet_not_found_a_l_enfilement():
    assert _refus(404, "fleet_not_found", lambda: RJ._exige_flotte_servie(None)) \
        == "automatisation inconnue"


def test_trigger_not_found_dit_automatisation_inconnue(monkeypatch):
    monkeypatch.setattr(RT.db, "get_trigger", lambda *a, **k: None)
    monkeypatch.setattr(RT.db, "delete_trigger", lambda *a, **k: False)
    for appel in (_declencheur(op="get", trigger_id=1),
                  _declencheur(op="delete", trigger_id=1)):
        assert _refus(404, "trigger_not_found", appel) == "automatisation inconnue"


# ── les autres refus listés ───────────────────────────────────────────────────

def test_org_required_sur_les_deux_surfaces():
    assert _refus(400, "org_required", _flotte(op="list", org_id=None)) \
        == "les automatisations sont org-scopées"
    assert _refus(400, "org_required", _declencheur(op="list", org_id=None)) \
        == "les automatisations sont org-scopées"


def test_status_not_settable():
    _refus(400, "status_not_settable",
           _flotte(op="create", label="l", procedure="p", tools=["data_rows"],
                   status="running"))
    msg = _refus(400, "status_not_settable",
                 _flotte(op="update", fleet_id=1, status="stopped"))
    # Il nomme les gestes qui POSENT l'état, au lieu de dire qu'aucun ne le fait.
    assert "`op=launch`" in msg and "`op=stop`" in msg


def test_not_launchable(monkeypatch):
    _pour_lancer(monkeypatch, {"id": 1, "status": "running", "procedure": "p",
                               "input": "x", "tools": [], "sub": "alexis"})
    msg = _refus(409, "not_launchable", _flotte(op="launch", fleet_id=1))
    assert msg.startswith("cette automatisation est `running`")


def test_not_takeable(monkeypatch):
    monkeypatch.setattr(ORD.db, "prendre", lambda *a, **k: None)
    monkeypatch.setattr(ORD.db, "get_fleet", lambda *a, **k: {"model": "claude-sonnet-5", "id": 1,
                                                              "status": "draft"})
    msg = _refus(409, "not_takeable", _flotte(op="take", fleet_id=1, taken_by="o"))
    assert msg.startswith("cette automatisation est `draft`")


def test_target_et_context_is_frozen():
    _refus(400, "target_is_frozen", _flotte(op="update", fleet_id=1, namespace="t"))
    _refus(400, "context_is_frozen", _flotte(op="update", fleet_id=1, provider="openai"))
    _refus(400, "context_is_frozen",
           _flotte(op="update", fleet_id=1, descriptions_outils={"entieres": ["x"]}))


def test_missing_fields_et_field_not_settable():
    msg = _refus(400, "missing_fields", _flotte(op="create"))
    assert "le nom de l'automatisation" in msg
    _refus(400, "missing_fields", _flotte(op="update", fleet_id=1, tools=[]))
    _refus(400, "field_not_settable",
           _flotte(op="create", fleet_id=1, label="l", procedure="p", tools=["x"]))
    _refus(400, "field_not_settable", _flotte(op="update", fleet_id=1, procedure="q"))


def test_no_runner_armed_dit_l_effet_pas_la_machine(monkeypatch):
    sans = {"armed": False, "workers": 0, "last_seen": None, "families": ["anthropic"]}
    monkeypatch.setattr(RF.db, "runner_arme", lambda org: sans)
    _pour_lancer(monkeypatch, {"id": 1, "status": "draft", "procedure": "p",
                               "input": "x", "tools": [], "sub": "alexis"})
    msg = _refus(400, "no_runner_armed", _flotte(op="launch", fleet_id=1))
    assert msg.startswith("rien n'exécute les automatisations de cette org")
    msg = _refus(400, "no_runner_armed",
                 _declencheur(op="create", procedure="veille", cron="5 6 * * *",
                              tools=["data_write"]))
    assert msg.startswith("rien n'exécute les automatisations de cette org")


# ── les descriptions déclarées au contrat suivent ────────────────────────────

@pytest.mark.parametrize("cle, codes", [
    ("runner.fleets", None),
    ("runner.triggers", None),
    ("runner.jobs", {"fleet_not_found", "fleet_not_serving"}),
])
def test_les_descriptions_declarees_suivent_le_lexique(cle, codes):
    for err in by_key(cle).errors:
        if codes is None or err.code in codes:
            _lexique(err.when)


def test_les_codes_et_statuts_ne_bougent_pas():
    attendu = {("runner.fleets", "fleet_not_found"): 404,
               ("runner.triggers", "trigger_not_found"): 404,
               ("runner.jobs", "fleet_not_found"): 404}
    for (cle, code), status in attendu.items():
        assert {e.code: e.status for e in by_key(cle).errors}[code] == status
