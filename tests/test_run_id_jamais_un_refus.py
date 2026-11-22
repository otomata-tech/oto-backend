"""`_run_id=` ne fait échouer AUCUN appel — signaux #651 (`oto_trigger`) et #664
(`oto_procedure`), 02/09/2026.

Le serveur servait deux consignes contradictoires. La notice du handshake :
« `_run_id` OBLIGATOIRE sur CHAQUE appel dès qu'un run est ouvert ». La surface :
l'axe n'est advertisé/lu que sur les outils de TRAVAIL (connecteurs, `data_*`) plus
quatre outils de spine projet nommés à la main après le feedback #168. Les 53 autres
capacités `oto_*` recevaient donc un jeton qu'elles ne déclarent pas, et le schéma
plat les faisait refuser AVANT le handler :

    Arguments invalides — valeur(s) refusée(s) : _run_id (Unexpected keyword argument)

Un agent qui obéit à la consigne perd son appel ; celui qui désobéit passe. C'est le
même défaut que #168, et le nommer outil par outil l'a laissé revenir deux fois.

Ce que ces tests tiennent, sur la surface SERVIE (adaptateur de capacités + le vrai
`CallContextMiddleware`, jeton lu des arguments bruts) :

  1. les deux outils NOMMÉS par les signaux acceptent `_run_id` — et l'appel
     descend jusqu'au handler (il bute sur l'authentification, pas sur le jeton) ;
  2. la CLASSE entière : aucune capacité montée en MCP ne refuse le jeton ;
  3. le jeton n'atteint pas le handler (il est retiré, pas passé à l'`Input`) ;
  4. ce qui n'a pas changé : sur un outil hors surface de corrélation, `_run_id` ne
     POSE rien — pas de contexte de run, donc pas de résolution d'org du run, donc
     aucun refus nouveau ;
  5. `oto_call` le DÉCLARE et le replie dans les arguments de sa cible.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import call_axes, session_org


# ── Surface servie : les capacités telles que le boot les monte ───────────────

_SERVEUR: dict = {}


def _serveur():
    """L'adaptateur de capacités, monté une fois (comme `server.py` le fait)."""
    if not _SERVEUR:
        from fastmcp import FastMCP

        from oto_mcp.capabilities import _mcp_adapter, registry
        m = FastMCP("t-run-id")
        _mcp_adapter.register(m, registry.CAPABILITIES)
        _SERVEUR["mcp"] = m
        _SERVEUR["noms"] = sorted(
            c.mcp for c in registry.CAPABILITIES if getattr(c, "mcp", None))
    return _SERVEUR


def _appel(nom: str, arguments: dict):
    """L'appel tel qu'il arrive en production : le middleware lit les axes des
    arguments BRUTS, pose/retire, puis l'outil est dispatché."""
    from oto_mcp.middleware.call_context import CallContextMiddleware

    srv = _serveur()

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
        outil = await srv["mcp"].get_tool(nom)
        return await outil.run(c.message.arguments)

    async def _go():
        return await CallContextMiddleware().on_call_tool(ctx, _next)

    return asyncio.run(_go())


def _motif(nom: str, arguments: dict) -> str:
    """Le motif du refus, ou '' si l'appel a abouti."""
    try:
        _appel(nom, arguments)
        return ""
    except Exception as e:  # noqa: BLE001 — c'est le motif qu'on inspecte
        return str(e)


# ── 1. Les deux outils nommés par les signaux ────────────────────────────────

@pytest.mark.parametrize("nom", ["oto_procedure", "oto_trigger"])
def test_le_jeton_ne_refuse_plus_l_appel(nom):
    """#651/#664 : l'appel bute désormais là où il buterait SANS le jeton.

    On ne compare pas à « ça marche » (rien n'est authentifié dans ce banc) mais au
    MÊME appel sans jeton : c'est ce qui prouve que `_run_id` n'est plus la cause.
    """
    sans = _motif(nom, {"op": "list"})
    avec = _motif(nom, {"op": "list", "_run_id": "abc123"})
    assert avec == sans, (nom, sans, avec)
    assert "_run_id" not in avec
    assert "Unexpected keyword argument" not in avec


# ── 2. La CLASSE : aucune capacité servie ne refuse le jeton ─────────────────

def test_aucune_capacite_servie_ne_refuse_le_jeton():
    """La garde de classe. Nommer les outils un par un a laissé le défaut revenir
    deux fois (#168 → #651/#664) ; ici, une capacité NEUVE qui refuserait `_run_id`
    fait rougir ce test le jour où elle est déclarée."""
    coupables = []
    for nom in _serveur()["noms"]:
        motif = _motif(nom, {"_run_id": "abc123"})
        if "Unexpected keyword argument" in motif and "_run_id" in motif:
            coupables.append(nom)
    assert coupables == [], coupables


# ── 3. Le jeton n'atteint pas le handler ────────────────────────────────────

def test_le_jeton_est_retire_avant_le_dispatch():
    """Accepté ne veut pas dire « transmis » : l'`Input` d'une capacité ne déclare
    pas `_run_id`, il doit avoir disparu des arguments au moment du dispatch."""
    vus: dict = {}
    from oto_mcp.middleware.call_context import CallContextMiddleware

    class _Msg:
        pass

    class _Ctx:
        pass

    msg = _Msg()
    msg.name = "oto_procedure"
    msg.arguments = {"op": "list", "_run_id": "abc123"}
    ctx = _Ctx()
    ctx.message = msg

    async def _next(c):
        vus.update(dict(c.message.arguments))
        return None

    asyncio.run(CallContextMiddleware().on_call_tool(ctx, _next))
    assert vus == {"op": "list"}, vus


# ── 4. Ce qui n'a PAS changé : hors surface de travail, le jeton ne POSE rien ─

def test_hors_surface_de_correlation_le_jeton_ne_pose_aucun_contexte():
    """⚠️ Le jeton est RETIRÉ, pas posé. Poser la ContextVar ferait résoudre l'org
    du run (#639) sur des outils qui ne l'ont jamais fait — donc de NOUVEAUX refus
    (« tu n'es pas membre de l'org de ce run ») sur des appels qui passaient. Cesser
    de refuser n'est pas élargir la corrélation."""
    vu: dict = {}
    from oto_mcp.middleware.call_context import CallContextMiddleware

    class _Msg:
        pass

    class _Ctx:
        pass

    msg = _Msg()
    # `oto_procedure` a rejoint la surface de corrélation (#1355) : l'exemple est un
    # outil qui en reste hors.
    msg.name = "oto_trigger"
    msg.arguments = {"op": "list", "_run_id": "run-de-quelqu-un-d-autre"}
    ctx = _Ctx()
    ctx.message = msg

    async def _next(c):
        vu["run"] = session_org.current_call_run()
        return None

    asyncio.run(CallContextMiddleware().on_call_tool(ctx, _next))
    assert vu["run"] is None
    # …et l'axe reste hors de la surface ANNONCÉE : le budget de `tools/list` ne
    # bouge pas d'un caractère pour ces 53 outils.
    assert "_run_id" not in {a.param for a in call_axes.axes_for("oto_trigger")}


# ── 5. `oto_call` le déclare lui-même, et le replie sur sa cible ─────────────

_CATALOGUE: dict = {}


def _catalogue():
    """Ce que charge le BOOT (`register_all`), pas un module seul — `oto_call` doit
    pouvoir RÉSOUDRE une cible, donc le catalogue entier."""
    if not _CATALOGUE:
        from fastmcp import FastMCP

        from oto_mcp.tools import register_all
        m = FastMCP("t-run-id-call")
        register_all(m)
        _CATALOGUE["mcp"] = m
    return _CATALOGUE["mcp"]


def test_oto_call_declare_le_jeton_dans_sa_signature_servie():
    """`SELF_HANDLED` est une liste par NOM : elle rouille en silence le jour où
    l'outil cesse de déclarer le paramètre — le middleware le lui mangerait alors
    avant le dispatch, sans que rien ne rougisse. La garde vit ici."""
    for nom in sorted(call_axes.SELF_HANDLED):
        outil = asyncio.run(_catalogue().get_tool(nom))
        props = (outil.parameters or {}).get("properties") or {}
        assert {"_org", "_run_id"} <= set(props), (nom, sorted(props))


def test_oto_call_replie_le_jeton_sur_sa_cible(monkeypatch):
    """`_run_id=` posé AU NIVEAU d'`oto_call` (là où le modèle voit son frère `_org`)
    atteint bien le contexte de la CIBLE, au lieu de faire échouer l'appel."""
    from fastmcp import Client

    from oto_mcp import session_org as so
    poses: list = []
    vrai = so.set_call_run
    monkeypatch.setattr(so, "set_call_run", lambda v: (poses.append(v), vrai(v))[1])

    async def _go():
        async with Client(_catalogue()) as c:
            # La cible échouera (aucun credential dans ce banc) : ce qu'on observe est
            # la POSE de l'axe, qui précède l'exécution de la cible.
            try:
                await c.call_tool("oto_call",
                                  {"name": "serper_search",
                                   "arguments": {"q": "x"},
                                   "_run_id": "run-replie"})
            except Exception:  # noqa: BLE001 — l'échec de la cible n'est pas le sujet
                pass
    asyncio.run(_go())
    assert "run-replie" in poses, poses
