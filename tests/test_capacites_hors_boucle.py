"""Garde-fou perf (gel de prod du 2026-09-01) : une CAPACITÉ ne touche pas la base
depuis le thread de l'event loop.

Pourquoi un garde-fou de plus, à côté des deux qui existent :

- `test_no_blocking_async_handlers` énumère les `@mcp.tool` et accepte un `async def`
  dès qu'il porte un `await` dans son propre scope. Les tools montés par la couche
  capacité sont fabriqués par `_mcp_adapter._make_tool` : ils sont `async def` et ils
  `await` (il leur faut la boucle pour un handler async et pour le refresh de
  visibilité). Ils passaient donc le critère — pendant que les 285 handlers SYNC
  qu'ils appellent tournaient dans la boucle ;
- `middleware/test_no_blocking_db_in_middleware` observe le bon thread, mais ne
  regarde que les middlewares.

Ce qui s'est passé : `data_patch_schema` déclare une clé métier, la clé pose un index
UNIQUE, `CREATE INDEX CONCURRENTLY` attend par conception la fin de toute transaction
ouverte avant lui — et une requête d'analyse lancée à la main tournait depuis 47 min.
Douze minutes quarante-huit sans une seule réponse, 376 connexions acceptées par le
noyau et jamais servies. **Une simple lecture avait gelé la production**, parce que le
travail qu'elle retenait tenait la boucle. Cf. `docs/event-loop-perf.md` mode n°4.

Détection : on ne lit pas le source, on OBSERVE le thread — même parti que le
garde-fou des middlewares, et pour la même raison (aucune analyse statique ne décide
où un appel finit par descendre). Deux crans :

1. **le SEAM** (`_make_tool` / `_make_handler`) : ce que les deux adaptateurs font
   d'un handler sync. C'est l'énoncé général — il vaut pour les 285 d'un coup, sans
   liste à tenir ;
2. **un cas RÉEL, sur une VRAIE base** : la capacité de l'incident, montée depuis le
   registre, jouée en entier jusqu'à la pose d'index. On y vérifie que le DDL a
   VRAIMENT été atteint (sinon la garde est inerte et son vert ne vaut rien) et qu'il
   ne l'a pas été depuis la boucle.

Et un contrôle qui MORD : le même travail appelé nûment dans la boucle — la forme
d'avant le correctif — doit être attrapé.
"""
from __future__ import annotations

import asyncio
import inspect
import threading
import uuid
from typing import Optional

import pytest
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse

from oto_mcp.capabilities import _mcp_adapter, _rest_adapter
from oto_mcp.capabilities._types import Capability, ResolvedCtx, RestBinding


# --------------------------------------------------------------------------- #
# Cran 1 — le SEAM : ce que les adaptateurs font d'un handler sync
# --------------------------------------------------------------------------- #

class _Vide(BaseModel):
    pass


def _cap_sonde(vu: dict, *, handler_async: bool = False) -> Capability:
    """Une capacité qui ne fait qu'une chose : dire dans quel thread on l'a jouée.

    Elle est délibérément creuse. Ce qu'on mesure ici n'est pas ce qu'un handler
    fait, c'est où l'adaptateur le met — et sur cette question, un handler creux
    répond exactement comme un handler qui interroge la base."""
    def _autz(raw, inp=None):
        vu["autz"] = threading.current_thread()
        return ResolvedCtx(sub=raw.sub or "u", org_id=None)

    def _sync(ctx, inp):
        vu["handler"] = threading.current_thread()
        return {"ok": True}

    async def _async(ctx, inp):
        vu["handler"] = threading.current_thread()
        return {"ok": True}

    return Capability(key="sonde.hors_boucle", handler=_async if handler_async else _sync,
                      Input=_Vide, authz=_autz, mcp="sonde_hors_boucle",
                      rest=RestBinding(verb="POST", path="/api/sonde"))


def _joue(coro_factory) -> threading.Thread:
    """Joue la coroutine dans une boucle neuve ; rend le thread de CETTE boucle."""
    porteur: dict = {}

    async def _run():
        porteur["boucle"] = threading.current_thread()
        return await coro_factory()

    asyncio.run(_run())
    return porteur["boucle"]


def _handler_rest(cap, binding, sub: str = "sub-test"):
    def _json_error(_req, status, code, message=None, **kw):
        return JSONResponse({"error": code, "detail": message}, status_code=status)

    def _json_response(_req, payload, status=200):
        return JSONResponse(payload, status_code=status)

    async def _auth(_req, _verifier, **_kw):
        return sub, None

    return _rest_adapter._make_handler(cap, binding, None, _auth, _json_response,
                                       _json_error)


async def _post(handler):
    async def _receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    req = Request({"type": "http", "method": "POST", "path": "/api/sonde",
                   "headers": [(b"content-type", b"application/json")],
                   "query_string": b"", "path_params": {}}, receive=_receive)
    return await handler(req)


def test_le_seam_mcp_range_le_handler_sync_hors_de_la_boucle(monkeypatch):
    monkeypatch.setattr(_mcp_adapter, "current_user_sub_from_token", lambda: "u")
    vu: dict = {}
    tool = _mcp_adapter._make_tool(_cap_sonde(vu))
    boucle = _joue(lambda: tool())
    assert vu["handler"] is not boucle, (
        "le handler SYNC d'une capacité a tourné dans le thread de l'event loop : "
        "le serveur est mono-loop, la moindre requête lente y gèle TOUT "
        "(cf. docs/event-loop-perf.md — 12 min 48 s de prod muette le 2026-09-01)")


def test_le_seam_mcp_range_aussi_lautz_hors_de_la_boucle(monkeypatch):
    """L'autz n'est pas un décor : elle marche la cascade des rôles, donc la base.

    Elle était nommée comme reste-à-traiter dans `docs/event-loop-perf.md` depuis le
    15/08 (`_authz.ORG_MEMBER` appelé depuis `_rest_adapter._handler`)."""
    monkeypatch.setattr(_mcp_adapter, "current_user_sub_from_token", lambda: "u")
    vu: dict = {}
    tool = _mcp_adapter._make_tool(_cap_sonde(vu))
    boucle = _joue(lambda: tool())
    assert vu["autz"] is not boucle, (
        "la règle d'autz d'une capacité a tourné dans la boucle — elle interroge la "
        "base (rôles, orgs, cascade de connecteurs) à CHAQUE appel")


def test_le_seam_rest_range_le_handler_sync_hors_de_la_boucle():
    vu: dict = {}
    cap = _cap_sonde(vu)
    handler = _handler_rest(cap, cap.rest_bindings()[0])
    boucle = _joue(lambda: _post(handler))
    assert vu["handler"] is not boucle, (
        "le handler SYNC d'une capacité a tourné dans le thread de l'event loop côté "
        "REST — la face change, le serveur mono-loop est le même")
    assert vu["autz"] is not boucle, "idem pour la règle d'autz"


def test_un_handler_ASYNC_reste_dans_la_boucle(monkeypatch):
    """Le pendant nécessaire : une coroutine n'a rien à faire dans un thread sans
    boucle. Sans ce test, « tout au thread » passerait pour la bonne réponse et
    casserait les 34 capacités réellement asynchrones."""
    monkeypatch.setattr(_mcp_adapter, "current_user_sub_from_token", lambda: "u")
    vu: dict = {}
    tool = _mcp_adapter._make_tool(_cap_sonde(vu, handler_async=True))
    boucle = _joue(lambda: tool())
    assert vu["handler"] is boucle, (
        "un handler `async def` doit être awaité DANS la boucle ; un thread de "
        "threadpool n'a pas de boucle où le jouer")


def test_le_registre_na_pas_dautre_forme_de_handler():
    """Les deux seuls cas que les adaptateurs savent placer : coroutine ou pas.

    Une capacité montée autrement (partial d'une coroutine, objet appelable dont
    `__call__` est async…) tromperait `iscoroutinefunction` au montage et repartirait
    dans la boucle sans que rien ne le dise."""
    from oto_mcp.capabilities.registry import CAPABILITIES
    louches = [c.key for c in CAPABILITIES
               if not inspect.isfunction(c.handler) and not inspect.ismethod(c.handler)]
    assert not louches, (
        f"handler(s) de forme inattendue — `inspect.iscoroutinefunction` décide au "
        f"montage où les placer, vérifier qu'il ne se trompe pas sur : {louches}")


# --------------------------------------------------------------------------- #
# Cran 2 — le cas RÉEL, sur une vraie base : la capacité de l'incident
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def base_jetable(pg_dsn):
    """Une base à nous, montée puis rendue — le chemin doit s'exécuter EN ENTIER.

    Un stub ne conviendrait pas ici : ce qu'on veut voir, c'est le thread au moment
    où le DDL part réellement vers PostgreSQL."""
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    nom = "oto_loop_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + nom

    url_prec, pool_prec = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = pool_prec
        if url_prec is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_prec
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()


class _Mouchards:
    """Note le thread appelant sur les DEUX seams d'accès à la base, puis DÉLÈGUE.

    Déléguer (au lieu de refuser, comme le fait le garde-fou des middlewares) est le
    point : on veut que le chemin aille jusqu'au bout, donc jusqu'au `CREATE INDEX
    CONCURRENTLY`. C'est ce dernier qui a gelé la production, pas les lectures d'avant."""

    def __init__(self, monkeypatch):
        from oto_mcp.db import _conn as dbconn
        from oto_mcp.db import datastore as ds
        self.pool: list[threading.Thread] = []
        self.ddl: list[threading.Thread] = []

        pool_vrai = dbconn._get_pool
        ddl_vrai = ds._connect_autocommit

        def _pool():
            self.pool.append(threading.current_thread())
            return pool_vrai()

        def _ddl(*a, **kw):
            self.ddl.append(threading.current_thread())
            return ddl_vrai(*a, **kw)

        monkeypatch.setattr(dbconn, "_get_pool", _pool)
        # ⚠️ `datastore.py` importe `_connect_autocommit` PAR SON NOM : patcher
        # `_conn._connect_autocommit` ne toucherait pas le nom déjà lié là-bas.
        monkeypatch.setattr(ds, "_connect_autocommit", _ddl)


def _cap_du_registre(cle: str) -> Capability:
    from oto_mcp.capabilities.registry import CAPABILITIES
    for c in CAPABILITIES:
        if c.key == cle:
            return c
    raise AssertionError(f"capacité `{cle}` absente du registre — a-t-elle été renommée ?")


# Ce que `data_patch_schema` recevait le soir de l'incident : une clé métier
# DÉCLARÉE. C'est elle qui déclenche la pose de l'index UNIQUE, donc le
# `CREATE INDEX CONCURRENTLY`.
_PATCH_A_CLE = {"key": "siren",
                "fields": [{"key": "siren", "type": "text"},
                           {"key": "nom", "type": "text"}]}


@pytest.fixture
def tableau(base_jetable):
    from oto_mcp import db
    ns = str(db.create_datastore("user", "sub-loop", "t-" + uuid.uuid4().hex[:6]))
    return ns


def test_la_pose_dindex_de_lincident_ne_part_pas_de_la_boucle(tableau, monkeypatch):
    """Le chemin exact du 2026-09-01, joué en entier : `data_patch_schema` déclare
    une clé métier → index UNIQUE → `CREATE INDEX CONCURRENTLY`.

    C'est CE dernier appel qui, ce soir-là, a attendu 12 min 48 s dans la boucle."""
    monkeypatch.setattr(_mcp_adapter, "current_user_sub_from_token", lambda: "sub-loop")
    mouchards = _Mouchards(monkeypatch)
    tool = _mcp_adapter._make_tool(_cap_du_registre("me.datastore.patch_schema"))

    boucle = _joue(lambda: tool(datastore=tableau, **_PATCH_A_CLE))

    assert mouchards.ddl, (
        "garde INERTE : le chemin n'a jamais atteint le DDL, donc son vert ne prouve "
        "rien. Vérifier que `set_schema` pose toujours l'index de clé métier avant de "
        "conclure quoi que ce soit de ce test.")
    assert not [t for t in mouchards.ddl if t is boucle], (
        "`CREATE INDEX CONCURRENTLY` est parti depuis le thread de l'event loop. Il "
        "attend, PAR CONCEPTION, la fin de toute transaction ouverte avant lui — une "
        "simple lecture suffit à le retenir, et il retient alors le serveur entier. "
        "C'est l'incident du 2026-09-01, à l'identique.")
    assert not [t for t in mouchards.pool if t is boucle], (
        f"{len([t for t in mouchards.pool if t is boucle])} lecture(s)/écriture(s) de "
        "base depuis la boucle sur le chemin d'une capacité")


def test_le_meme_chemin_par_la_face_REST_ne_part_pas_non_plus_de_la_boucle(
        tableau, monkeypatch):
    """Deux faces, un seul serveur mono-loop : la face REST monte ses routes par un
    autre adaptateur, et rien ne garantit que les deux aient la même discipline."""
    mouchards = _Mouchards(monkeypatch)
    cap = _cap_du_registre("me.datastore.patch_schema")
    binding = cap.rest_bindings()[0]
    # Le MÊME propriétaire que le tableau : sur un autre `sub`, la capacité refuse
    # avant d'atteindre la base, et la garde deviendrait inerte sans le dire.
    handler = _handler_rest(cap, binding, sub="sub-loop")

    async def _appel():
        import json as _json
        corps = _json.dumps(_PATCH_A_CLE).encode()

        async def _receive():
            return {"type": "http.request", "body": corps, "more_body": False}

        req = Request({"type": "http", "method": binding.verb,
                       "path": binding.path, "headers": [
                           (b"content-type", b"application/json")],
                       "query_string": b"",
                       "path_params": {"datastore": tableau}},
                      receive=_receive)
        return await handler(req)

    reponse = {}

    async def _capte():
        reponse["r"] = await _appel()

    boucle = _joue(_capte)

    assert reponse["r"].status_code == 200, (
        f"l'appel REST a échoué ({reponse['r'].status_code}, "
        f"{reponse['r'].body[:200]!r}) — il n'a donc pas pu atteindre le DDL, et le "
        "reste du test ne mesurerait rien")
    assert mouchards.ddl, "garde INERTE côté REST : le DDL n'a pas été atteint"
    assert not [t for t in mouchards.ddl if t is boucle], (
        "`CREATE INDEX CONCURRENTLY` est parti de la boucle par la face REST")
    assert not [t for t in mouchards.pool if t is boucle], (
        "accès base depuis la boucle sur le chemin REST d'une capacité")


def test_le_mouchard_mord(tableau, monkeypatch):
    """Contrôle : le MÊME travail appelé nûment dans la boucle EST attrapé.

    C'est la forme d'avant le correctif — `cap.handler(ctx, inp)` au milieu d'un
    `async def`. Sans ce test, les deux précédents pourraient rester verts pour de
    mauvaises raisons (mouchard mal branché, chemin qui n'atteint plus le DDL)."""
    mouchards = _Mouchards(monkeypatch)
    cap = _cap_du_registre("me.datastore.patch_schema")

    async def naif():
        ctx = ResolvedCtx(sub="sub-loop", org_id=None)
        inp = cap.Input(datastore=tableau, **_PATCH_A_CLE)
        cap.handler(ctx, inp)          # nûment, dans la boucle — la maladie

    boucle = _joue(naif)

    assert any(t is boucle for t in mouchards.ddl), (
        "le mouchard n'a pas vu un `CREATE INDEX CONCURRENTLY` pourtant lancé DANS la "
        "boucle : la détection est cassée, les tests ci-dessus ne prouvent rien")
    assert any(t is boucle for t in mouchards.pool), (
        "le mouchard n'a pas vu les accès base pourtant faits DANS la boucle")
