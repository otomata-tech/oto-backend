"""Le run porte le contexte de son appel, jusqu'à `runs.project_id` (oto-backend#290).

`axes_for("run_start")` rendait `[]` : les prédicats d'axe ne couvraient que les tools de
TRAVAIL, et le namespace `run` n'en est pas un. Le run — l'objet dont le métier est de
CORRÉLER des appels de travail — était donc le seul à ne pas pouvoir porter les jetons qui
qualifient ces appels. Conséquences : `runs.project_id` toujours NULL (quatre lecteurs sur
une colonne morte) et `runs.org_id` = l'org MAISON même quand tout le déroulé se fait
sous `_org=`.

⚠️ **Ce fichier ne monkeypatche plus `access.current_project` / `access.current_org`.**
Il les stubbait, donc il passait au vert sur un chemin qui ne se déclenchait jamais — le
mode de panne déjà payé en juin (deux fonctions RBAC cassées, masquées par des tests qui
stubbaient la fonction qu'ils devaient exercer). Ici l'appel traverse le VRAI chemin :
`CallContextMiddleware.on_call_tool` → advertisement/garde/pose de l'axe → la fonction
réelle de `run_start` → `access.current_project()` → `db.insert_run`. Ne restent stubbés
que la DB (`ownership`, `org_store`), l'identité du jeton, et `insert_run` — le point
d'observation, pas le seam sous test.
"""
from __future__ import annotations

import asyncio

import pytest
from fastmcp import FastMCP
from oto_mcp.mcp_errors import McpError
from oto_mcp import call_axes, db, org_store, ownership, session_org
from oto_mcp.auth import hooks as auth_hooks
from oto_mcp.middleware.call_context import CallContextMiddleware
from oto_mcp.tools import guide_run as drt


# ── Doubles : l'état de session (push_run) et le message de l'appel ──────────

class _SessionCtx:
    """Context FastMCP réduit à ce que `push_run` utilise : l'état de session."""

    def __init__(self):
        self._state: dict = {}

    async def get_state(self, key):
        return self._state.get(key)

    async def set_state(self, key, value):
        self._state[key] = value


class _Msg:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _Ctx:
    def __init__(self, msg):
        self.message = msg


@pytest.fixture(scope="module")
def run_start_fn():
    """La VRAIE fonction du tool `run_start`, telle qu'enregistrée au serveur."""
    mcp = FastMCP("test")
    drt.register(mcp)
    return asyncio.run(mcp.get_tool("run_start")).fn


def _wire(monkeypatch, *, sub="u1", home_org=3, project_org=42, readable=True) -> dict:
    """Stubbe la DB et l'identité — jamais les seams de contexte. Rend le dict qui
    capte les kwargs de `db.insert_run` (le point d'observation)."""
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: sub)
    monkeypatch.setattr(auth_hooks, "current_user_sub_from_token", lambda: sub)
    monkeypatch.setattr(org_store, "get_active_org", lambda s: home_org)   # l'org MAISON
    monkeypatch.setattr(org_store, "resolve_org_for_user", lambda s, o: int(o))
    monkeypatch.setattr(ownership, "can_access", lambda *a, **k: readable)
    monkeypatch.setattr(ownership, "owner_of", lambda rt, rid: ("org", str(project_org)))
    # #480 : la pose de `_project=` lit l'appartenance (verdict des clés) — membre ici.
    from oto_mcp import roles
    monkeypatch.setattr(roles, "is_org_member", lambda s, o: True)
    rec: dict = {}
    monkeypatch.setattr(db, "insert_run", lambda run_id, **kw: rec.update(kw, run_id=run_id))
    return rec


async def _start_run(run_start_fn, **args) -> dict:
    """Ouvre un run PAR LE CHEMIN RÉEL : middleware (axes) puis fonction du tool."""
    mw = CallContextMiddleware()
    seen: dict = {}

    async def _next(ctx):
        seen["args"] = dict(ctx.message.arguments)      # jetons retirés avant dispatch
        return await run_start_fn(_SessionCtx(), **ctx.message.arguments)

    out = await mw.on_call_tool(_Ctx(_Msg("run_start", dict(args))), _next)
    return {"result": out, "dispatched": seen.get("args")}


# ── L'advertisement : le run est un porteur de contexte comme les autres ─────

def _params(name):
    return {a.param for a in call_axes.axes_for(name)}


def test_les_verbes_de_run_advertisent_les_jetons_de_contexte():
    for name in ("run_start", "run_finish"):
        assert {"_project", "_org"} <= _params(name), name
        # `_run_id=` reste hors de portée : c'est `run_start` qui OUVRE un run,
        # le corréler à un autre run n'a pas de sens.
        assert "_run_id" not in _params(name), name


def test_le_schema_expose_les_jetons_au_client():
    # sans l'advertisement, `additionalProperties:false` ferait refuser le jeton côté
    # client : l'agent ne pourrait littéralement pas l'envoyer.
    schema = call_axes.inject_schema(
        {"type": "object", "properties": {"label": {}}}, call_axes.axes_for("run_start"))
    assert {"label", "_project", "_org"} <= set(schema["properties"])


# ── Le contexte arrive jusqu'à la ligne `runs` ──────────────────────────────

@pytest.mark.asyncio
async def test_le_projet_de_lappel_est_gele_dans_le_run(monkeypatch, run_start_fn):
    rec = _wire(monkeypatch)
    out = await _start_run(run_start_fn, label="prospection Q3", doctrine="prospection",
                           _project=9)
    assert out["dispatched"] == {"label": "prospection Q3", "doctrine": "prospection"}
    assert rec["project_id"] == 9                  # la colonne cesse d'être morte
    assert rec["org_id"] == 42                     # org PROPRIÉTAIRE du projet, co-posée
    assert rec["guide"] == "prospection" and rec["run_id"] == out["result"]["run_id"]


@pytest.mark.asyncio
async def test_lorg_du_jeton_prime_sur_lorg_maison(monkeypatch, run_start_fn):
    # le run se range sous l'org où il se déroule, pas sous l'org d'habitude de
    # celui qui l'ouvre — sinon la lentille d'org ne le voit jamais.
    rec = _wire(monkeypatch, home_org=3)
    await _start_run(run_start_fn, label="audit", _org=42)
    assert rec["org_id"] == 42 and rec["project_id"] is None


@pytest.mark.asyncio
async def test_sans_jeton_le_run_retombe_sur_la_maison(monkeypatch, run_start_fn):
    # aucun jeton = comportement d'avant, inchangé (l'axe n'est jamais obligatoire).
    rec = _wire(monkeypatch, home_org=3)
    await _start_run(run_start_fn, label="run ad-hoc")
    assert rec["org_id"] == 3 and rec["project_id"] is None


@pytest.mark.asyncio
async def test_stdio_local_sans_sub_ne_scope_rien(monkeypatch, run_start_fn):
    rec = _wire(monkeypatch, sub=None)
    await _start_run(run_start_fn, label="local")
    assert rec["run_id"] and rec["org_id"] is None and rec["project_id"] is None


@pytest.mark.asyncio
async def test_un_projet_illisible_refuse_avant_douvrir_le_run(monkeypatch, run_start_fn):
    # la garde de l'axe (`can_access`) vaut ici comme ailleurs : pas de run gelé sur
    # un projet auquel l'appelant n'a pas accès, et un refus NOMMÉ, pas un repli muet.
    rec = _wire(monkeypatch, readable=False)
    with pytest.raises(McpError):
        await _start_run(run_start_fn, label="curieux", _project=9)
    assert rec == {}


@pytest.mark.asyncio
async def test_le_jeton_ne_fuit_pas_apres_lappel(monkeypatch, run_start_fn):
    _wire(monkeypatch)
    await _start_run(run_start_fn, label="prospection Q3", _project=9)
    assert session_org.current_call_project() is None
    assert session_org.current_call_org() is None
