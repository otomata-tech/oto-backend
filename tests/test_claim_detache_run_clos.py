"""Un `start` repris après la clôture de son run se sert SANS run — et le travail
garde la trace des runs qu'il a détachés (`payload._plateforme.runs_detaches`).

Mesuré le 13/09/2026 sur banc : après `complete(ok=false)`, la tentative suivante
recevait le MÊME `run_id`, que le runner venait de clore (`run_finish(failed)`). Le
worker reprenait alors un fil clos dont l'historique disait « tu tiens r0 », sur une
ligne que la conclusion avait déjà libérée.

Prouvé contre un PostgreSQL jetable, par la file telle que la route l'appelle
(`runner.jobs` → `_jobs`, vrai `db.claim_next_job`). Les faits `run_start` /
`run_finish` sont écrits dans `tool_calls` sous la forme du journal : c'est là, et
nulle part ailleurs, que la clôture se lit. La réservation de ligne passe par le
chemin monté (middleware `_run_id=` + outil de `register_all`).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from oto_mcp.capabilities import runner_jobs as RJ
from oto_mcp.capabilities._types import ResolvedCtx

SUB = "sub-run-clos"
ORG = 7131
WORKER = "worker-run-clos"


@pytest.fixture(scope="module")
def live(pg_dsn):
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_run_clos_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name

    prev_url, prev_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        # Le porteur d'un travail EXISTE et est membre de son org : le serveur le
        # vérifie à la réservation (cf. tests/test_complete_releases_633.py).
        from oto_mcp.db._conn import _connect
        with _connect() as _c:
            _c.execute("INSERT INTO users (sub) VALUES (%s) ON CONFLICT DO NOTHING",
                       (WORKER,))
            _c.execute("INSERT INTO orgs (id, name) VALUES (%s, %s) "
                       "ON CONFLICT DO NOTHING", (ORG, "org du banc"))
            _c.execute("INSERT INTO org_members (org_id, sub, org_role) "
                       "VALUES (%s, %s, 'org_admin') ON CONFLICT DO NOTHING",
                       (ORG, WORKER))
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = prev_pool
        if prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_url
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


@pytest.fixture(autouse=True)
def _file_vide(live):
    """Une file vide par cas : la réservation prend le plus ancien travail de l'org,
    et un `pending` laissé par un cas ne doit pas être servi au suivant."""
    _sql("DELETE FROM runner_jobs")


@pytest.fixture
def surface(live, monkeypatch):
    """Les outils `data_*` tels que le serveur les monte, l'acteur tenu."""
    from oto_mcp.datastore.core import make_store
    from oto_mcp.tools import datastore as T
    monkeypatch.setattr(T, "_acting_store", lambda: make_store(SUB))
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    monkeypatch.setattr(T, "_project_hint", lambda ns: None)


# ── outillage ────────────────────────────────────────────────────────────────────

def _sql(requete: str, *args) -> None:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute(requete, args)


def _jobs(**kw) -> dict:
    """La capacité telle que la route l'appelle — le worker porte un jeton d'org."""
    return RJ._jobs(ResolvedCtx(sub=WORKER, org_id=ORG), RJ.JobsInput(**kw))


def _relu(job_id: int) -> dict:
    from oto_mcp import db
    return db.get_job(job_id, ORG)


def _trace(job: dict) -> list:
    return ((job.get("payload") or {}).get("_plateforme") or {}).get("runs_detaches") or []


def _run_ouvert() -> str:
    """Un run tel que le journal le connaît : l'index (`runs`, cible de la FK du
    travail) ET le fait `run_start`, que le middleware stampe de son `run_id`."""
    from oto_mcp import db
    run_id = uuid.uuid4().hex
    db.insert_run(run_id, sub=WORKER, org_id=ORG, label="travail hébergé")
    db.insert_tool_call({"tool": "run_start", "sub": WORKER, "org_id": ORG,
                         "run_id": run_id, "args": {"label": "travail hébergé"},
                         "ok": True})
    return run_id


def _clore(run_id: str, *, fait: bool = True) -> None:
    """`run_finish(failed)` comme le runner le joue : l'écriture de confort de
    l'index, et — sauf `fait=False` — le FAIT, que le middleware inscrit en tâche
    de fond après la réponse."""
    from oto_mcp import db
    db.finish_run(run_id, "failed", sub=WORKER)
    if fait:
        db.insert_tool_call({"tool": "run_finish", "sub": WORKER, "org_id": ORG,
                             "run_id": run_id,
                             "args": {"run_id": run_id, "outcome": "failed"},
                             "ok": True})


def _prendre() -> dict:
    job = _jobs(op="claim")["job"]
    assert job, "la file portait un travail : le claim le rend"
    return job


def _backoff_ecoule(job_id: int) -> None:
    _sql("UPDATE runner_jobs SET due_at = NOW() WHERE id = %s", job_id)


def _bail_mort(job_id: int) -> None:
    _sql("UPDATE runner_jobs SET lease_until = NOW() - interval '1 second' "
         "WHERE id = %s", job_id)


def _vol_rate(*, max_attempts: int = 3, fait: bool = True) -> tuple[int, str]:
    """T1 comme le runner le joue : claim → run_start → bind_run → run_finish(failed)
    → complete(ok=false). Rend (travail, run clos)."""
    _jobs(op="enqueue", kind="start", max_attempts=max_attempts,
          payload={"procedure": "p-run-clos", "input": "fais le travail"})
    job = _prendre()
    r1 = _run_ouvert()
    assert _jobs(op="bind_run", job_id=job["id"], run_id=r1)["ok"]
    _clore(r1, fait=fait)
    out = _jobs(op="complete", job_id=job["id"], ok=False, run_id=r1,
                error="l'agent a échoué")
    assert out["status"] == "pending", out
    return int(job["id"]), r1


# ── ① run clos : servi sans run, trace écrite, puis un run NEUF se lie ─────────

def test_un_start_sur_run_clos_se_sert_sans_run_et_trace_le_detachement(live):
    from oto_mcp import db
    job_id, r1 = _vol_rate()
    assert _jobs(op="claim")["job"] is None, (
        "backoff : la file ne ressert pas le travail dans la foulée de sa "
        "conclusion — c'est ce qui laisse au fait `run_finish` le temps de s'inscrire")
    _backoff_ecoule(job_id)

    t2 = _prendre()
    assert t2["id"] == job_id and t2["attempts"] == 2, t2
    assert t2.get("run_id") is None, (
        f"le run {r1} est clos : la tentative 2 en ouvre un neuf — servi "
        f"{t2.get('run_id')!r}")
    relu = _relu(job_id)
    assert relu["run_id"] is None, "le détachement est PERSISTÉ, pas seulement servi"
    trace = _trace(relu)
    assert [(e["run_id"], e["tentative"], e["raison"]) for e in trace] == \
        [(r1, 1, "run_clos")], trace
    assert trace[0]["a"], trace
    assert db.run_closed_at(r1) is not None, "l'ancien run et sa clôture restent intacts"

    r2 = _run_ouvert()
    assert _jobs(op="bind_run", job_id=job_id, run_id=r2)["ok"]
    assert _relu(job_id)["run_id"] == r2 != r1


# ── ② la ligne libérée à T1 se réserve à nouveau à T2, sous le run neuf ─────────

_OUTILS: dict = {}


def _outil(nom: str):
    """Ce que charge le BOOT (`register_all`), pas un module seul."""
    if not _OUTILS:
        from fastmcp import FastMCP

        from oto_mcp.tools import register_all
        m = FastMCP("t-run-clos")
        register_all(m)
        _OUTILS["data_claim_next"] = asyncio.run(m.get_tool("data_claim_next"))
    return _OUTILS[nom]


def _reserver_ligne(ns: str, run: str) -> dict:
    """La réservation comme elle arrive en production : `_run_id=` lu des arguments
    BRUTS par le middleware, posé, retiré, puis l'outil dispatché."""
    from oto_mcp.middleware.call_context import CallContextMiddleware

    outil = _outil("data_claim_next")

    class _Msg:
        pass

    class _Ctx:
        pass

    msg = _Msg()
    msg.name = "data_claim_next"
    msg.arguments = {"datastore": ns, "worker": run, "filter": {"statut": "a_enrichir"},
                     "lease_s": 600, "_run_id": run}
    ctx = _Ctx()
    ctx.message = msg

    async def _next(c):
        return await outil.run(c.message.arguments)

    async def _go():
        return await CallContextMiddleware().on_call_tool(ctx, _next)

    return asyncio.run(_go()).structured_content


def _bail(ns_id, row_id) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return dict(conn.execute(
            "SELECT claimed_by, claimed_run FROM datastore_rows "
            "WHERE ns_id = %s AND row_id = %s", (ns_id, row_id)).fetchone() or {})


def test_la_ligne_liberee_a_t1_se_reserve_a_nouveau_a_t2(surface):
    from oto_mcp import db
    ns_id = db.create_datastore("user", SUB, "file-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    db.datastore_insert_row(ns_id, "r0", {"siren": "551110000", "statut": "a_enrichir"})

    _jobs(op="enqueue", kind="start",
          payload={"procedure": "p-run-clos", "input": "fais le travail"})
    job_id = int(_prendre()["id"])
    r1 = _run_ouvert()
    assert _jobs(op="bind_run", job_id=job_id, run_id=r1)["ok"]
    ligne = _reserver_ligne(ns, r1)["row"]
    assert ligne and _bail(ns_id, ligne["_id"])["claimed_run"] == r1

    _clore(r1)
    out = _jobs(op="complete", job_id=job_id, ok=False, run_id=r1, error="échec")
    assert out["rows_released"] == 1, out
    assert _bail(ns_id, ligne["_id"])["claimed_run"] is None, "T1 : la ligne est libérée"
    _backoff_ecoule(job_id)

    t2 = _prendre()
    assert t2["id"] == job_id and t2.get("run_id") is None, t2
    r2 = _run_ouvert()
    assert _jobs(op="bind_run", job_id=job_id, run_id=r2)["ok"]
    reprise = _reserver_ligne(ns, r2)["row"]
    assert reprise and reprise["_id"] == ligne["_id"], "T2 : la même ligne, réservée à nouveau"
    assert _bail(ns_id, ligne["_id"])["claimed_run"] == r2, "sous le run NEUF"


# ── ③ la trace survit à la conclusion suivante ──────────────────────────────────

def test_la_trace_survit_au_succes_suivant(live):
    job_id, r1 = _vol_rate()
    _backoff_ecoule(job_id)
    _prendre()
    r2 = _run_ouvert()
    assert _jobs(op="bind_run", job_id=job_id, run_id=r2)["ok"]

    out = _jobs(op="complete", job_id=job_id, ok=True, run_id=r2,
                result={"usage_tokens": 10})
    assert out["status"] == "done", out
    relu = _relu(job_id)
    assert relu["run_id"] == r2, relu
    assert [e["run_id"] for e in _trace(relu)] == [r1], relu["payload"]


# ── ④ témoin : un bail mort sur un run OUVERT garde son run ─────────────────────

def test_un_bail_mort_sur_run_ouvert_garde_son_run_sans_trace(live):
    _jobs(op="enqueue", kind="start",
          payload={"procedure": "p-run-clos", "input": "fais le travail"})
    job_id = int(_prendre()["id"])
    r1 = _run_ouvert()                 # jamais clos : l'agent est mort en plein tour
    assert _jobs(op="bind_run", job_id=job_id, run_id=r1)["ok"]
    _bail_mort(job_id)

    reprise = _prendre()
    assert reprise["id"] == job_id and reprise["attempts"] == 2, reprise
    assert reprise["run_id"] == r1, "un run OUVERT se reprend : son fil est vivant"
    relu = _relu(job_id)
    assert relu["run_id"] == r1 and "_plateforme" not in (relu["payload"] or {}), relu


# ── ⑤ témoin : un `continue` sur un run clos est inchangé ───────────────────────

def test_un_continue_sur_run_clos_garde_son_run(live):
    r1 = _run_ouvert()
    _clore(r1)
    _jobs(op="enqueue", kind="continue", run_id=r1, payload={"input": "et ensuite ?"})

    job = _prendre()
    assert job["kind"] == "continue" and job["run_id"] == r1, job
    assert "_plateforme" not in (_relu(job["id"])["payload"] or {})


# ── ⑥ le rejeu ne duplique pas la trace ─────────────────────────────────────────

def test_le_rejeu_ne_duplique_pas_la_trace(live):
    from oto_mcp import db
    job_id, r1 = _vol_rate(max_attempts=5)
    _backoff_ecoule(job_id)
    assert _prendre()["run_id"] is None

    # Deux prises successives sans nouveau run : rien à détacher la seconde fois.
    assert _jobs(op="complete", job_id=job_id, ok=False, error="encore")["status"] == "pending"
    _backoff_ecoule(job_id)
    assert _prendre()["run_id"] is None
    assert [e["run_id"] for e in _trace(_relu(job_id))] == [r1]

    # Le MÊME état rejoué : le run clos se retrouve lié, le bail meurt, on reprend.
    assert db.bind_job_run(job_id, WORKER, r1)
    _bail_mort(job_id)
    rejeu = _prendre()
    assert rejeu["run_id"] is None, rejeu
    trace = _trace(_relu(job_id))
    assert [e["run_id"] for e in trace] == [r1], f"une entrée par run, jamais deux — {trace}"


# ── ⑦ la clôture se lit du FAIT journalisé ──────────────────────────────────────

def test_la_cloture_se_lit_du_fait_journalise(live):
    """L'index `runs` dit « clos » (`finish_run`), mais le fait `run_finish` n'est pas
    inscrit : rien n'est détaché. L'index est une écriture de confort jamais lue ; la
    clôture est le fait. En nominal le fait est là bien avant la reprise (cas ① :
    backoff) ; s'il manque pour de bon (journalisation en échec), le travail est
    repris sur son run comme avant ce correctif."""
    job_id, r1 = _vol_rate(fait=False)
    _backoff_ecoule(job_id)
    t2 = _prendre()
    assert t2["run_id"] == r1 and _trace(_relu(job_id)) == [], t2

    # Le fait arrive : la prise suivante détache.
    _clore(r1)
    assert _jobs(op="complete", job_id=job_id, ok=False, run_id=r1,
                 error="encore")["status"] == "pending"
    _backoff_ecoule(job_id)
    t3 = _prendre()
    assert t3["run_id"] is None, t3
    assert [(e["run_id"], e["tentative"]) for e in _trace(_relu(job_id))] == [(r1, 2)]


# ── ⑧ le champ réservé ne s'enfile pas ──────────────────────────────────────────

def test_un_client_ne_pose_pas_le_champ_reserve(live):
    from oto_mcp import db
    faux = {"runs_detaches": [{"run_id": "r-invente", "tentative": 9,
                               "raison": "run_clos", "a": "hier"}]}

    res = _jobs(op="enqueue", kind="start",
                payload={"procedure": "p", "input": "x", "_plateforme": faux})
    relu = _relu(res["id"])
    assert "_plateforme" not in relu["payload"], (
        "par la route : le champ réservé est RETIRÉ à l'enfilement — le serveur "
        f"seul l'écrit ({relu['payload']!r})")
    assert relu["payload"]["input"] == "x"

    direct = db.enqueue_job(ORG, "start", payload={"input": "y", "_plateforme": faux})
    assert "_plateforme" not in (_relu(direct["id"])["payload"] or {}), \
        "au db-layer aussi : tout enfileur passe par là"
