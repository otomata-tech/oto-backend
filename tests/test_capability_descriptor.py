"""Garde-fous du descripteur de capacité (ADR 0009) — purs, sans DB."""
import pytest
from pydantic import BaseModel

from oto_mcp.capabilities._authz import SUB_ONLY
from oto_mcp.capabilities._types import Capability, RestBinding


class _In(BaseModel):
    x: int = 0


def _h(ctx, inp):
    return {}


def test_capability_requires_a_surface():
    # mcp=None ET rest=None → opt-out non explicite → rejet au boot.
    with pytest.raises(ValueError):
        Capability(key="k", handler=_h, Input=_In, authz=SUB_ONLY)


def test_capability_requires_authz():
    # authz est obligatoire (pas de défaut) → oubli = TypeError au chargement.
    with pytest.raises(TypeError):
        Capability(key="k", handler=_h, Input=_In, mcp="t")  # type: ignore[call-arg]


def test_capability_ok_with_one_surface():
    c = Capability(key="k", handler=_h, Input=_In, authz=SUB_ONLY, mcp="t")
    assert c.mcp == "t" and c.rest is None


def test_capability_ok_rest_only():
    c = Capability(key="k", handler=_h, Input=_In, authz=SUB_ONLY,
                   rest=RestBinding("GET", "/api/x"))
    assert c.rest.verb == "GET" and c.mcp is None
