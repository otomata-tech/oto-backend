"""Les modèles qu'une org ÉTEINT pour ses agents, SUR UNE VRAIE BASE : la colonne (tous
allumés par défaut), l'interrupteur et ses refus, le catalogue qui les marque et redérive
le défaut, la pose refusée, l'agent déjà posé ARRÊTÉ sans repli (horloge, webhook,
lancement à la main, campagne), les agents rendus par la bascule, et le travail déjà en
file arrêté à la réservation.
"""
from __future__ import annotations

import contextlib

import pytest

from oto_mcp.capabilities._types import AuthzDenied

OPUS, SONNET, HAIKU = "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"


@pytest.fixture
def org(live, monkeypatch):
    from oto_mcp import db
    from oto_mcp.capabilities import _cle_exigee
    from oto_mcp.db._conn import _connect
    # Anthropic servie, aucune clé exigée : le défaut de l'org est Sonnet.
    monkeypatch.setattr(_cle_exigee, "manquantes", lambda *a, **k: [])
    monkeypatch.setattr(db, "runner_arme", lambda org_id: {"families": ["anthropic"]})
    with _connect() as c:
        return c.execute("INSERT INTO orgs (name) VALUES ('banc-modeles-eteints') "
                         "RETURNING id").fetchone()["id"]


def _reglage(op, org_id, **champs):
    from oto_mcp.capabilities import org_console as oc
    from oto_mcp.capabilities._types import ResolvedCtx
    return oc._org_settings(ResolvedCtx(sub="admin", org_id=org_id, role="org_admin"),
                            oc.OrgSettingsInput(op=op, domain="models", org_id=org_id,
                                                **champs))


def _agent(org_id, model, enabled=True) -> int:
    from oto_mcp.db._conn import _connect
    with _connect() as c:
        return c.execute(
            "INSERT INTO runner_triggers (org_id, sub, procedure, tools, model, enabled) "
            "VALUES (%s, 'admin', 'veille', '[]'::jsonb, %s, %s) RETURNING id",
            (org_id, model, enabled)).fetchone()["id"]


def test_tous_allumes_par_defaut_puis_un_eteint_et_rallume(org):
    assert _reglage("get", org) == {"org_id": org, "disabled": []}
    assert _reglage("set", org, model=OPUS, enabled=False) == {
        "org_id": org, "disabled": [OPUS], "model": OPUS,
        "agents_on_model": {"count": 0, "ids": []}}
    # Idempotent : l'éteindre deux fois ne le liste qu'une.
    assert _reglage("set", org, model=OPUS, enabled=False)["disabled"] == [OPUS]
    assert _reglage("set", org, model=OPUS, enabled=True)["disabled"] == []


def test_un_modele_inconnu_ou_un_abonnement_ne_se_bascule_pas(org):
    for model in ("gpt-5", "sub:sonnet"):
        with pytest.raises(AuthzDenied) as e:
            _reglage("set", org, model=model, enabled=False)
        assert (e.value.status, e.value.code) == (400, "unknown_model")


def test_une_org_inconnue_est_404(live):
    from oto_mcp.capabilities.orgs import models as om
    with pytest.raises(AuthzDenied) as e:
        om.lire(999_999)
    assert (e.value.status, e.value.code) == (404, "unknown_org")


def test_le_modele_par_defaut_ne_s_eteint_pas(org):
    with pytest.raises(AuthzDenied) as e:
        _reglage("set", org, model=SONNET, enabled=False)
    assert (e.value.status, e.value.code) == (409, "model_is_default")
    # Le rallumer (ou le laisser allumé) reste permis.
    assert _reglage("set", org, model=SONNET, enabled=True)["disabled"] == []


def test_le_catalogue_marque_l_eteint_et_redérive_le_defaut(org):
    from oto_mcp import db
    from oto_mcp.capabilities import _modele
    db.modeles_eteints.poser(org, SONNET, False)   # posé en base, hors de la garde 409
    etat = _modele.etat_servi({"families": ["anthropic"]}, org)
    par_id = {m["id"]: m for m in etat["models"]}
    assert par_id[SONNET]["allowed"] is False and par_id[SONNET]["disabled"] is True
    assert par_id[SONNET]["default"] is False
    assert par_id[OPUS]["default"] is True and "disabled" not in par_id[OPUS]
    assert sum(1 for m in etat["models"] if m["default"]) == 1


def test_la_pose_sur_un_modele_eteint_est_refusee(org):
    from oto_mcp.capabilities import _modele
    _reglage("set", org, model=OPUS, enabled=False)
    with pytest.raises(AuthzDenied) as e:
        _modele.exige_modele_permis(org, OPUS)
    assert (e.value.status, e.value.code) == (403, "model_disabled")
    _modele.exige_modele_permis(org, HAIKU)


def test_la_bascule_rend_les_agents_poses_sur_le_modele_en_pause_compris(org):
    from oto_mcp.db._conn import _connect
    actif, en_pause = _agent(org, OPUS), _agent(org, OPUS, enabled=False)
    _agent(org, HAIKU)                                   # un autre modèle : pas compté
    with _connect() as c:
        autre = c.execute("INSERT INTO orgs (name) VALUES ('banc-autre') "
                          "RETURNING id").fetchone()["id"]
    _agent(autre, OPUS)                                  # une autre org : pas comptée
    attendu = {"count": 2, "ids": sorted([actif, en_pause])}
    assert _reglage("set", org, model=OPUS, enabled=False)["agents_on_model"] == attendu
    # Rallumer rend les mêmes : ceux qui repartent.
    assert _reglage("set", org, model=OPUS, enabled=True)["agents_on_model"] == attendu


@pytest.fixture
def tick(org, monkeypatch):
    """Le tick sur UN agent de l'org, posé sur Opus. Rend les livraisons écrites et les
    travaux enfilés."""
    from oto_mcp import runner_tick
    ecrit, enfiles = [], []
    t = {"id": 5, "org_id": org, "sub": "admin", "cron": "5 6 * * *", "tz": "UTC",
         "next_due": "2026-11-16 06:05:00", "procedure": "veille", "project_id": None,
         "tools": [], "input": "go", "label": None, "max_steps": None, "model": OPUS,
         "enabled": True}
    monkeypatch.setattr(runner_tick.db, "due_triggers", lambda: [t])
    monkeypatch.setattr(runner_tick.db, "consume_due", lambda *a: True)
    monkeypatch.setattr(runner_tick.org_suspension, "etat", lambda org: None)
    monkeypatch.setattr(runner_tick.procedure_retiree, "archivee", lambda *a: None)
    monkeypatch.setattr(runner_tick, "exigence_manquee", lambda t: None)
    monkeypatch.setattr(runner_tick.db, "_connect", lambda: contextlib.nullcontext("conn"))
    monkeypatch.setattr(runner_tick.db, "enregistrer",
                        lambda conn, tid, org, issue, **k: ecrit.append((tid, issue)))
    monkeypatch.setattr(runner_tick, "charge_du_declencheur",
                        lambda t, *a, **k: {"model": t["model"]})
    monkeypatch.setattr(runner_tick, "cibles_du_relais", lambda t: [])
    monkeypatch.setattr(runner_tick._abonnement, "raison_de_peremption",
                        lambda *a, **k: "raison")
    monkeypatch.setattr(runner_tick.db, "perimer_travaux_du_declencheur",
                        lambda *a, **k: 0)
    monkeypatch.setattr(runner_tick.db, "enqueue_job",
                        lambda org, kind, sub=None, payload=None, **k: enfiles.append(payload))
    return ecrit, enfiles


def test_l_horloge_arrete_l_agent_d_un_modele_eteint_sans_repli(tick, org):
    from oto_mcp import runner_tick
    ecrit, enfiles = tick
    _reglage("set", org, model=OPUS, enabled=False)
    assert runner_tick._tick() == 0
    assert enfiles == [], "aucun repli sur le défaut : rien n'est enfilé"
    assert ecrit == [(5, "refused_model_disabled")]


def test_l_horloge_enfile_l_agent_d_un_modele_allume(tick):
    from oto_mcp import runner_tick
    ecrit, enfiles = tick
    assert runner_tick._tick() == 1
    assert ecrit == [] and enfiles[0]["model"] == OPUS


def test_le_webhook_refuse_l_agent_d_un_modele_eteint_et_le_nomme(org, monkeypatch):
    from oto_mcp import runner_hook
    ecrit = []
    t = {"id": 7, "org_id": org, "sub": "admin", "procedure": "veille", "model": OPUS,
         "enabled": True, "project_id": None}
    monkeypatch.setattr(runner_hook, "_authentifier", lambda *a, **k: (t, None, None))
    monkeypatch.setattr(runner_hook.org_suspension, "etat", lambda org: None)
    monkeypatch.setattr(runner_hook.procedure_retiree, "archivee", lambda *a: None)
    monkeypatch.setattr(runner_hook.db, "_connect", lambda: contextlib.nullcontext("conn"))
    monkeypatch.setattr(runner_hook.db, "enregistrer",
                        lambda conn, tid, org, issue, **k: ecrit.append((tid, issue)))
    monkeypatch.setattr(runner_hook.db, "enqueue_job",
                        lambda *a, **k: pytest.fail("rien ne doit être enfilé"))
    _reglage("set", org, model=OPUS, enabled=False)
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(7, "secret", None)
    assert (e.value.statut, e.value.code) == (409, "model_disabled")
    assert ecrit == [(7, "refused_model_disabled")]


def test_le_lancement_a_la_main_d_un_modele_eteint_est_refuse(org):
    from oto_mcp.capabilities import runner_triggers
    from oto_mcp.capabilities._types import ResolvedCtx
    _reglage("set", org, model=OPUS, enabled=False)
    ctx = ResolvedCtx(sub="admin", org_id=org, role="org_admin")
    with pytest.raises(AuthzDenied) as e:
        runner_triggers._exiger_lancable(ctx, {"sub": "admin"}, None, OPUS)
    assert (e.value.status, e.value.code) == (403, "model_disabled")


def test_la_campagne_d_un_modele_eteint_s_arrete_nommee(org, monkeypatch):
    from oto_mcp import db
    from oto_mcp.capabilities import runner_jobs
    arrets = []
    monkeypatch.setattr(db, "une_campagne_vit", lambda org_id: True)
    monkeypatch.setattr(db, "arreter_campagnes_epuisees", lambda org_id: [])
    monkeypatch.setattr(db, "accuser_arrets_effectifs", lambda org_id: [])
    monkeypatch.setattr(db, "clore_campagnes_terminees", lambda org_id: [])
    monkeypatch.setattr(db, "campagne_a_servir", lambda org_id, ordonner: {
        "id": 3, "org_id": org, "sub": "admin", "model": OPUS, "procedure": "veille"})
    monkeypatch.setattr(db, "demander_arret",
                        lambda fid, oid, raison: arrets.append((fid, oid, raison)))
    monkeypatch.setattr(db, "enqueue_job",
                        lambda *a, **k: pytest.fail("rien ne doit être enfilé"))
    _reglage("set", org, model=OPUS, enabled=False)
    assert runner_jobs._produire_pour_une_campagne(org, 60) is None
    assert arrets == [(3, org, "model_disabled")]


def test_un_travail_deja_en_file_est_arrete_a_la_reservation(org, monkeypatch):
    from oto_mcp.capabilities import _modele, runner_jobs
    vu = {}

    def refuser(job, appelant, raison):
        vu["raison"] = raison
        return {"refuse": True}
    monkeypatch.setattr(runner_jobs, "_refuser_sans_cle", refuser)
    _reglage("set", org, model=OPUS, enabled=False)
    job = {"id": 1, "org_id": org, "sub": "admin",
           "payload": {"model": OPUS, "model_family": "anthropic"}}
    assert runner_jobs._avec_cle(job, "anthropic", "worker", worker=True) == {"refuse": True}
    assert vu["raison"] == _modele.RAISON_MODELE_ETEINT
