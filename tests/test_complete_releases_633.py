"""#633 — conclure un job du runner (`POST /api/me/runner/jobs` op=complete) libère
ce que son run tenait dans le datastore, et le DIT : `rows_released` avec `0`
explicite, `null` + raison quand rien n'a été tenté.

Mesuré le 29/08/2026 sur une campagne : un poste de flotte lit « le témoin que la
clôture du travail rend » — or `complete` ne libérait rien et rendait
`{"ok": true, "status": …}` sans compte. La libération ne jouait que sur
`run_finish`, l'appel de l'AGENT, qui rendait `rows_released` seulement s'il y
avait au moins une ligne (absent = zéro). Un agent mort sans `run_finish` laissait
sa ligne au bail jusqu'à expiration ; `complete`, l'appel du WORKER qui survit à
l'agent, n'y changeait rien.

Prouvé ici sur le chemin réel : la réservation posée par `data_claim_next` tel qu'il
est monté (middleware `_run_id=` + outil enregistré par `register_all`), puis la
capacité `runner.jobs` telle que la route REST l'appelle (`_jobs`, vrai
`db.complete_job`), contre un PostgreSQL jetable.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from oto_mcp.capabilities import runner_jobs as RJ
from oto_mcp.capabilities._types import ResolvedCtx

SUB = "sub-flotte-633"
ORG = 633
WORKER = "worker-633"


@pytest.fixture(scope="module")
def live(pg_dsn):
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_complete633_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name

    prev_url, prev_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        # ⚠️ Le porteur d'un travail doit EXISTER et être membre de son org — c'est
        # le cas en production (l'enfilage est réservé aux membres), et depuis la
        # délégation le serveur le VÉRIFIE à la réservation. Un harnais qui ne le
        # modélisait pas décrivait un état impossible, et il a rougi le jour où
        # quelque chose l'a enfin lu.
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


@pytest.fixture
def surface(live, monkeypatch):
    """Les outils `data_*` tels que le serveur les monte, l'acteur tenu."""
    from oto_mcp.datastore.core import make_store
    from oto_mcp.tools import datastore as T
    monkeypatch.setattr(T, "_acting_store", lambda: make_store(SUB))
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    monkeypatch.setattr(T, "_project_hint", lambda ns: None)


_OUTILS: dict = {}


def _outil(nom: str):
    """Ce que charge le BOOT (`register_all`), pas un module seul."""
    if not _OUTILS:
        from fastmcp import FastMCP

        from oto_mcp.tools import register_all
        m = FastMCP("t-633")
        register_all(m)
        _OUTILS["data_claim_next"] = asyncio.run(m.get_tool("data_claim_next"))
    return _OUTILS[nom]


def _run() -> str:
    """Un run RÉEL, indexé : `runner_jobs.run_id` référence `runs` (FK) — un job ne
    se lie qu'à un run qu'un `run_start` a ouvert, ce que le worker fait toujours."""
    from oto_mcp import db
    run_id = uuid.uuid4().hex
    db.insert_run(run_id, sub=WORKER, org_id=ORG, label="campagne-633")
    return run_id


def _table(n: int):
    from oto_mcp import db
    ns_id = db.create_datastore("user", SUB, "file-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    for i in range(n):
        db.datastore_insert_row(ns_id, f"r{i}", {"siren": f"5511100{i}", "statut": "a_enrichir"})
    return ns, ns_id


def _claim(ns: str, run: str) -> dict:
    """La réservation comme elle arrive en production : `_run_id=` lu des arguments
    BRUTS par le middleware, posé, retiré, puis l'outil dispatché."""
    from oto_mcp.middleware.call_context import CallContextMiddleware

    outil = _outil("data_claim_next")

    class _Msg:
        pass

    class _Ctx:                     # comme le vrai MiddlewareContext : PAS de get_state
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
            "SELECT claimed_by, claimed_until, claimed_run FROM datastore_rows "
            "WHERE ns_id = %s AND row_id = %s", (ns_id, row_id)).fetchone() or {})


def _jobs(**kw) -> dict:
    """La capacité telle que la route l'appelle — le worker porte un jeton d'org."""
    return RJ._jobs(ResolvedCtx(sub=WORKER, org_id=ORG), RJ.JobsInput(**kw))


def _job_claime() -> int:
    """Un job `start` enfilé puis pris par le worker — la prise réelle (SKIP LOCKED)."""
    _jobs(op="enqueue", kind="start", payload={"procedure": "p-633"})
    job = _jobs(op="claim")["job"]
    assert job, "la file portait un job : le claim le rend"
    return int(job["id"])


# ── ① l'agent est mort sans `run_finish` : conclure le job rend sa ligne ─────────

@pytest.mark.parametrize("ok, statut", [(True, "done"), (False, "pending")])
def test_conclure_le_job_libere_la_ligne_du_run_de_l_appel(surface, ok, statut):
    """Claim sous `_run_id=R` par le chemin réel, PAS de `run_finish` (l'agent est
    mort), puis `complete(job, run_id=R)` par le worker : la ligne revient libre,
    la réponse porte le compte EXACT, et un autre run peut la reprendre. Quel que
    soit `ok` : un job qui repart en file (backoff) ne travaille plus non plus."""
    ns, ns_id = _table(1)
    run_r = _run()
    job_id = _job_claime()

    ligne = _claim(ns, run_r)["row"]
    assert ligne and _bail(ns_id, ligne["_id"])["claimed_run"] == run_r

    out = _jobs(op="complete", job_id=job_id, ok=ok, run_id=run_r,
                error=None if ok else "l'agent est mort")
    assert out["ok"] is True and out["status"] == statut
    assert out["rows_released"] == 1, (
        "le poste de flotte lit le compte que la clôture du travail rend : "
        f"{out!r}")
    assert out["run_id"] == run_r and out["release"] == "ok", out
    bail = _bail(ns_id, ligne["_id"])
    assert bail["claimed_by"] is None and bail["claimed_run"] is None, bail

    reprise = _claim(ns, _run())["row"]
    assert reprise and reprise["_id"] == ligne["_id"], "la ligne est de nouveau dans la file"


def test_conclure_libere_aussi_le_run_lie_par_bind_run(surface):
    """Le run peut venir du JOB (`bind_run`), pas seulement de l'appel : un worker qui
    conclut sans repasser `run_id` libère quand même ce que ce run tenait."""
    ns, ns_id = _table(1)
    run_r = _run()
    job_id = _job_claime()
    assert _jobs(op="bind_run", job_id=job_id, run_id=run_r)["ok"]

    ligne = _claim(ns, run_r)["row"]
    assert ligne

    out = _jobs(op="complete", job_id=job_id, ok=True)
    assert out["rows_released"] == 1 and out["run_id"] == run_r, out
    assert _bail(ns_id, ligne["_id"])["claimed_run"] is None


def test_conclure_ne_libere_que_ce_que_son_run_tenait(surface):
    """La clause `claimed_run = run` : la ligne d'un AUTRE run reste au bail, et le
    compte le dit — 0, présent."""
    ns, ns_id = _table(1)
    run_r, run_q = _run(), _run()
    job_id = _job_claime()

    ligne = _claim(ns, run_r)["row"]
    assert ligne

    out = _jobs(op="complete", job_id=job_id, ok=True, run_id=run_q)
    assert "rows_released" in out and out["rows_released"] == 0, (
        "« zéro ligne rendue » se distingue de « champ absent » : le 0 est ÉCRIT "
        f"— {out!r}")
    assert out["run_id"] == run_q and out["release"] == "ok", out
    assert _bail(ns_id, ligne["_id"])["claimed_run"] == run_r, "le bail de R est intact"


# ── ② sans run connu : rien n'est libéré, et c'est dit ────────────────────────────

def test_sans_run_connu_rien_n_est_libere_et_la_reponse_le_dit(surface):
    """Ni `run_id` à l'appel, ni `bind_run` : il n'y a rien à libérer PAR RUN. La
    réponse ne fabrique pas un 0 (ce serait « j'ai regardé, il n'y avait rien ») :
    `rows_released` est null et `release` en donne la raison."""
    ns, ns_id = _table(1)
    run_r = _run()
    job_id = _job_claime()

    ligne = _claim(ns, run_r)["row"]        # tenue par un run que le job ne connaît pas
    assert ligne

    out = _jobs(op="complete", job_id=job_id, ok=True)
    assert out["ok"] is True and out["status"] == "done"
    assert "rows_released" in out and out["rows_released"] is None, out
    assert out["release"] == "no_run" and out["run_id"] is None, out
    assert _bail(ns_id, ligne["_id"])["claimed_run"] == run_r, "rien n'a bougé"


# ── ③ best-effort : la libération ne conditionne jamais la clôture du job ───────

def test_une_liberation_qui_echoue_ne_bloque_pas_la_cloture(surface, monkeypatch):
    """Comme `run_finish` : libérer est un service rendu, pas une condition. Si la
    base tousse sur la libération, le job est quand même conclu — et la réponse ne
    ment pas avec un 0 : null, raison `failed`."""
    from oto_mcp import db as d
    job_id = _job_claime()

    def _tousse(run_id, **_kw):
        raise RuntimeError("la base tousse")
    monkeypatch.setattr(RJ.db, "datastore_release_by_run", _tousse)

    out = _jobs(op="complete", job_id=job_id, ok=True, run_id=_run())
    assert out["ok"] is True and out["status"] == "done", out
    assert out["rows_released"] is None and out["release"] == "failed", out
    assert d.get_job(job_id, ORG)["status"] == "done", "le job EST conclu en base"


# ── ④ la forme est DÉCLARÉE : l'Output atteint l'OpenAPI ─────────────────────────

def test_la_forme_de_la_reponse_est_declaree_dans_l_openapi():
    """Un poste de flotte n'a pas à deviner : la 200 de `POST /api/me/runner/jobs`
    nomme `rows_released`, `run_id` et `release` (ADR 0059 — on ne fige que ce qui
    est généré)."""
    from oto_mcp import openapi

    doc = openapi.build()
    schema = (doc["paths"]["/api/me/runner/jobs"]["post"]["responses"]["200"]
              ["content"]["application/json"]["schema"])
    props = schema.get("properties", {})
    assert {"rows_released", "run_id", "release"} <= props.keys(), sorted(props)
    assert "0" in props["rows_released"].get("description", ""), (
        "la description dit que le 0 est explicite — c'est ce qu'un poste lit")
    assert set(props["release"].get("enum") or props["release"].get("anyOf", [{}])[0]
               .get("enum", [])) >= {"ok", "no_run", "failed"}, props["release"]
