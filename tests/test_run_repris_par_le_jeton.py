"""Un agent hébergé garde son run quand sa session MCP change (27/10/2026).

Un agent hébergé (CLI derrière le relais du runner, jeton de DÉLÉGATION d'un travail)
ouvre un run par `run_start` ; ses appels y sont rattachés par la pile de runs de la
SESSION. Un appel long coupé côté client relance le relais : session neuve, pile vide,
et chaque appel suivant partait au journal avec `run_id` NULL — la timeline du run
s'arrêtait là pendant que l'agent travaillait encore près d'une demi-heure. Le relais
retire `_run_id` des schémas servis au modèle : l'agent ne pouvait pas le repasser.

Deux étages :
- `db.run_ouvert_du_jeton`, contre un vrai PostgreSQL (DDL réel) : le run ouvert par CE
  jeton de délégation, pas clos — jamais celui d'un autre jeton, d'un autre compte, d'un
  jeton d'utilisateur, ni d'avant le jeton ;
- la reprise par le middleware, sur un vrai serveur FastMCP et DEUX sessions : la
  seconde lit le run de la première, une fois, et `run_finish` l'y dépile.
"""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastmcp import Context

# Importés AVANT tout patch du lecteur d'identité : ils le lient à leur import, et un
# import pendant le test lierait le faux pour tout le worker (#1111).
import oto_mcp.capabilities._mcp_adapter  # noqa: F401,E402
import oto_mcp.tools.meta  # noqa: F401,E402
from oto_mcp.db import _schema, usage


def _real_ddl(table: str) -> str:
    m = re.search(rf"^CREATE TABLE IF NOT EXISTS {table} \(.*?^\);",
                  _schema._SCHEMA, re.S | re.M)
    assert m, f"DDL de `{table}` introuvable dans _schema.py"
    return m.group(0)


@pytest.fixture()
def conn(pg_module_dsn, monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    with psycopg.connect(pg_module_dsn, row_factory=dict_row, autocommit=True) as c:
        for t in ("user_api_tokens", "tool_calls", "users"):
            c.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
        for t in ("users", "tool_calls", "user_api_tokens"):
            c.execute(_real_ddl(t))
        c.execute("INSERT INTO users (sub, email) VALUES ('u1', 'u1@acme.test'), "
                  "('u2', 'u2@acme.test')")

        @contextmanager
        def _connect_test():
            yield c

        monkeypatch.setattr(usage, "_connect", _connect_test)
        yield c


def _ago(minutes: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def _jeton(conn, *, sub="u1", kind="delegation", ago=60.0, org=None) -> int:
    """`org` : l'org du travail (`verrou_org_id`), celle où l'agent ouvre ses runs."""
    return conn.execute(
        "INSERT INTO user_api_tokens (sub, label, token_hash, created_at, kind, "
        "verrou_org, verrou_org_id) "
        "VALUES (%s, 'runner job', md5(random()::text), %s, %s, %s, %s) RETURNING id",
        (sub, _ago(ago), kind, org is not None or None, org)).fetchone()["id"]


def _fait(conn, tool, run_id, args, *, sub="u1", token_id=None, ago=10.0, org=None):
    conn.execute(
        "INSERT INTO tool_calls (created_at, kind, sub, tool, args, run_id, token_id, "
        "token_kind, org_id) VALUES (%s, 'mcp', %s, %s, %s::jsonb, %s, %s, %s, %s)",
        (_ago(ago), sub, tool, json.dumps(args), run_id, token_id,
         "delegation" if token_id is not None else None, org))


def _ouvrir(conn, run_id, **kw):
    _fait(conn, "run_start", run_id, {"label": "travail"}, **kw)


def _clore(conn, run_id, *, sub="u1", token_id=None, ago=5.0):
    _fait(conn, "run_finish", run_id, {"run_id": run_id, "outcome": "done"}, sub=sub,
          token_id=token_id, ago=ago)


# ── La lecture : le run ouvert par CE jeton ─────────────────────────────────

def test_le_run_ouvert_par_le_jeton_est_retrouve(conn):
    j = _jeton(conn)
    _ouvrir(conn, "r1", token_id=j)
    assert usage.run_ouvert_du_jeton("u1", j) == "r1"


def test_un_run_clos_n_est_plus_repris(conn):
    j = _jeton(conn)
    _ouvrir(conn, "r1", token_id=j, ago=10.0)
    _clore(conn, "r1", token_id=j, ago=5.0)
    assert usage.run_ouvert_du_jeton("u1", j) is None


def test_le_run_d_un_autre_jeton_n_est_jamais_repris(conn):
    mien, autre = _jeton(conn), _jeton(conn)
    _ouvrir(conn, "r-autre", token_id=autre)
    assert usage.run_ouvert_du_jeton("u1", mien) is None


def test_le_jeton_d_un_autre_compte_ne_rend_rien(conn):
    j = _jeton(conn, sub="u2")
    _ouvrir(conn, "r2", sub="u2", token_id=j)
    assert usage.run_ouvert_du_jeton("u1", j) is None


def test_un_jeton_d_utilisateur_ne_suit_pas_d_une_session_a_l_autre(conn):
    """Un jeton d'utilisateur sert des sessions sans lien (une CI, Slack…) : un run
    ouvert par l'une ne s'impose pas aux autres."""
    j = _jeton(conn, kind="user")
    _ouvrir(conn, "r1", token_id=j)
    assert usage.run_ouvert_du_jeton("u1", j) is None


def test_une_ouverture_anterieure_au_jeton_est_ignoree(conn):
    j = _jeton(conn, ago=5.0)
    _ouvrir(conn, "r-vieux", token_id=j, ago=30.0)
    assert usage.run_ouvert_du_jeton("u1", j) is None


def test_le_run_est_cherche_dans_l_org_du_travail(conn):
    """Le jeton d'un travail est borné à son org : son run s'y ouvre, et la recherche
    s'y borne — c'est ce qui fait servir l'index par ses trois colonnes de tête."""
    j = _jeton(conn, org=7)
    _ouvrir(conn, "r-ailleurs", token_id=j, org=8, ago=5.0)
    assert usage.run_ouvert_du_jeton("u1", j) is None
    _ouvrir(conn, "r-ici", token_id=j, org=7, ago=10.0)
    assert usage.run_ouvert_du_jeton("u1", j) == "r-ici"


def test_la_recherche_est_servie_par_l_index_des_ouvertures_jusqu_a_l_org(conn):
    """Le plan : `idx_tool_calls_run_start_sub` avec `sub`, `org_id` ET `created_at` en
    condition d'index — sans l'org, la colonne du milieu manque et PG parcourt tous les
    `run_start` du compte sur la rétention."""
    from oto_mcp.db import index_releve
    [ix] = [i for i in index_releve.OUVERTURES if i.nom == "idx_tool_calls_run_start_sub"]
    conn.execute(f"CREATE INDEX {ix.nom} ON tool_calls {ix.forme}")
    j = _jeton(conn, org=7)
    _ouvrir(conn, "r1", token_id=j, org=7)
    vus = []
    reel = conn.execute

    def espion(requete, *a, **k):
        if "FROM tool_calls s" in str(requete):
            conn.execute("SET enable_seqscan = off")
            vus.append(reel("EXPLAIN " + str(requete), *a, **k).fetchall())
        return reel(requete, *a, **k)

    class _Espion:
        def __getattr__(self, nom):
            return espion if nom == "execute" else getattr(conn, nom)

    @contextmanager
    def _connect_espion():
        yield _Espion()

    usage._connect, avant = _connect_espion, usage._connect
    try:
        assert usage.run_ouvert_du_jeton("u1", j) == "r1"
    finally:
        usage._connect = avant
        conn.execute("RESET enable_seqscan")
    lignes = [next(iter(r.values())) for r in vus[0]]
    assert any(ix.nom in ligne for ligne in lignes), lignes
    [cond] = [ligne for ligne in lignes if "Index Cond" in ligne]
    assert "org_id" in cond and "created_at" in cond, lignes


def test_des_runs_imbriques_rendent_le_plus_recent_encore_ouvert(conn):
    j = _jeton(conn)
    _ouvrir(conn, "exterieur", token_id=j, ago=20.0)
    _ouvrir(conn, "interieur", token_id=j, ago=15.0)
    _clore(conn, "interieur", token_id=j, ago=10.0)
    assert usage.run_ouvert_du_jeton("u1", j) == "exterieur"
    _ouvrir(conn, "troisieme", token_id=j, ago=2.0)
    assert usage.run_ouvert_du_jeton("u1", j) == "troisieme"


# ── La reprise : une session neuve, le même travail ─────────────────────────

def _serveur(monkeypatch, *, kind="delegation", run_du_jeton="r1"):
    from fastmcp import FastMCP

    from oto_mcp import db, guide_run
    from oto_mcp.auth import hooks
    from oto_mcp.middleware.call_context import CallContextMiddleware
    from oto_mcp.tools import guide_run as outils_run
    monkeypatch.setattr(hooks, "current_token_axes",
                        lambda: {"token_id": 7, "token_kind": kind})
    monkeypatch.setattr(hooks, "current_user_sub_from_token", lambda: "u1")
    lectures = []

    def lire(sub, token_id):
        lectures.append((sub, token_id))
        return run_du_jeton

    monkeypatch.setattr(db, "run_ouvert_du_jeton", lire)
    m = FastMCP("t-run-repris")
    m.add_middleware(CallContextMiddleware())
    outils_run.register(m)

    @m.tool()
    async def sonde(ctx: Context) -> str:
        """Ce que lisent le journal (`server._calllog_sink`) et la cible d'`oto_call`."""
        return str(await guide_run.active_run_id(ctx))
    return m, lectures


async def _sonder(client) -> str:
    return (await client.call_tool("sonde")).content[0].text


def test_une_session_neuve_reprend_le_run_du_jeton(monkeypatch):
    from fastmcp import Client
    m, lectures = _serveur(monkeypatch)

    async def scenario():
        async with Client(m) as neuve:
            vus = [await _sonder(neuve), await _sonder(neuve)]
            fin = await neuve.call_tool("run_finish", {"run_id": "r1", "outcome": "done"})
            return vus, fin.structured_content, await _sonder(neuve)

    vus, fin, apres = asyncio.run(scenario())
    assert vus == ["r1", "r1"]
    # Cherché UNE fois par session, pas à chaque appel.
    assert lectures == [("u1", 7)]
    # Repris sur la pile de la session : `run_finish` l'y dépile, la suite est hors run.
    assert fin["was_open"] is True and apres == "None"


def test_une_session_qui_tient_deja_un_run_ne_cherche_rien(monkeypatch):
    from fastmcp import Client
    m, lectures = _serveur(monkeypatch, run_du_jeton=None)

    async def scenario():
        async with Client(m) as c:
            ouvert = await c.call_tool("run_start", {"label": "ici"})
            return ouvert.structured_content["run_id"], await _sonder(c)

    run_id, vu = asyncio.run(scenario())
    assert vu == run_id
    # Le premier appel (run_start) a cherché, rien n'était à reprendre : la recherche ne
    # se refait plus, et la pile de la session fait foi.
    assert lectures == [("u1", 7)]


def test_un_jeton_d_utilisateur_ne_reprend_rien(monkeypatch):
    from fastmcp import Client
    m, lectures = _serveur(monkeypatch, kind="user")

    async def scenario():
        async with Client(m) as c:
            return await _sonder(c)

    assert asyncio.run(scenario()) == "None"
    assert lectures == []


def test_sans_run_ouvert_la_session_reste_hors_run_et_ne_recherche_plus(monkeypatch):
    from fastmcp import Client
    m, lectures = _serveur(monkeypatch, run_du_jeton=None)

    async def scenario():
        async with Client(m) as c:
            return [await _sonder(c) for _ in range(3)]

    assert asyncio.run(scenario()) == ["None"] * 3
    assert lectures == [("u1", 7)]
