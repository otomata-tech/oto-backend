"""`oto_doc` et `oto_procedure` reçoivent `_project=` — signal d'usage 1471 (13/11/2026).

Le bloc A et la notice disent « `_project=<id>` sur chaque appel ». Ces deux capacités
ne le déclaraient pas, et le schéma plat les faisait refuser AVANT le handler :

    _project — Unexpected keyword argument

Un agent qui suivait la consigne perdait son appel, à chaque fois (doublons 1544, 1578,
1096). Ce que ces tests tiennent :

  1. l'axe est lu ET annoncé sur ces deux outils, et seulement eux dans le spine ;
  2. sur la surface SERVIE (adaptateur de capacités + vrai `CallContextMiddleware`),
     le jeton ne fait plus refuser l'appel, et il pose le projet de l'appel ;
  3. sur `oto_doc`, le jeton tient lieu de `project_id` pour les ops par projet, un
     désaccord entre les deux est refusé nommément, et il ne désigne rien ailleurs.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import access, call_axes, session_org
from oto_mcp.capabilities._types import ResolvedCtx


def _lus(name: str) -> set[str]:
    return {a.param for a in call_axes.axes_for_call(name)}


def _annonces(name: str) -> set[str]:
    return {a.param for a in call_axes.axes_for_listing(name, {})}


# ── 1. La surface de l'axe ───────────────────────────────────────────────────

@pytest.mark.parametrize("nom", ["oto_doc", "oto_procedure"])
def test_doc_et_procedure_prennent_le_projet(nom):
    assert "_project" in _lus(nom)
    assert "_project" in _annonces(nom)


def test_l_entree_reste_nominative():
    # L'axe ne s'ouvre pas à tout le spine `oto_*` : chaque outil y entre avec sa raison.
    for nom in ("oto_whoami", "oto_trigger", "oto_create_org"):
        assert "_project" not in _lus(nom), nom


# ── 2. La surface servie ─────────────────────────────────────────────────────

_SERVEUR: dict = {}


def _serveur():
    if not _SERVEUR:
        from fastmcp import FastMCP

        from oto_mcp.capabilities import _mcp_adapter, registry
        m = FastMCP("t-project-spine")
        _mcp_adapter.register(m, registry.CAPABILITIES)
        _SERVEUR["mcp"] = m
        _SERVEUR["org"] = _mcp_adapter.reserved_org_tool_names(registry.CAPABILITIES)
    return _SERVEUR


class _Msg:
    pass


class _Ctx:                         # comme le vrai MiddlewareContext : PAS de get_state
    pass


def _appel(nom: str, arguments: dict, suivant=None):
    from oto_mcp.middleware.call_context import CallContextMiddleware

    srv = _serveur()
    msg = _Msg()
    msg.name = nom
    msg.arguments = dict(arguments)
    ctx = _Ctx()
    ctx.message = msg

    async def _next(c):
        if suivant is not None:
            return suivant(c)
        outil = await srv["mcp"].get_tool(nom)
        return await outil.run(c.message.arguments)

    return asyncio.run(CallContextMiddleware(srv["org"]).on_call_tool(ctx, _next))


def _motif(nom: str, arguments: dict) -> str:
    try:
        _appel(nom, arguments)
        return ""
    except Exception as e:  # noqa: BLE001 — c'est le motif qu'on inspecte
        return str(e)


@pytest.fixture
def _projet_lisible(monkeypatch):
    """La garde du jeton passe (projet 5, org 42) : ce banc ne porte pas sur elle
    (`test_call_axes_project.py`), mais sur ce qui vient après."""
    monkeypatch.setattr(call_axes, "require_axis_sub", lambda axis: "u")
    monkeypatch.setattr(session_org, "current_subdomain_candidate", lambda: None)
    monkeypatch.setattr(call_axes, "_resolve_project_context_guarded",
                        lambda sub, pid, cand: (42, None, None))


@pytest.mark.parametrize("nom", ["oto_doc", "oto_procedure"])
def test_le_jeton_ne_refuse_plus_l_appel(nom, _projet_lisible):
    """L'appel bute là où il buterait SANS le jeton (rien n'est authentifié dans ce
    banc) : c'est ce qui prouve que `_project` n'est plus la cause."""
    sans = _motif(nom, {"op": "list"})
    avec = _motif(nom, {"op": "list", "_project": 5})
    assert "Unexpected keyword argument" not in avec, avec
    assert avec == sans, (nom, sans, avec)


@pytest.mark.parametrize("nom", ["oto_doc", "oto_procedure"])
def test_le_jeton_pose_le_projet_et_disparait_des_arguments(nom, _projet_lisible):
    """Le handler voit le projet de l'appel (`access.current_project`, ce que lit le
    bloc `project_instance` d'`oto_procedure`), et plus le jeton dans ses arguments.
    L'org du projet n'est PAS co-posée sur ces deux outils : leurs listes la liraient
    comme celle de l'appelant (`test_projet_partage_hors_org_listes`)."""
    vu: dict = {}

    def _suivant(c):
        vu["args"] = dict(c.message.arguments)
        vu["projet"] = access.current_project()
        vu["org"] = session_org.current_call_org()

    _appel(nom, {"op": "list", "_project": 5}, _suivant)
    assert vu == {"args": {"op": "list"}, "projet": 5, "org": None}
    assert session_org.current_call_project() is None      # reset après l'appel


# ── 3. `oto_doc` : le jeton tient lieu de `project_id` ───────────────────────

@pytest.fixture
def _sous_projet():
    def _poser(pid):
        return session_org.set_call_project(pid)
    jetons: list = []
    yield lambda pid: jetons.append(_poser(pid))
    for tok in reversed(jetons):
        session_org.reset_call_project(tok)


def _doc(monkeypatch, **champs):
    """Le dispatcher `me.doc`, la lecture par projet remplacée par un témoin."""
    from oto_mcp.capabilities.docs import core, reads
    vu: dict = {}
    monkeypatch.setattr(reads, "liste", lambda sub, inp: vu.setdefault("inp", inp))
    out = core._doc(ResolvedCtx(sub="u", org_id=42, role="member"),
                    core.DocInput(**champs))
    return vu.get("inp"), out


def test_le_jeton_tient_lieu_de_project_id(monkeypatch, _sous_projet):
    _sous_projet(5)
    inp, _ = _doc(monkeypatch, op="list")
    assert inp.project_id == 5


def test_les_deux_d_accord_passent(monkeypatch, _sous_projet):
    _sous_projet(5)
    inp, _ = _doc(monkeypatch, op="list", project_id=5)
    assert inp.project_id == 5


def test_les_deux_en_desaccord_sont_refuses(monkeypatch, _sous_projet):
    from oto_mcp.capabilities._types import AuthzDenied
    _sous_projet(5)
    with pytest.raises(AuthzDenied) as e:
        _doc(monkeypatch, op="list", project_id=6)
    assert e.value.code == "project_conflict"
    assert "_project" in str(e.value.message) and "project_id" in str(e.value.message)


def test_sans_jeton_rien_ne_change(monkeypatch):
    from oto_mcp.capabilities._types import AuthzDenied
    from oto_mcp.capabilities.docs import core
    with pytest.raises(AuthzDenied) as e:
        core._doc(ResolvedCtx(sub="u", org_id=42, role="member"), core.DocInput(op="list"))
    assert e.value.code == "missing_project"
    assert "_project" in str(e.value.message)      # le refus nomme les deux portes


@pytest.mark.parametrize("op", ["create", "bulk_create", "list", "search"])
def test_les_ops_par_projet_le_recoivent(op, _sous_projet):
    from oto_mcp.capabilities.docs import core
    _sous_projet(5)
    assert core._projet_de_l_appel(core.DocInput(op=op)).project_id == 5


@pytest.mark.parametrize("op", ["get", "update", "delete", "move", "shared_with_me"])
def test_ailleurs_le_jeton_ne_designe_rien(op, _sous_projet):
    # Une op par `doc_id` trouve son projet par la page ; `shared_with_me` REFUSE un
    # `project_id` — le jeton, contexte de l'appel, ne doit pas l'y faire entrer.
    from oto_mcp.capabilities.docs import core
    _sous_projet(5)
    assert core._projet_de_l_appel(core.DocInput(op=op)).project_id is None
