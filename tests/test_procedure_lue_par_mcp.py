"""Un travail hébergé lit sa procédure par `oto_procedure`, et travaille, dans l'org DU
TRAVAIL — contre un vrai PostgreSQL, par le montage MCP réel (13/09/2026).

Depuis le retrait de l'injection, l'agent n'a plus que l'instruction « lis la procédure
X » et la charge SERVIE au claim (`runner_jobs._charge_servie`) : l'outil de lecture, et
l'org du travail, que le worker impose en `_org` à chaque appel qui déclare l'axe. Qu'un
nom d'outil soit présent ne prouve pas que le texte est accessible, ni dans la bonne
org : ces bancs appellent `run_start`, `oto_procedure` et `data_write` par un client MCP
sur `register_all` + l'adaptateur des capacités + `CallContextMiddleware`, comme
`server._build_mcp` les monte.

Le monde est celui qui a révélé le défaut : un porteur membre de deux orgs, dont l'org
ACTIVE (A) n'est pas celle du déclencheur (B), et une procédure au même slug dans les
deux. Les refus par palier (procédure personnelle d'autrui, équipe hors appartenance)
ont leurs bancs : `test_procedure_paliers_681.py`, `test_accuse_procedure_perso.py` ; la
résolution par run et la garde de l'axe : `test_org_du_run_639.py`,
`middleware/test_call_context_org_axis.py`.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from oto_mcp.capabilities._types import AuthzDenied, RawCtx

PORTEUR = "u-porteur-deux-orgs"
CORPS_A = "# Passe\n\nCORPS DE L'ORG A — la procédure de l'org active."
CORPS_B = "# Passe\n\nCORPS DE L'ORG B — celle du déclencheur, en version 2."


@pytest.fixture(scope="module")
def monde(pg_dsn):
    """Une base JETABLE, bootée par le vrai `init_db`. Org A = l'org active du porteur ;
    org B = celle du travail, où `passe` est en version 2 ; org C = une org dont il n'est
    pas membre. Un guide SEUL, sans procédure homonyme, vit dans l'org B."""
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    nom = "oto_lue_mcp_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    url_avant, pool_avant = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = pg_dsn.rsplit("/", 1)[0] + "/" + nom
    dbconn._pool = None
    try:
        from oto_mcp import db, org_store
        from oto_mcp.db import init_db
        init_db()
        a = org_store.create_org("Org active", created_by="u-auteur")
        b = org_store.create_org("Org du travail", created_by="u-auteur")
        c = org_store.create_org("Org étrangère", created_by="u-auteur")
        for org in (a, b):
            org_store.add_org_member(org, PORTEUR, "org_member")
        assert org_store.set_active_org(PORTEUR, a)
        org_store.set_instruction("org", a, "passe", CORPS_A, set_by="u-auteur")
        org_store.set_instruction("org", b, "passe", "# Passe\n\nversion 1", set_by="u-auteur")
        org_store.set_instruction("org", b, "passe", CORPS_B, set_by="u-auteur")
        org_store.set_instruction("org", c, "passe", "# Passe\n\nCORPS DE L'ORG C",
                                  set_by="u-auteur")
        db.set_guide_db("org", str(b), "veille-guide", "# Veille\n\n<tool:data_write>")
        yield {"a": a, "b": b, "c": c}
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = pool_avant
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()


@pytest.fixture
def porteur(monde, monkeypatch):
    """L'identité que porte le jeton délégué, là où chaque étage la lit — et les outils
    `data_*` tenus par cet acteur, comme `test_org_du_run_639.py`."""
    from oto_mcp.datastore.core import make_store
    from oto_mcp.tools import datastore as T
    # ⚠️ Le serveur — donc tout module qui LIE `current_user_sub_from_token` à son import
    # (adaptateur, middleware, outils) — est monté AVANT de patcher `auth.hooks` : importé
    # pendant le patch, un module lierait le faux lecteur, que monkeypatch prendrait pour
    # sa valeur d'origine et « restaurerait » — l'identité du porteur fuyait alors dans
    # tous les bancs suivants du worker.
    import oto_mcp.call_axes  # noqa: F401
    _serveur()
    for cible in ("oto_mcp.auth.hooks.current_user_sub_from_token",
                  "oto_mcp.capabilities._mcp_adapter.current_user_sub_from_token",
                  "oto_mcp.middleware.call_context.current_user_sub_from_token",
                  "oto_mcp.call_axes.current_user_sub_from_token"):
        monkeypatch.setattr(cible, lambda: PORTEUR)
    monkeypatch.setattr(T, "_acting_store", lambda: make_store(PORTEUR))
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    monkeypatch.setattr(T, "_project_hint", lambda ns: None)
    return monde


_SERVEUR: dict = {}


def _serveur():
    """Le montage de `server._build_mcp`, ni plus ni moins pour ce qui est appelé ici."""
    if not _SERVEUR:
        from fastmcp import FastMCP

        from oto_mcp.capabilities import _mcp_adapter
        from oto_mcp.capabilities.registry import CAPABILITIES
        from oto_mcp.middleware.call_context import CallContextMiddleware
        from oto_mcp.tools import register_all
        m = FastMCP("t-procedure-lue")
        register_all(m)
        _mcp_adapter.register(m, CAPABILITIES)
        m.add_middleware(CallContextMiddleware())
        _SERVEUR["m"] = m
    return _SERVEUR["m"]


def _servi(org: int, payload: dict) -> dict:
    """La charge telle que le claim la SERT — la fonction du claim, pas une copie."""
    from oto_mcp.capabilities import runner_jobs
    return runner_jobs._charge_servie(
        {"id": 1, "org_id": org, "sub": PORTEUR, "payload": payload})["payload"]


def _table(org: int) -> tuple[str, int]:
    from oto_mcp import db
    ns_id = db.create_datastore("org", str(org), "file-" + uuid.uuid4().hex[:6])
    # `statut` déclarée : une colonne non déclarée est refusée à l'écriture (oto#124).
    db.set_datastore_schema(ns_id, {"fields": [{"key": "statut", "type": "text"}]})
    db.datastore_insert_row(ns_id, "r0", {"statut": "a_faire"})
    return str(ns_id), ns_id



def _valeur(ns_id: int, champ: str):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        row = conn.execute("SELECT data FROM datastore_rows WHERE ns_id = %s "
                           "AND row_id = 'r0'", (ns_id,)).fetchone()
    return (row or {}).get("data", {}).get(champ)


@pytest.mark.asyncio
async def test_un_travail_de_declencheur_lit_et_ecrit_dans_l_org_du_TRAVAIL(porteur):
    from fastmcp import Client

    from oto_mcp import db
    from oto_mcp.capabilities import _instruction

    a, b = porteur["a"], porteur["b"]
    # Le travail tel que le TICK l'enfile : aucune org dans la charge.
    charge = _servi(b, {"procedure": "passe", "tools": ["data_write"], "trigger_id": 1,
                        "input": _instruction.derivee("passe")})
    assert (charge["org_id"], "oto_procedure" in charge["tools"]) == (b, True)
    assert "`passe`" in charge["input"]
    axe = {"_org": charge["org_id"]}   # ce que le worker pose à chaque appel qui le déclare
    ns, ns_id = _table(b)

    async with Client(_serveur()) as c:
        ouvert = (await c.call_tool(
            "run_start", {"label": "déclencheur", "guide": "passe", **axe})).structured_content
        lu = (await c.call_tool(
            "oto_procedure", {"op": "get", "slug": charge["procedure"], **axe})).structured_content
        ecrit = (await c.call_tool(
            "data_write", {"datastore": ns, "id": "r0", "row": {"statut": "fait"},
                           "_run_id": ouvert["run_id"], **axe})).structured_content

    assert ouvert["guide_version"] == 2, "le run fige la version de l'org du travail"
    assert db.get_run_head(ouvert["run_id"])["org_id"] == b
    assert (lu["org_id"], lu["version"]) == (b, 2) and "CORPS DE L'ORG B" in lu["body_md"]
    assert "CORPS DE L'ORG A" not in lu["body_md"]
    assert ecrit["_id"] == "r0" and _valeur(ns_id, "statut") == "fait"
    assert a != b


@pytest.mark.asyncio
async def test_sans_axe_org_l_appel_se_resout_dans_l_org_ACTIVE_du_porteur(porteur):
    """Ce que la charge servie ferme : le même geste SANS `_org` — ce que recevait
    l'agent d'un déclencheur — ouvre le run et lit la procédure de l'org active."""
    from fastmcp import Client

    async with Client(_serveur()) as c:
        ouvert = (await c.call_tool(
            "run_start", {"label": "sans axe", "guide": "passe"})).structured_content
        lu = (await c.call_tool(
            "oto_procedure", {"op": "get", "slug": "passe"})).structured_content

    assert ouvert["guide_version"] == 1
    assert lu["org_id"] == porteur["a"] and "CORPS DE L'ORG A" in lu["body_md"]


@pytest.mark.asyncio
async def test_une_org_hors_des_droits_du_porteur_est_REFUSEE_sans_repli(porteur):
    """Servir une org n'est pas un droit : hors appartenance, lecture et écriture sont
    refusées — jamais servies depuis l'org active."""
    from fastmcp import Client

    ns_a, ns_id_a = _table(porteur["a"])
    axe = {"_org": porteur["c"]}
    async with Client(_serveur()) as c:
        lecture = await c.call_tool("oto_procedure", {"op": "get", "slug": "passe", **axe},
                                    raise_on_error=False)
        ecriture = await c.call_tool(
            "data_write", {"datastore": ns_a, "id": "r0", "row": {"statut": "vole"}, **axe},
            raise_on_error=False)

    assert lecture.is_error and ecriture.is_error
    assert "CORPS DE L'ORG" not in str(lecture.content)
    assert _valeur(ns_id_a, "statut") == "a_faire"


def test_une_reference_absente_de_la_table_des_procedures_est_une_erreur_EXPLICITE(porteur):
    """L'exemple exact du magasin qui diffère. Un déclencheur DÉDUIT ses outils des
    guides (`runner_triggers._outils_de_la_procedure`) : posé sur un slug qui n'existe
    qu'en guide, il est accepté et reçoit l'outil de lecture. Mais `oto_procedure` lit
    `org_instructions` : la lecture rend un refus nommé — jamais un texte vide, jamais un
    repli sur le guide. L'injection lisait déjà cette table : ce cas n'est pas né du
    retrait, il est désormais VISIBLE dans le fil de l'agent."""
    from oto_mcp import session_org
    from oto_mcp.capabilities import runner_triggers
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.registry import CAPABILITIES

    b = porteur["b"]
    outils = runner_triggers._outils_de_la_procedure(ResolvedCtx(sub=PORTEUR, org_id=b),
                                                     "veille-guide")
    assert outils == ["data_write"], "le déclencheur se pose sur le guide"
    assert _servi(b, {"procedure": "veille-guide", "tools": outils})["tools"] == [
        "data_write", "oto_procedure"]

    cap = next(x for x in CAPABILITIES if x.key == "org.procedure.console")
    inp = cap.Input(op="get", slug="veille-guide")
    jeton = session_org.set_call_org(b)
    try:
        with pytest.raises(AuthzDenied) as e:
            out = cap.handler(cap.authz(RawCtx(sub=PORTEUR), inp), inp)
            asyncio.run(out) if asyncio.iscoroutine(out) else out
    finally:
        session_org._CALL_ORG.reset(jeton)
    assert (e.value.status, e.value.code) == (404, "unknown_guide")
