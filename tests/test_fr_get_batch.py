"""fr_get en lot (feedback #143) : `sirens=[…]` qualifie une liste en un appel —
profils dans l'ordre d'entrée, échec par-SIREN dégradé sans faire tomber le lot,
bornes d'entrée en McpError INVALID_PARAMS. Proxies FOD (fod_fr) stubés (pas de
réseau) — les données entreprise passent par le service FOD depuis B2a (ADR 0028)."""
from __future__ import annotations

import pytest
from oto_mcp.mcp_errors import McpError
class _Reg:
    """FastMCP minimal : capture les fonctions décorées par @mcp.tool()."""

    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        if a and callable(a[0]):  # @mcp.tool sans parenthèses
            return deco(a[0])
        return deco


class _Entreprises:
    def __init__(self, *a, **k): ...

    def get_by_siren(self, siren):
        if siren == "000000000":
            raise RuntimeError("identity down")
        return {"siren": siren, "nom_complet": f"Boite {siren}"}


class _Inpi:
    def __init__(self, *a, **k): ...

    def list_exercises(self, siren):
        return []


class _Bodacc:
    def __init__(self, *a, **k): ...

    def search_by_siren(self, siren, famille, limit):
        return {"results": [], "total_count": 0}


class _Noop:
    def __init__(self, *a, **k): ...


@pytest.fixture()
def fr_get(monkeypatch):
    # Les données entreprise passent par les proxies FOD (fod_fr) depuis B2a :
    # on stube les proxies, pas les classes de clients lib. SIRENE (keyé) reste
    # in-process via oto.tools.sirene.SireneClient — non touché par fr_get.
    monkeypatch.setattr("oto_mcp.fod.fr.entreprises", _Entreprises())
    monkeypatch.setattr("oto_mcp.fod.fr.inpi", _Inpi())
    monkeypatch.setattr("oto_mcp.fod.fr.bodacc", _Bodacc())
    monkeypatch.setattr("oto_mcp.fod.fr.egapro", _Noop())
    monkeypatch.setattr("oto.tools.sirene.SireneClient", _Noop)
    from oto_mcp.tools import fr
    reg = _Reg()
    fr.register(reg)
    return reg.tools["fr_get"]


def test_single_mode_unchanged(fr_get):
    out = fr_get(siren="123456789")
    assert out["siren"] == "123456789"
    assert out["identity"]["siren"] == "123456789"


def test_batch_returns_profiles_in_order(fr_get):
    out = fr_get(sirens=["111111111", "222222222", "333333333"])
    assert out["count"] == 3
    assert [p["siren"] for p in out["profiles"]] == [
        "111111111", "222222222", "333333333"]


def test_batch_failure_is_per_siren(fr_get):
    # 000000000 fait planter la source identité → erreur DANS le lot, pas du lot
    out = fr_get(sirens=["111111111", "000000000"])
    assert out["count"] == 2
    assert out["profiles"][0]["identity"]["siren"] == "111111111"
    assert out["profiles"][1]["error"] == "identity_unavailable"
    assert out["profiles"][1]["siren"] == "000000000"


def test_input_guards(fr_get):
    with pytest.raises(McpError, match="not both"):
        fr_get(siren="1", sirens=["2"])
    with pytest.raises(McpError, match="not both"):
        fr_get()
    with pytest.raises(McpError, match="liste vide"):
        fr_get(sirens=["  "])
    with pytest.raises(McpError, match="20 au plus"):
        fr_get(sirens=[str(i).zfill(9) for i in range(21)])
