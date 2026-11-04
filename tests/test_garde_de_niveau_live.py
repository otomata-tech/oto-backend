"""La garde de niveau (#1499) : ce qu'un appel exige est noté aux DEUX bords d'un appel et
lu par la résolution, sur chaque chemin — appel direct (middleware de contexte),
`oto_call` (`meta.executer_cible`), recette (chaque page est un `executer_cible`).

Sur une vraie base (`live`) : le serveur est un FastMCP de test dont chaque outil rend ce
que la résolution exigerait à l'instant de son exécution (`access.besoin()`) — lire →
`read`, écrire → `write`, envoyer ou détruire → `sensitive`. Le refus lui-même, sur le
compte résolu, est tenu par `tests/access/test_resolution_par_regles_1499.py`.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

# Importés AVANT que `serveur` ne double le lecteur d'identité : un module importé sous la
# doublure la lierait pour de bon (`tests/_etat_global`).
from oto_mcp.capabilities import _mcp_adapter  # noqa: F401
from oto_mcp.middleware import call_context  # noqa: F401
from oto_mcp.tools import meta  # noqa: F401

SUB = "sub-garde-de-niveau"


@pytest.fixture(scope="module")
def org(live):
    from oto_mcp import db
    from oto_mcp.org_store import members, orgs
    db.upsert_user(SUB, email=f"{SUB}@acme.test", name=SUB)
    o = orgs.create_org(f"Acme niveau {uuid.uuid4().hex[:4]}", created_by=SUB)
    members.add_org_member(o, SUB, "org_admin")
    return o


@pytest.fixture
def serveur(org, monkeypatch):
    from fastmcp import FastMCP

    from oto_mcp import access, call_axes
    from oto_mcp.auth import hooks
    from oto_mcp.tools import effets
    from oto_mcp.tools.lecture import LECTURE
    for mod in (access, hooks, call_axes):
        monkeypatch.setattr(mod, "current_user_sub_from_token", lambda: SUB, raising=False)
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: SUB)
    monkeypatch.setitem(effets.EFFETS, "acme_ecrire", "ecrire")
    monkeypatch.setitem(effets.EFFETS, "acme_envoyer", "envoyer")
    monkeypatch.setitem(effets.EFFETS, "acme_detruire", "detruire")
    m = FastMCP("t-garde-de-niveau")

    @m.tool(annotations=LECTURE)
    def acme_lire(x: str = "") -> dict:
        return {"content": [{"exige": access.besoin()}]}

    for nom in ("acme_ecrire", "acme_envoyer", "acme_detruire"):
        def _outil(x: str = "") -> dict:
            return {"content": [{"exige": access.besoin()}]}
        m.tool(name=nom)(_outil)

    @m.tool()
    def acme_sans_classe(x: str = "") -> dict:
        return {"content": [{"exige": access.besoin()}]}
    return m


ATTENDU = {"acme_lire": "read", "acme_ecrire": "write", "acme_envoyer": "sensitive",
           "acme_detruire": "sensitive"}


def _exige(resultat) -> str:
    data = getattr(resultat, "structured_content", None) or getattr(resultat, "data", None)
    if data is None and hasattr(resultat, "result"):
        return _exige(resultat.result)
    if isinstance(data, dict):
        return data["content"][0]["exige"]
    raise AssertionError(f"résultat illisible : {resultat!r}")


def _cible(m, name: str, args: dict):
    from oto_mcp.tools import meta

    async def _go():
        tool = await meta.resoudre_outil(m, name)
        return await meta.executer_cible(tool, SUB, name, name, dict(args))
    return asyncio.run(_go())


@pytest.mark.parametrize("outil", sorted(ATTENDU))
def test_oto_call_note_ce_que_la_cible_exige(serveur, outil):
    res = _cible(serveur, outil, {"x": "1"})
    assert res.ok, res
    assert _exige(res) == ATTENDU[outil]


def test_oto_call_vers_une_op_sans_classe_echoue_nommement(serveur):
    res = _cible(serveur, "acme_sans_classe", {"x": "1"})
    assert not res.ok and "no declared effect" in res.message


@pytest.mark.parametrize("outil", sorted(ATTENDU))
def test_l_appel_direct_note_ce_que_l_outil_exige(serveur, outil):
    """Le middleware de contexte note l'appel avant de passer la main au handler, avec ce
    que l'objet FastMCP déclare (lecture seule)."""
    from oto_mcp import access
    from oto_mcp.middleware.call_context import CallContextMiddleware

    class _Msg:
        pass

    class _Fctx:
        fastmcp = serveur

    class _Ctx:
        fastmcp_context = _Fctx()
    ctx, ctx.message = _Ctx(), _Msg()
    ctx.message.name, ctx.message.arguments = outil, {}

    async def _next(c):
        return access.besoin()
    assert asyncio.run(CallContextMiddleware(frozenset()).on_call_tool(ctx, _next)) == \
        ATTENDU[outil]


def test_une_recette_note_ce_que_chaque_page_exige(serveur, monkeypatch):
    """Une recette appelle chaque page par `executer_cible` (une recette ne sert que des
    outils en lecture) : la page note `read`, comme un appel direct."""
    from oto_mcp.recipes import moteur
    from oto_mcp.tools import meta
    vus = []
    orig = meta.executer_cible

    async def _espion(tool, sub, outil, demande, args):
        res = await orig(tool, sub, outil, demande, args)
        vus.append(_exige(res))
        return res
    monkeypatch.setattr(meta, "executer_cible", _espion)
    from oto_mcp.recipes import contrat
    corps = contrat.valider({"tool": "acme_lire", "source": {"items": "content"},
                             "map": {"exige": "exige"}, "key": {"column": "exige"},
                             "limits": {"max_units": 5}})
    asyncio.run(moteur.executer(corps, {}, fastmcp=serveur, sub=SUB, ecrire=False))
    assert vus == ["read"]
