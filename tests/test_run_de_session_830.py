"""Le run de la SESSION rallumé pour les lecteurs qui rattachent, et pour eux seuls (#830).

Un agent ouvre un run par `run_start` puis travaille sans repasser `_run_id` : le run
vit sur la pile de la session (`guide_run`). Le middleware la relisait sur le
`MiddlewareContext`, qui n'a pas `get_state` — lecture avalée, pile vide : ce repli
était mort depuis le 28/08/2026, et le datastore ne connaissait un run que s'il était
NOMMÉ. Il se relit désormais sur `fastmcp_context` et se pose dans une ContextVar À
PART (`session_org.current_session_run`) :

- LISENT le run de session (explicite d'abord) : le datastore (réservation, verrou,
  indice de refus), le journal des gestes (`geste.estampille`), les jobs `jev` —
  `session_org.current_call_or_session_run()` ;
- NE LE LISENT PAS : la résolution d'org du run (`run_org.pin_for_call`, #639 — elle
  change l'org de l'appel et REFUSE qui n'en est plus membre) et les gardes anti-agent
  d'`oto_fleet` (`runner_fleets._run_courant`). Ils restent sur `_run_id` explicite.

Tout passe par un VRAI serveur FastMCP (client en mémoire, vraie session, vrai
`CallContextMiddleware`) : c'est l'objet que le middleware reçoit qui était faux, un
contexte de laboratoire doté de `get_state` ne le verrait pas.
"""
from __future__ import annotations

import asyncio
import types
import uuid

import pytest

from oto_mcp import session_org

SUB = "sub-run-session-830"


def _serveur(monkeypatch, *, sub=SUB, delegation_run=None):
    """Un serveur réel : la chaîne `CallContextMiddleware`, les verbes `run_*`, et une
    SONDE montée sous le nom `oto_fleet` — un nom sur la surface de corrélation, pour
    que `_run_id` explicite y soit posé comme sur le vrai outil."""
    from fastmcp import FastMCP

    from oto_mcp import call_axes, db, run_org
    from oto_mcp.auth import hooks
    from oto_mcp.capabilities import runner_fleets as RF
    from oto_mcp.datastore import hors_org, outils
    from oto_mcp.geste import estampille
    from oto_mcp.middleware.call_context import CallContextMiddleware
    from oto_mcp.tools import guide_run as outils_run

    if delegation_run is not None:
        monkeypatch.setattr(hooks, "current_token_axes",
                            lambda: {"token_id": 7, "token_kind": "delegation"})
        monkeypatch.setattr(hooks, "current_user_sub_from_token", lambda: sub)
        monkeypatch.setattr(db, "run_ouvert_du_jeton", lambda s, t: delegation_run)
    # Le sub que lit l'org du run : présent, pour que `pin_for_call` aille jusqu'à la
    # résolution — et une résolution espionnée, qui dit quel run elle a vu.
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: sub)
    # Ce même sub réveillerait les portes d'activation et de niveau d'accès, qui lisent
    # l'org en base : hors sujet ici, ouvertes.
    from oto_mcp.connectors import access_level_gate, activation_gate

    async def _ouvert(*a, **k):
        return None
    monkeypatch.setattr(activation_gate, "require_active", _ouvert)
    monkeypatch.setattr(access_level_gate, "require_covered", _ouvert)
    vus_par_pin: list = []

    def _resoudre(s, run_id, *, poser):
        vus_par_pin.append(run_id)
        return None
    monkeypatch.setattr(run_org, "_resoudre", _resoudre)

    m = FastMCP("t-run-session-830")
    m.add_middleware(CallContextMiddleware())
    outils_run.register(m)

    @m.tool(name="oto_fleet")
    async def sonde(op: str = "list") -> dict:
        """Ce que chaque famille de lecteurs voit, au moment du handler."""
        return {
            "explicite": session_org.current_call_run(),
            "session": session_org.current_session_run(),
            "datastore": outils._current_run(),
            "hors_org": hors_org.run_courant(),
            "geste": estampille()["run_id"],
            "flotte": RF._run_courant(),
        }
    return m, vus_par_pin


async def _sonder(client, **args) -> dict:
    return (await client.call_tool("oto_fleet", {"op": "list", **args})).structured_content


def _scenario(m, corps):
    from fastmcp import Client

    async def _go():
        async with Client(m) as c:
            return await corps(c)
    return asyncio.run(_go())


# ── 1. Les lecteurs qui rattachent voient le run de la session ───────────────

def test_un_run_ouvert_sans_jeton_est_vu_du_datastore_et_des_gestes(monkeypatch):
    m, _ = _serveur(monkeypatch)

    async def corps(c):
        run_id = (await c.call_tool("run_start", {"label": "t"})).structured_content["run_id"]
        return run_id, await _sonder(c)

    run_id, vu = _scenario(m, corps)
    assert vu["session"] == run_id
    assert vu["datastore"] == run_id, "le datastore ne voyait un run que NOMMÉ (#830)"
    assert vu["hors_org"] == run_id
    assert vu["geste"] == run_id, "le journal des gestes porte le run de la session"


def test_le_run_explicite_garde_la_priorite(monkeypatch):
    m, _ = _serveur(monkeypatch)

    async def corps(c):
        await c.call_tool("run_start", {"label": "t"})
        return await _sonder(c, _run_id="run-nomme")

    vu = _scenario(m, corps)
    assert vu["datastore"] == vu["geste"] == vu["explicite"] == "run-nomme"
    assert vu["session"] is None, "un run nommé ne laisse pas poser celui de la session"


def test_apres_run_finish_plus_aucun_run(monkeypatch):
    m, _ = _serveur(monkeypatch)

    async def corps(c):
        run_id = (await c.call_tool("run_start", {"label": "t"})).structured_content["run_id"]
        await c.call_tool("run_finish", {"run_id": run_id, "outcome": "done"})
        return await _sonder(c)

    vu = _scenario(m, corps)
    assert vu["session"] is vu["datastore"] is vu["geste"] is None


def test_le_run_repris_du_jeton_est_vu_des_le_premier_appel(monkeypatch):
    """#1392 : la reprise pousse le run sur la pile de la session NEUVE dans le même
    middleware — le premier appel de cette session le voit déjà."""
    m, _ = _serveur(monkeypatch, delegation_run="r-repris")
    vu = _scenario(m, lambda c: _sonder(c))
    assert vu["datastore"] == vu["geste"] == "r-repris"
    assert vu["flotte"] is None


def test_le_contexte_d_appel_est_rendu_apres_l_appel(monkeypatch):
    m, _ = _serveur(monkeypatch)

    async def corps(c):
        await c.call_tool("run_start", {"label": "t"})
        await _sonder(c)
        return session_org.current_session_run()

    assert _scenario(m, corps) is None


# ── 2. Garde-fou : l'org du run et les gardes de flotte restent sur l'explicite ──

def test_le_run_de_session_ne_reveille_ni_l_org_du_run_ni_les_gardes_de_flotte(monkeypatch):
    """Si l'on branchait un jour `pin_for_call` ou `_run_courant` sur le run de
    session, des appels qui ne nomment aucun run changeraient d'org — ou seraient
    refusés (#639) — et un humain admin dans un run se verrait refuser `launch`."""
    m, vus_par_pin = _serveur(monkeypatch)

    async def corps(c):
        run_id = (await c.call_tool("run_start", {"label": "t"})).structured_content["run_id"]
        return run_id, await _sonder(c)

    run_id, vu = _scenario(m, corps)
    assert vu["session"] == run_id                  # le run de session est bien là…
    assert vu["flotte"] is None, "gardes d'oto_fleet branchées sur le run de session"
    assert vu["explicite"] is None
    assert vus_par_pin == [], "l'org du run résolue sur le run de session (#639)"


def test_avec_run_id_explicite_l_org_du_run_et_les_gardes_le_voient(monkeypatch):
    m, vus_par_pin = _serveur(monkeypatch)
    vu = _scenario(m, lambda c: _sonder(c, _run_id="run-nomme"))
    assert vu["flotte"] == "run-nomme"
    assert vus_par_pin == ["run-nomme"]


def test_oto_fleet_launch_dans_un_run_de_session_n_est_pas_refuse(monkeypatch):
    """Le verbe réel, sur le run de session : la garde `not_from_a_run` ne mord pas."""
    from oto_mcp.capabilities import runner_fleets as RF
    from oto_mcp.capabilities._types import ResolvedCtx

    class _AuDelaDeLaGarde(Exception):
        pass

    def _lit_la_flotte(*a, **k):
        raise _AuDelaDeLaGarde
    monkeypatch.setattr("oto_mcp.roles.is_org_admin", lambda sub, org: True)
    monkeypatch.setattr(RF.db, "runner_arme", lambda org: {
        "armed": True, "workers": 1, "last_seen": "2026-09-23 08:00:00", "families": []})
    monkeypatch.setattr(RF.db, "get_fleet", _lit_la_flotte)
    session_org_token = session_org.set_session_run("run-de-session")
    try:
        with pytest.raises(_AuDelaDeLaGarde):
            RF._fleets(ResolvedCtx(sub="humain", org_id=2),
                       RF.FleetInput(op="launch", fleet_id=7))
    finally:
        session_org.reset_session_run(session_org_token)


# ── 3. L'indice « tu as oublié `_run_id` » ───────────────────────────────────

def _serveur_indice(monkeypatch):
    from oto_mcp.tools import datastore as T
    m, _ = _serveur(monkeypatch)
    monkeypatch.setattr(T.access, "current_user_sub_from_token", lambda: SUB)
    monkeypatch.setattr(T.db, "get_run_head", lambda run: {"sub": SUB})

    @m.tool(name="sonde_indice")
    async def sonde_indice(run: str) -> str:
        return str(T._omitted_run_hint(types.SimpleNamespace(claimed_run=run)))
    return m


def test_l_indice_se_tait_quand_le_run_de_session_est_pose(monkeypatch):
    m = _serveur_indice(monkeypatch)

    async def corps(c):
        run_id = (await c.call_tool("run_start", {"label": "t"})).structured_content["run_id"]
        return (await c.call_tool("sonde_indice", {"run": run_id})).content[0].text

    assert _scenario(m, corps) == "None"


def test_l_indice_parle_sans_aucun_run(monkeypatch):
    m = _serveur_indice(monkeypatch)

    async def corps(c):
        return (await c.call_tool("sonde_indice", {"run": "r-tenu"})).content[0].text

    texte = _scenario(m, corps)
    assert "You passed no `_run_id`" in texte and "`r-tenu`" in texte


# ── 1 bis. Bout en bout sur PostgreSQL : réservation rattachée, puis libérée ──

_OUTILS: dict = {}


def test_une_reservation_sans_jeton_est_rattachee_au_run_et_liberee(live, monkeypatch):
    """`run_start` → `data_claim_next` SANS `_run_id` → `run_finish`, sur un vrai
    serveur : la ligne porte le run de la session, et la clôture la rend."""
    from fastmcp import FastMCP

    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    from oto_mcp.db._conn import _connect
    from oto_mcp.middleware.call_context import CallContextMiddleware
    from oto_mcp.tools import datastore as T
    from oto_mcp.tools import register_all

    monkeypatch.setattr(T, "_acting_store", lambda: make_store(SUB))
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    monkeypatch.setattr(T, "_project_hint", lambda ns: None)
    ns_id = db.create_datastore("user", SUB, "file-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    for i in range(2):

        db.datastore_insert_row(ns_id, f"r{i}", {"siren": f"85045{i:04d}"})

    m = FastMCP("t-run-session-830-pg")
    m.add_middleware(CallContextMiddleware())
    register_all(m)

    def _bail(row_id):
        with _connect() as conn:
            return dict(conn.execute(
                "SELECT claimed_by, claimed_run FROM datastore_rows "
                "WHERE ns_id = %s AND row_id = %s", (ns_id, row_id)).fetchone())

    async def corps(c):
        run_id = (await c.call_tool("run_start", {"label": "t"})).structured_content["run_id"]
        pris = (await c.call_tool("data_claim_next",
                                  {"datastore": ns, "worker": "w"})).structured_content
        bail = _bail(pris["row"]["_id"])
        fin = (await c.call_tool("run_finish",
                                 {"run_id": run_id, "outcome": "done"})).structured_content
        return run_id, pris["row"]["_id"], bail, fin

    run_id, row_id, bail, fin = _scenario(m, corps)
    assert bail["claimed_run"] == run_id, "la réservation n'est pas rattachée au run"
    assert fin["rows_released"] == 1
    assert _bail(row_id)["claimed_by"] is None
