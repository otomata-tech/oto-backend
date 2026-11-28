"""Un AGENT-RECETTE : un déclencheur qui joue une recette publiée, sans modèle.

Prouvé sur vraie base : la pose épingle la version, les champs d'agent sont refusés,
l'horloge / le webhook / `run_now` enfilent la MÊME charge de famille `recette`, seul
un worker `provider="recette"` la réserve (sans clé de modèle), une recette devenue
indisponible saute l'occurrence avec son motif, et la face REST ne sert au jeton
délégué que le run épinglé de son travail. `X-Oto-Run` lie la requête REST au run.
"""
from __future__ import annotations

import dataclasses
import os
import uuid

import pytest

from oto_mcp.capabilities import runner_jobs as RJ
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

ORG = 9733
A = "sub-recette-a"
SLUG = "veille-test"
CORPS = {"kind": "code",
         "sources": {"recipe.py": "async def run(oto, params):\n"
                                  "    await oto.call(\"fr_search\", {\"q\": params[\"q\"]})\n"
                                  "    return {\"n\": 0}\n"},
         "entrypoint": "recipe:run", "params": {"q": {"required": True}},
         "limits": {"max_units": 1}}


@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_recette_agent_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    avant_url, avant_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    avant_key = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["DATABASE_URL"] = dsn
    os.environ["OTO_MCP_MASTER_KEY"] = "5" * 64
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        _poser_la_recette()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = avant_pool
        for cle, valeur in (("DATABASE_URL", avant_url),
                            ("OTO_MCP_MASTER_KEY", avant_key)):
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


def _poser_la_recette():
    from oto_mcp.db import recipes as dbr
    f = dbr.create_recipe(owner_type="org", owner_id=str(ORG), slug=SLUG, title="Veille",
                          description="", created_by=A, body=CORPS, note=None)
    dbr.decide_version(f["id"], 1, status="publiee", decided_by=A, test_report=None)
    dbr.create_recipe(owner_type="org", owner_id=str(ORG), slug="brouillon",
                      title="Brouillon", description="", created_by=A, body=CORPS,
                      note=None)


def _worker_recette_vivant():
    """Un worker `recette` sonde la file : sa présence rend la famille servie."""
    from oto_mcp import db
    db.claim_next_job(None, "w-recette", lease_seconds=60, depot="recette",
                      famille_seule=True)


def _ctx():
    return ResolvedCtx(sub=A, org_id=ORG)


def _vider():
    from oto_mcp import db
    with db._connect() as conn:
        conn.execute("DELETE FROM runner_jobs WHERE org_id = %s", (ORG,))


def _creer(**kw):
    _worker_recette_vivant()
    base = dict(op="create", recipe={"slug": SLUG, "params": {"q": "x"}},
                cron="0 7 * * *", private=True)
    base.update(kw)
    return RT._triggers_sync(_ctx(), RT.TriggerInput(**base))["trigger"]


def test_la_pose_epingle_la_version_et_ne_porte_ni_modele_ni_procedure(live):
    from oto_mcp import db
    t = _creer()
    assert t["recipe"] == {"slug": SLUG, "version": 1, "params": {"q": "x"},
                           "datastore": None}
    assert t["procedure"] is None and t["model"] is None
    assert t["recipe_title"] == "Veille" and t["recipe_latest_version"] == 1
    # L'ancien code lit une ligne SANS modèle (il saute l'occurrence) : jamais NULL ici.
    with db._connect() as conn:
        brut = conn.execute("SELECT procedure, tools, model FROM runner_triggers "
                            "WHERE id = %s", (t["id"],)).fetchone()
    assert brut["procedure"] == "" and brut["tools"] == [] and brut["model"] is None


def test_une_version_publiee_plus_tard_ne_change_pas_l_epingle(live):
    from oto_mcp.db import recipes as dbr
    t = _creer()
    f = dbr.get_recipe("org", str(ORG), SLUG)
    n = dbr.propose_version(f["id"], expected_version=f["latest_version"], body=CORPS,
                            note=None, proposed_by=A)
    dbr.decide_version(f["id"], n, status="publiee", decided_by=A, test_report=None)
    lu = RT._triggers_sync(_ctx(), RT.TriggerInput(op="get", trigger_id=t["id"]))["trigger"]
    assert lu["recipe"]["version"] == 1 and lu["recipe_latest_version"] == n
    # Re-épingler est explicite.
    maj = RT._triggers_sync(_ctx(), RT.TriggerInput(
        op="update", trigger_id=t["id"], recipe={"slug": SLUG, "version": n}))["trigger"]
    assert maj["recipe"]["version"] == n and maj["recipe"]["params"] == {"q": "x"}


@pytest.mark.parametrize("champ,valeur", [("model", "claude-haiku-4-5"),
                                          ("procedure", "p"), ("input", "go"),
                                          ("tools", ["fr_search"])])
def test_un_champ_d_agent_est_refuse_en_le_nommant(live, champ, valeur):
    with pytest.raises(AuthzDenied) as e:
        _creer(**{champ: valeur})
    assert e.value.code == "recipe_trigger_field" and champ in e.value.message


@pytest.mark.parametrize("recette,code", [
    ({"slug": "inconnue"}, "unknown_recipe"),
    ({"slug": "brouillon", "params": {"q": "x"}}, "recipe_not_published"),
    ({"slug": SLUG}, "recipe_missing_params"),
    ({"slug": SLUG, "version": 42, "params": {"q": "x"}}, "recipe_version_unknown"),
])
def test_une_recette_qui_ne_tourne_pas_est_refusee_a_la_pose(live, recette, code):
    with pytest.raises(AuthzDenied) as e:
        _creer(recipe=recette)
    assert e.value.code == code


def test_un_agent_a_modele_ne_devient_pas_un_agent_recette(live):
    from oto_mcp import db
    agent = db.create_trigger(ORG, A, procedure="p", tz="Europe/Paris", tools=["x"],
                              cron="0 7 * * *", model="claude-haiku-4-5")
    with pytest.raises(AuthzDenied) as e:
        RT._triggers_sync(_ctx(), RT.TriggerInput(
            op="update", trigger_id=agent["id"], recipe={"slug": SLUG}))
    assert e.value.code == "recipe_immutable"


def _travaux(trigger_id):
    from oto_mcp import db
    with db._connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, payload, sub FROM runner_jobs WHERE org_id = %s "
            "AND payload->>'trigger_id' = %s ORDER BY id", (ORG, str(trigger_id)))]


def _rendre_du(trigger_id):
    from oto_mcp import db
    with db._connect() as conn:
        conn.execute("UPDATE runner_triggers SET next_due = NOW() - INTERVAL '1 minute' "
                     "WHERE id = %s", (trigger_id,))


def test_l_horloge_enfile_une_charge_recette_sans_modele(live):
    from oto_mcp import runner_tick
    _vider()
    t = _creer()
    _rendre_du(t["id"])
    runner_tick._tick()
    (job,) = _travaux(t["id"])
    p = job["payload"]
    assert p["model_family"] == "recette" and job["sub"] == A
    assert p["recipe"] == {"slug": SLUG, "version": t["recipe"]["version"],
                           "scope": "org", "params": {"q": "x"}}
    assert p["tools"] == ["oto_recipe", "fr_search"]
    assert not ({"procedure", "input", "model"} & set(p))


def test_une_recette_devenue_indisponible_saute_l_occurrence_avec_son_motif(live):
    from oto_mcp import db, runner_tick
    _vider()
    t = _creer()
    db.update_trigger(t["id"], ORG, {"recipe": {**t["recipe"], "version": 99}})
    _rendre_du(t["id"])
    runner_tick._tick()
    assert _travaux(t["id"]) == []
    with db._connect() as conn:
        issue = conn.execute("SELECT outcome FROM runner_hook_deliveries WHERE "
                             "trigger_id = %s ORDER BY id DESC LIMIT 1",
                             (t["id"],)).fetchone()
    assert issue["outcome"] == db.REFUSE_RECIPE_UNAVAILABLE


def test_run_now_enfile_la_meme_charge_au_nom_de_l_appelant(live):
    _vider()
    t = _creer()
    rep = RT._triggers_sync(_ctx(), RT.TriggerInput(op="run_now", trigger_id=t["id"]))
    (job,) = _travaux(t["id"])
    assert rep["job"]["id"] == job["id"] and job["payload"]["model_family"] == "recette"
    with pytest.raises(AuthzDenied) as e:
        RT._triggers_sync(_ctx(), RT.TriggerInput(op="run_now", trigger_id=t["id"]))
    assert e.value.code == "run_already_queued"


def test_un_travail_de_recette_n_a_qu_une_tentative(live):
    """Jamais rejoué : un échec ne repart pas en file (sinon le refus du worker
    `recipe_job_not_replayed` écrasait la vraie cause dans `last_error`)."""
    from oto_mcp import db
    _vider()
    t = _creer()
    RT._triggers_sync(_ctx(), RT.TriggerInput(op="run_now", trigger_id=t["id"]))
    (job,) = _travaux(t["id"])
    with db._connect() as conn:
        n = conn.execute("SELECT max_attempts FROM runner_jobs WHERE id = %s",
                         (job["id"],)).fetchone()["max_attempts"]
    assert n == 1


def test_le_webhook_enfile_la_charge_recette_marquee_hook(live):
    from oto_mcp import db, runner_hook
    _vider()
    t = _creer(kind="webhook", cron=None)
    runner_hook.enfiler(db.get_trigger(t["id"], ORG), None, "test")
    (job,) = _travaux(t["id"])
    assert job["payload"]["hook"] is True
    assert job["payload"]["recipe"]["slug"] == SLUG


def test_un_webhook_recette_refuse_le_corps_transmis(live):
    with pytest.raises(AuthzDenied) as e:
        _creer(kind="webhook", cron=None, payload_mode="inline")
    assert e.value.code == "recipe_trigger_field"


# ── La réservation ────────────────────────────────────────────────────────────

def _delegue_factice(monkeypatch):
    # Hors sujet ici : l'émission du jeton (comptes, appartenances).
    monkeypatch.setattr(RJ, "_delegue",
                        lambda job, bail, claimant: {**job, "delegated_token": "otd_x"})


def test_seul_le_worker_recette_reserve_un_travail_recette_sans_cle(live, monkeypatch):
    _vider()
    _delegue_factice(monkeypatch)
    t = _creer()
    RT._triggers_sync(_ctx(), RT.TriggerInput(op="run_now", trigger_id=t["id"]))
    autre = ResolvedCtx(sub="w-anthropic", org_id=None, platform_worker=True)
    assert RJ._jobs(autre, RJ.JobsInput(op="claim", provider="anthropic",
                                         org_ids=[ORG])) == {"job": None}
    assert RJ._jobs(autre, RJ.JobsInput(op="claim", org_ids=[ORG])) == {"job": None}
    worker = ResolvedCtx(sub="w-recette", org_id=None, platform_worker=True)
    pris = RJ._jobs(worker, RJ.JobsInput(op="claim", provider="recette",
                                          org_ids=[ORG], lease_seconds=900))["job"]
    assert pris["payload"]["recipe"]["slug"] == SLUG
    assert pris["delegated_token"] == "otd_x"
    assert not pris.get("model_key") and not pris.get("delegation_refusee")


def test_le_worker_recette_ne_prend_pas_un_travail_sans_famille(live, monkeypatch):
    from oto_mcp import db
    _vider()
    _delegue_factice(monkeypatch)
    db.enqueue_job(ORG, "start", payload={"input": "go", "tools": []}, sub=A)
    worker = ResolvedCtx(sub="w-recette", org_id=None, platform_worker=True)
    assert RJ._jobs(worker, RJ.JobsInput(op="claim", provider="recette",
                                          org_ids=[ORG])) == {"job": None}
    _vider()


def test_un_membre_ne_reserve_pas_en_worker_recette(live):
    with pytest.raises(AuthzDenied) as e:
        RJ._jobs(_ctx(), RJ.JobsInput(op="claim", provider="recette"))
    assert e.value.code == "recipe_worker_platform_only"


# ── La face REST sous jeton délégué ───────────────────────────────────────────

def test_le_jeton_delegue_ne_joue_que_la_recette_epinglee_de_son_travail(live):
    from oto_mcp.api import base as api_base
    from oto_mcp.capabilities import recipes as cap
    assert ("POST", "/api/me/recipes") in api_base.ROUTES_DE_LA_DELEGATION
    _vider()
    t = _creer()
    RT._triggers_sync(_ctx(), RT.TriggerInput(op="run_now", trigger_id=t["id"]))
    (job,) = _travaux(t["id"])
    bon = cap.RecipeInput(op="run", slug=SLUG, scope="org",
                          version=t["recipe"]["version"], params={"q": "x"})
    cap._exiger_la_recette_du_travail(bon, job["id"])
    for autre in (bon.model_copy(update={"slug": "brouillon"}),
                  bon.model_copy(update={"version": 77}),
                  bon.model_copy(update={"params": {"q": "y"}}),
                  bon.model_copy(update={"datastore": 12}),
                  bon.model_copy(update={"op": "get"})):
        with pytest.raises(AuthzDenied) as e:
            cap._exiger_la_recette_du_travail(autre, job["id"])
        assert e.value.code == "delegation_recipe_mismatch"


def test_x_oto_run_lie_la_requete_rest_au_run(monkeypatch):
    """Le worker lie le run par l'en-tête `X-Oto-Run` (pas de `_run_id` dans le corps) :
    l'adaptateur REST le pose là où le MCP pose `_run_id`, que l'exécution d'une recette
    lit pour ranger ses appels sous ce run."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oto_mcp import session_org
    from oto_mcp.api import base as api_base
    from oto_mcp.capabilities import _rest_adapter, run_thread
    from oto_mcp.capabilities import recipes as cap
    from oto_mcp.capabilities.registry import CAPABILITIES

    vu = {}

    async def handler(ctx, inp):
        vu["run"] = session_org.current_call_run()
        return {"receipt": {}}

    async def authentifie(request, verifier, **kw):
        return "u-rest", None

    monkeypatch.setattr(run_thread, "run_de_l_en_tete", lambda sub, run: ORG)
    (c,) = [c for c in CAPABILITIES if c.key == "me.recipe"]
    c = dataclasses.replace(c, handler=handler,
                            authz=lambda raw, inp: ResolvedCtx(sub=raw.sub, org_id=None))
    routes = _rest_adapter.make_routes(None, authentifie, api_base._json,
                                       api_base._json_error, api_base.options_handler, [c])
    r = TestClient(Starlette(routes=routes)).post(
        "/api/me/recipes", json={"op": "run", "slug": SLUG}, headers={"X-Oto-Run": "run-42"})
    assert r.status_code == 200, r.text
    assert vu["run"] == "run-42"
    assert cap  # la capacité est enregistrée


def test_recipe_est_un_objet_nu_valide_par_recipespec():
    """Le schéma d'outil reste PLAT (`recipe` = dict) ; la forme se valide au geste."""
    import pytest as _pytest
    from oto_mcp.capabilities import _declencheur_recette as dr
    from oto_mcp.capabilities._types import AuthzDenied
    assert dr.spec({"slug": "x", "version": 2, "params": {"a": 1}, "datastore": 5}) == {
        "slug": "x", "version": 2, "params": {"a": 1}, "datastore": 5}
    with _pytest.raises(AuthzDenied) as e:
        dr.spec({"version": "deux"})
    assert e.value.code == "invalid_recipe_spec"


# ── Le report : un refus AVANT démarrage rend le travail à la file ──────────

def _prendre_en_worker_recette(monkeypatch, t):
    _delegue_factice(monkeypatch)
    RT._triggers_sync(_ctx(), RT.TriggerInput(op="run_now", trigger_id=t["id"]))
    worker = ResolvedCtx(sub="w-recette", org_id=None, platform_worker=True)
    pris = RJ._jobs(worker, RJ.JobsInput(op="claim", provider="recette",
                                          org_ids=[ORG], lease_seconds=900))["job"]
    return worker, pris


def _ligne(job_id):
    from oto_mcp import db
    with db._connect() as conn:
        return dict(conn.execute(
            "SELECT status, attempts, max_attempts, run_id, claimed_by, last_error, "
            "payload, due_at > NOW() AS plus_tard FROM runner_jobs WHERE id = %s",
            (job_id,)).fetchone())


def test_defer_rend_le_travail_sans_tentative_et_sans_run(live, monkeypatch):
    """Le bac à sable plein ne tue plus l'unique tentative : `pending`, tentative
    rendue, run détaché (la reprise ouvre un run neuf), échéance reculée."""
    from oto_mcp import db
    _vider()
    t = _creer()
    worker, pris = _prendre_en_worker_recette(monkeypatch, t)
    with db._connect() as conn:
        conn.execute("INSERT INTO runs (run_id, sub, org_id, label) VALUES "
                     "('run-avant', %s, %s, 'recette') ON CONFLICT DO NOTHING", (A, ORG))
    db.bind_job_run(pris["id"], "w-recette", "run-avant")
    rep = RJ._jobs(worker, RJ.JobsInput(op="defer", job_id=pris["id"],
                                         retry_after_seconds=1,
                                         reason="sandbox_busy: account slot held"))
    assert rep["status"] == "pending" and rep["reports"] == 1
    ligne = _ligne(pris["id"])
    assert ligne["status"] == "pending" and ligne["attempts"] == 0
    assert ligne["run_id"] is None and ligne["claimed_by"] is None
    assert ligne["plus_tard"] is True          # borné à 5 s minimum
    assert ligne["last_error"].startswith("deferred: sandbox_busy")
    # Repris : la tentative est la première, aucun run lié — la garde « jamais
    # rejouée » du worker (tentative > 1 ou run lié) ne le prend pas pour un rejeu.
    with db._connect() as conn:
        conn.execute("UPDATE runner_jobs SET due_at = NOW() WHERE id = %s", (pris["id"],))
    repris = RJ._jobs(worker, RJ.JobsInput(op="claim", provider="recette",
                                            org_ids=[ORG], lease_seconds=900))["job"]
    assert repris["id"] == pris["id"]
    assert repris["attempts"] == 1 and not repris.get("run_id")


def test_defer_au_dela_du_plafond_echoue_recipe_never_started(live, monkeypatch):
    from oto_mcp import db
    _vider()
    t = _creer()
    worker, pris = _prendre_en_worker_recette(monkeypatch, t)
    with db._connect() as conn:
        conn.execute("UPDATE runner_jobs SET payload = jsonb_set(payload, '{_reports}', "
                     "to_jsonb(%s::int)) WHERE id = %s", (db.REPORTS_RECETTE_MAX, pris["id"]))
    rep = RJ._jobs(worker, RJ.JobsInput(op="defer", job_id=pris["id"],
                                         retry_after_seconds=30, reason="sandbox_busy"))
    assert rep["status"] == "failed"
    ligne = _ligne(pris["id"])
    assert ligne["status"] == "failed"
    assert ligne["last_error"].startswith("recipe_never_started: sandbox_busy")


def test_defer_refuse_hors_prise_et_hors_recette(live, monkeypatch):
    from oto_mcp import db
    _vider()
    t = _creer()
    worker, pris = _prendre_en_worker_recette(monkeypatch, t)
    intrus = ResolvedCtx(sub="w-autre", org_id=None, platform_worker=True)
    with pytest.raises(AuthzDenied) as e:
        RJ._jobs(intrus, RJ.JobsInput(op="defer", job_id=pris["id"],
                                       retry_after_seconds=10, reason="x"))
    assert e.value.code == "job_not_found"
    with pytest.raises(AuthzDenied) as e:
        RJ._jobs(worker, RJ.JobsInput(op="defer", job_id=pris["id"]))
    assert e.value.code == "missing_fields"
    # Un travail d'agent à modèle ne se reporte pas : il a ses tentatives.
    job = db.enqueue_job(ORG, "start", payload={"input": "go", "tools": [],
                                                "model_family": "anthropic"}, sub=A)
    jid = job["id"] if isinstance(job, dict) else job
    with db._connect() as conn:
        conn.execute("UPDATE runner_jobs SET status = 'claimed', claimed_by = 'w-recette', "
                     "lease_until = NOW() + INTERVAL '5 minutes' WHERE id = %s", (jid,))
    with pytest.raises(AuthzDenied) as e:
        RJ._jobs(worker, RJ.JobsInput(op="defer", job_id=jid,
                                       retry_after_seconds=10, reason="x"))
    assert e.value.code == "defer_recipe_only"
    _vider()
