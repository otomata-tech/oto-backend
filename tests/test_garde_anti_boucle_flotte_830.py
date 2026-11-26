"""Les gardes anti-agent d'`oto_fleet` MORDENT sur le chemin servi (#830, 23/09/2026).

`launch` refuse `not_from_a_run` (« un agent qui se relance lui-même dépense en
boucle ») et `stop` refuse `not_your_own_fleet`. Les deux se décident sur
`runner_fleets._run_courant()`, qui lit `session_org.current_call_run()` — une
ContextVar que SEUL l'axe `_run_id` alimente côté handler. Le run actif de la pile de
session est posé à part (`session_org.current_session_run`) et ces gardes ne le lisent
pas, par choix (`test_run_de_session_830.py`).

Or `oto_fleet` n'était pas sur la surface de corrélation (`call_axes`) : le jeton
était retiré sans être posé, et le runner hébergé — qui ne pose `_run_id` QUE sur
un outil dont le schéma le déclare (`oto_runner/mcp.py`, `call()`) — ne l'envoyait
même pas. `_run_courant()` rendait `None` quel que soit le client : deux gardes
vertes, et inertes. Le banc d'origine (`test_runner_fleets.py`) doublait
`_run_courant` lui-même, donc ne pouvait pas le voir.

Ces tests passent par le VRAI `CallContextMiddleware` et ne doublent jamais
`_run_courant` : c'est la couture qui était cassée.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import call_axes, session_org
from oto_mcp.capabilities import runner_fleets as RF
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


def _par_le_middleware(nom: str, arguments: dict, handler):
    """L'appel tel qu'il arrive en production : les axes sont lus des arguments
    BRUTS par le middleware, posés, puis l'outil est dispatché (ici `handler`)."""
    from oto_mcp.middleware.call_context import CallContextMiddleware

    class _Msg:
        pass

    class _Ctx:                     # comme le vrai MiddlewareContext : PAS de get_state
        pass

    msg = _Msg()
    msg.name = nom
    msg.arguments = dict(arguments)
    ctx = _Ctx()
    ctx.message = msg

    async def _next(c):
        return handler(dict(c.message.arguments))

    return asyncio.run(CallContextMiddleware().on_call_tool(ctx, _next))


@pytest.fixture
def _flotte_lancable(monkeypatch):
    """Tout ce qui PRÉCÈDE la garde anti-agent est satisfait : compte bêta, admin
    d'org. `_run_courant` n'est PAS doublé — c'est lui qu'on éprouve."""
    monkeypatch.setattr("oto_mcp.roles.is_org_admin", lambda sub, org: True)
    monkeypatch.setattr(RF.db, "runner_arme", lambda org: {
        "armed": True, "workers": 1, "last_seen": "2026-09-23 08:00:00",
        "families": []})

    def _au_dela_de_la_garde(*a, **k):
        raise AssertionError("la garde anti-agent n'a pas mordu : le handler est "
                             "allé chercher ou modifier la flotte")
    monkeypatch.setattr(RF.db, "get_fleet", _au_dela_de_la_garde)
    monkeypatch.setattr(RF.db, "demander_arret", _au_dela_de_la_garde)


def test_oto_fleet_declare_le_jeton_de_run():
    """Le runner hébergé ne pose `_run_id` que sur un outil qui le DÉCLARE : sans
    l'axe dans le schéma servi, la garde n'a jamais de run à voir."""
    assert "_run_id" in {a.param for a in call_axes.axes_for("oto_fleet")}


def test_le_run_de_l_appel_atteint_le_handler_d_oto_fleet():
    """La couture elle-même : `_run_id` posé sur `oto_fleet` alimente ce que lit
    `_run_courant()` au moment du handler."""
    vu = {}
    _par_le_middleware(
        "oto_fleet", {"op": "list", "_run_id": "run-agent-830"},
        lambda args: vu.update(run=RF._run_courant(), args=args))
    assert vu["run"] == "run-agent-830"
    assert "_run_id" not in vu["args"], "le jeton est consommé, pas transmis à l'Input"


def test_un_deroule_qui_se_relance_est_refuse(_flotte_lancable):
    """La boucle réelle : un agent, dans son run, appelle `oto_fleet op=launch`."""
    ctx = ResolvedCtx(sub="agent", org_id=2)

    def _handler(args):
        return RF._fleets(ctx, RF.FleetInput(**args))

    with pytest.raises(AuthzDenied) as e:
        _par_le_middleware(
            "oto_fleet", {"op": "launch", "fleet_id": 7, "_run_id": "run-agent-830"},
            _handler)
    assert e.value.code == "not_from_a_run"


def test_un_deroule_n_arrete_pas_sa_propre_flotte(_flotte_lancable, monkeypatch):
    """`stop` depuis le run qui exécute CETTE flotte : refusé, nommé."""
    monkeypatch.setattr(RF.db, "run_appartient_a_flotte",
                        lambda run, fid: (run, fid) == ("run-agent-830", 7))
    ctx = ResolvedCtx(sub="agent", org_id=2)

    def _handler(args):
        return RF._fleets(ctx, RF.FleetInput(**args))

    with pytest.raises(AuthzDenied) as e:
        _par_le_middleware(
            "oto_fleet", {"op": "stop", "fleet_id": 7, "_run_id": "run-agent-830"},
            _handler)
    assert e.value.code == "not_your_own_fleet"


def test_hors_run_la_garde_ne_mord_pas(_flotte_lancable, monkeypatch):
    """Le contrôle : sans run, un humain admin lance normalement — la garde ne
    ferme pas le verbe, elle nomme le seul cas dangereux."""
    class _AuDelaDeLaGarde(Exception):
        pass

    def _lit_la_flotte(*a, **k):
        raise _AuDelaDeLaGarde
    monkeypatch.setattr(RF.db, "get_fleet", _lit_la_flotte)
    ctx = ResolvedCtx(sub="alexis", org_id=2)

    def _handler(args):
        return RF._fleets(ctx, RF.FleetInput(**args))

    with pytest.raises(_AuDelaDeLaGarde):
        _par_le_middleware("oto_fleet", {"op": "launch", "fleet_id": 7}, _handler)
    assert session_org.current_call_run() is None
