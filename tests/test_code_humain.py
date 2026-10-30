"""Exécuter ou décider du code non publié est un geste de personne : sous un jeton de
délégation (un agent hébergé), `code_human_only` (`capabilities/_code_humain.py`)."""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import verrou_org
from oto_mcp.capabilities import functions as F
from oto_mcp.capabilities import recipes as R
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="operateur", org_id=2)


@pytest.fixture
def agent_heberge():
    jeton = verrou_org.poser(verrou_org.Verrou(sub="operateur", org_id=2, job_id=9))
    yield
    verrou_org.lever(jeton)


@pytest.mark.parametrize("op", ["test", "publish", "refuse"])
def test_une_fonction_ne_s_eprouve_ni_ne_se_decide_depuis_un_agent(agent_heberge, op):
    with pytest.raises(AuthzDenied) as e:
        asyncio.run(F._function(CTX, F.FunctionInput(op=op, slug="f", version=1)))
    assert (e.value.status, e.value.code) == (403, "code_human_only")


@pytest.mark.parametrize("op", ["test", "publish"])
def test_une_recette_code_ne_s_eprouve_ni_ne_se_publie_depuis_un_agent(agent_heberge, op):
    with pytest.raises(AuthzDenied) as e:
        asyncio.run(R._personne_pour_le_code({"kind": "code"}, op))
    assert (e.value.status, e.value.code) == (403, "code_human_only")


def test_une_recette_declarative_reste_ouverte_a_un_agent(agent_heberge):
    """Une recette sans code n'exécute rien de non publié : la règle ne la touche pas."""
    assert asyncio.run(R._personne_pour_le_code({"tool": "x"}, "publish")) is None


def test_hors_jeton_de_delegation_rien_ne_change(monkeypatch):
    monkeypatch.setattr(F, "run_in_threadpool", _rendu_direct)
    monkeypatch.setattr(F, "_dispatch", lambda ctx, inp: {"vu": inp.op})
    assert asyncio.run(F._function(CTX, F.FunctionInput(op="test", slug="f",
                                                        version=1))) == {"vu": "test"}


async def _rendu_direct(f, *a):
    return f(*a)


def test_les_deux_capacites_declarent_le_refus():
    from oto_mcp.capabilities import registry
    for cle in ("me.function", "me.recipe"):
        [cap] = [c for c in registry.CAPABILITIES if c.key == cle]
        assert any(e.code == "code_human_only" and e.status == 403 for e in cap.errors), cle
