"""#639 — sans axe `_org`, un appel fait DANS un run se résout dans l'org du run.

Mesuré en production le 29/08/2026 (#631/#638) : un `data_write` sans `_org`, fait dans
un run ouvert sur l'org 226, a été résolu dans l'org 2 — l'org MAISON du sub — et refusé
« datastore inconnu » sur un tableau que la réservation du même run venait de résoudre.
82 refus de cette famille sur sept jours, 109 sur les sept jours suivants, tous des
`data_write` du runner. Décidé par Alexis le 30/08 : **l'org d'un appel qui porte un run
et aucun `_org` est `runs.org_id`**, pas la maison.

Ce qu'on garde vert, sur le chemin SERVI (middleware + outil monté, `_run_id` lu des
arguments bruts, vrai PostgreSQL, runs réels dans `runs`) :

  1. sans `_org`, dans un run d'org X, par un sub dont la maison est Y → résolu en X ;
     et le journal stampe X (la même expression que le sink, dans la vraie chaîne) ;
  2. `_org=Z` explicite garde la priorité — l'agent multi-org ne change pas ;
  3. le run appartient à son propriétaire (F-014, 12/10/2026) : un collègue membre de X,
     comme un étranger, ne gagne rien avec son `_run_id` — refus NOMMÉ, qui ne dit pas
     l'org, jamais un repli silencieux sur la maison ;
  4. hors run, la maison reste le défaut ; un run inconnu de `runs` (ou sans org) ne
     pose rien — `_run_id` y reste ce qu'il était, un identifiant de corrélation ;
  5. une lecture de `runs` par run, pas une par appel ni une par seam.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

SUB = "sub-runner-639"
ETRANGER = "sub-etranger-639"
COLLEGUE = "sub-collegue-639"


@pytest.fixture(scope="module")
def orgs(live) -> dict:
    """Trois orgs RÉELLES du même sub — `maison` (active, comme l'org 2 en prod),
    `travail` (celle du run, comme l'org 226) et `autre` (pour l'axe explicite) — un
    collègue membre de `travail`, et un étranger qui a sa propre org, membre ni de
    `travail` ni d'`autre`."""
    from oto_mcp import org_store
    maison = org_store.create_org("Maison 639", created_by=SUB)
    travail = org_store.create_org("Travail 639", created_by=SUB)
    autre = org_store.create_org("Autre 639", created_by=SUB)
    for o in (maison, travail, autre):
        org_store.add_org_member(o, SUB)
    assert org_store.set_active_org(SUB, maison)
    assert org_store.get_active_org(SUB) == maison
    org_store.add_org_member(travail, COLLEGUE)
    ailleurs = org_store.create_org("Ailleurs 639", created_by=ETRANGER)
    org_store.add_org_member(ailleurs, ETRANGER)
    assert org_store.set_active_org(ETRANGER, ailleurs)
    return {"maison": maison, "travail": travail, "autre": autre, "ailleurs": ailleurs}


def _acteur(monkeypatch, sub: str) -> None:
    """Les outils `data_*` tels que le serveur les monte, l'acteur tenu — et le sub
    que lisent les gardes des axes (`_org=`, l'org du run)."""
    from oto_mcp import call_axes
    from oto_mcp.datastore.core import make_store
    from oto_mcp.tools import datastore as T
    monkeypatch.setattr(T, "_acting_store", lambda: make_store(sub))
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    monkeypatch.setattr(T, "_project_hint", lambda ns: None)
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: sub)


@pytest.fixture
def surface(orgs, monkeypatch):
    _acteur(monkeypatch, SUB)
    return orgs


_OUTILS: dict = {}


def _outil(nom: str):
    """Ce que charge le BOOT (`register_all`), pas un module seul."""
    if not _OUTILS:
        from fastmcp import FastMCP

        from oto_mcp.tools import register_all
        m = FastMCP("t-639")
        register_all(m)
        for n in ("data_write",):
            _OUTILS[n] = asyncio.run(m.get_tool(n))
    return _OUTILS[nom]


def _table(org_id: int) -> tuple[str, int]:
    from oto_mcp import db
    ns_id = db.create_datastore("org", str(org_id), "campagne-" + uuid.uuid4().hex[:6])
    db.set_datastore_schema(ns_id, {"fields": [{"key": "siren"}, {"key": "statut"}]})
    db.datastore_insert_row(ns_id, "r0", {"siren": "551110001", "statut": "a_faire"})
    return str(ns_id), ns_id



def _run(org_id, sub: str = SUB) -> str:
    """Un run tel que `run_start` le pose : une ligne `runs` avec son org."""
    from oto_mcp import db
    run_id = uuid.uuid4().hex
    db.insert_run(run_id, sub=sub, org_id=org_id, label="t-639")
    return run_id


def _ecrire(arguments: dict):
    """`data_write` comme il arrive en production : `_run_id=` (et l'éventuel `_org=`)
    lus des arguments BRUTS par le middleware, posés, retirés, puis l'outil dispatché."""
    from oto_mcp.middleware.call_context import CallContextMiddleware

    outil = _outil("data_write")

    class _Msg:
        pass

    class _Ctx:                     # comme le vrai MiddlewareContext : PAS de get_state
        pass

    msg = _Msg()
    msg.name = "data_write"
    msg.arguments = dict(arguments)
    ctx = _Ctx()
    ctx.message = msg

    async def _next(c):
        return await outil.run(c.message.arguments)

    async def _go():
        return await CallContextMiddleware().on_call_tool(ctx, _next)

    return asyncio.run(_go()).structured_content


def _refus(arguments: dict) -> str:
    with pytest.raises(Exception) as e:
        _ecrire(arguments)
    return str(e.value)


def _valeur(ns_id: int, row_id: str, champ: str):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        row = conn.execute(
            "SELECT data FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id)).fetchone()
    return (row or {}).get("data", {}).get(champ)


# ── 1. sans `_org`, l'appel se résout dans l'org du run ───────────────────────

def test_sans_org_dans_un_run_l_appel_se_resout_dans_l_org_du_run(surface):
    """Le geste exact du 29/08 21:11:23, SANS réservation (le contournement de #638
    ne joue pas) : `data_write(datastore=<nom>, id=<ligne>)` avec `_run_id` d'un run
    de l'org `travail`, par un sub dont la maison est `maison`."""
    ns, ns_id = _table(surface["travail"])
    run = _run(surface["travail"])

    out = _ecrire({"datastore": ns, "id": "r0", "row": {"statut": "fait"},
                   "_run_id": run})
    assert out["_id"] == "r0", out
    assert _valeur(ns_id, "r0", "statut") == "fait"


@pytest.mark.asyncio
async def test_le_journal_stampe_l_org_du_run(surface):
    """La VRAIE chaîne (contexte d'appel, puis journal) : le sink relit
    `access.current_org(sub)` depuis la tâche d'insertion, où le contexte a été copié
    au `create_task` — c'est là que #630 lisait l'org maison. On exerce l'expression
    exacte du sink, dans l'ordre de prod, sur un outil de la surface de travail."""
    from fastmcp import Client, FastMCP

    from oto_mcp import access
    from oto_mcp.calllog import ToolCallLogger
    from oto_mcp.middleware.call_context import CallContextMiddleware

    run = _run(surface["travail"])
    rows: list[dict] = []
    vu: dict = {}

    mcp = FastMCP("t-639-journal")

    @mcp.tool()
    def data_sonde() -> dict:
        vu["org"] = access.current_org(SUB)
        return {"ok": True}

    async def sink(row: dict) -> None:
        if row.get("kind") == "protocol":
            return
        row["org_id"] = access.current_org(row.get("sub"))     # server._calllog_sink
        rows.append(row)

    mcp.add_middleware(CallContextMiddleware())
    async def identite():
        return {"sub": SUB}

    mcp.add_middleware(ToolCallLogger(sink, server="t", identity=identite))

    async with Client(mcp) as c:
        await c.call_tool("data_sonde", {"_run_id": run})
    assert vu["org"] == surface["travail"], vu
    assert [r["org_id"] for r in rows] == [surface["travail"]], rows


# ── 2. `_org` explicite garde la priorité ─────────────────────────────────────

def test_l_axe_org_explicite_garde_la_priorite(surface):
    """L'agent multi-org (run ouvert dans une org, travail dans une autre avec `_org=`)
    ne change pas : l'appel est résolu dans l'org de l'axe, pas dans celle du run."""
    ns_autre, ns_id = _table(surface["autre"])
    ns_travail, _ = _table(surface["travail"])
    run = _run(surface["travail"])

    out = _ecrire({"datastore": ns_autre, "id": "r0", "row": {"statut": "fait"},
                   "_org": surface["autre"], "_run_id": run})
    assert out["_id"] == "r0" and _valeur(ns_id, "r0", "statut") == "fait"

    msg = _refus({"datastore": ns_travail, "id": "r0", "row": {"statut": "fait"},
                  "_org": surface["autre"], "_run_id": run})
    assert f"org {surface['autre']}" in msg and f"`_org={surface['travail']}`" in msg, msg


# ── 3. le run d'autrui : refus nommé, qui ne dit pas l'org ────────────────────

@pytest.mark.parametrize("tiers", [ETRANGER, COLLEGUE])
def test_le_run_d_autrui_est_refuse_nommement(orgs, monkeypatch, tiers):
    """Le jeton d'un run n'est pas un axe de droits : un tiers qui le connaît — étranger,
    ou collègue membre de l'org du run (F-014, 12/10/2026) — n'y entre pas, avec ou sans
    `_org=`. Et il n'est pas non plus renvoyé en silence vers sa maison — le refus dit
    que le run n'est pas le sien, sans dire où il se déroule."""
    _acteur(monkeypatch, tiers)
    ns, ns_id = _table(orgs["travail"])
    run = _run(orgs["travail"])

    for axe in ({}, {"_org": orgs["travail"]}):
        if axe and tiers == ETRANGER:
            continue  # l'étranger est refusé par l'axe `_org=` lui-même
        msg = _refus({"datastore": ns, "id": "r0", "row": {"statut": "vole"},
                      "_run_id": run, **axe})
        assert "pas le tien" in msg and run in msg, msg
        assert f"org {orgs['travail']}" not in msg and "Travail 639" not in msg, msg
    assert _valeur(ns_id, "r0", "statut") == "a_faire"


# ── 4. hors run, et run inconnu : la maison reste le défaut ───────────────────

def test_hors_run_la_maison_reste_le_defaut(surface):
    ns_travail, _ = _table(surface["travail"])
    ns_maison, ns_id = _table(surface["maison"])

    msg = _refus({"datastore": ns_travail, "id": "r0", "row": {"statut": "fait"}})
    assert f"`_org={surface['travail']}`" in msg, msg

    out = _ecrire({"datastore": ns_maison, "id": "r0", "row": {"statut": "fait"}})
    assert out["_id"] == "r0" and _valeur(ns_id, "r0", "statut") == "fait"


def test_un_run_inconnu_ou_sans_org_ne_pose_rien(surface):
    """`_run_id` d'un run absent de `runs`, ou d'un run hors org : l'identifiant reste
    une corrélation, l'appel se résout comme avant (maison) — et le refus garde son
    indice."""
    ns, _ = _table(surface["travail"])

    msg = _refus({"datastore": ns, "id": "r0", "row": {"statut": "fait"},
                  "_run_id": uuid.uuid4().hex})
    assert f"`_org={surface['travail']}`" in msg, msg

    msg = _refus({"datastore": ns, "id": "r0", "row": {"statut": "fait"},
                  "_run_id": _run(None)})
    assert f"`_org={surface['travail']}`" in msg, msg


# ── 5. une lecture par run ────────────────────────────────────────────────────

def test_une_lecture_de_runs_par_run_pas_par_appel(surface, monkeypatch):
    """`runs.org_id` est immuable : deux appels du même run lisent la table une fois.
    Le seam `current_org`, lui, ne la lit jamais — il relit ce que le middleware a posé."""
    from oto_mcp import db
    ns, ns_id = _table(surface["travail"])
    run = _run(surface["travail"])
    lu: list[str] = []
    vrai = db.get_run_head
    monkeypatch.setattr(db, "get_run_head", lambda r: lu.append(r) or vrai(r))

    _ecrire({"datastore": ns, "id": "r0", "row": {"statut": "un"}, "_run_id": run})
    _ecrire({"datastore": ns, "id": "r0", "row": {"statut": "deux"}, "_run_id": run})
    assert _valeur(ns_id, "r0", "statut") == "deux"
    assert lu == [run], lu


# ── 6. `oto_call` (dispatch hors middleware) suit la même règle ───────────────

@pytest.mark.asyncio
async def test_oto_call_dispatche_aussi_dans_l_org_du_run(surface, monkeypatch):
    """`oto_call` rejoue les axes HORS middleware (ADR 0036) : un outil de connecteur
    dispatché avec `_run_id` dans `arguments` et sans `_org` se résout, lui aussi, dans
    l'org du run — sinon l'échappatoire aurait gardé l'ancien défaut. (`data_*` n'est
    pas dispatchable par `oto_call` : la sonde porte le datastore d'un connecteur.)"""
    from fastmcp import Client, FastMCP

    from oto_mcp import access
    from oto_mcp.middleware.call_context import CallContextMiddleware
    from oto_mcp.tools import meta, register_all
    monkeypatch.setattr(meta, "current_user_sub_from_token", lambda: SUB)

    run = _run(surface["travail"])
    mcp = FastMCP("t-639-oto-call")
    register_all(mcp)

    @mcp.tool()
    def serper_sonde() -> dict:
        return {"org": access.current_org(SUB)}

    mcp.add_middleware(CallContextMiddleware())
    async with Client(mcp) as c:
        res = await c.call_tool("oto_call", {"name": "serper_sonde",
                                             "arguments": {"_run_id": run}})
    assert res.structured_content["org"] == surface["travail"], res
