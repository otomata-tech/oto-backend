"""fr_search — la forme juridique dite devant le nom (feedback #325).

« SCI ASC » ramène 38 sociétés littéralement NOMMÉES "SCI ASC" et jamais la SCI
immatriculée "ASC" (SIREN 921960159, forme 6540) — or c'est ainsi qu'on désigne
une SCI à l'oral. Mesuré le 30/07/2026 : q=ASC seul rend 1580 résultats (cible
hors page 1), q=ASC + nature_juridique=6540 en rend 98, cible en page 1. La forme
appartient donc à un filtre ; le tool le fait tout seul sur la page 1, sans
sacrifier les résultats littéraux qui restent légitimes.
"""
from __future__ import annotations

import pytest

from france_opendata.compact import split_legal_form as _split_legal_form


class _Reg:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        if a and callable(a[0]):
            return deco(a[0])
        return deco


def _company(siren, nom, nj):
    return {"siren": siren, "nom_complet": nom, "nom_raison_sociale": nom,
            "nature_juridique": nj, "siege": {}, "dirigeants": [],
            "matching_etablissements": []}


LITERAL = [_company(f"87812469{i}", "SCI ASC", "6540") for i in range(2)]
TARGET = _company("921960159", "ASC", "6540")


@pytest.fixture()
def fr_search(monkeypatch):
    """Enregistre les tools fr avec un client entreprises scriptable ;
    renvoie (tool, journal des appels amont)."""
    calls = []

    class _Entreprises:
        def __init__(self, script):
            self.script = script

        def search(self, **kw):
            calls.append(kw)
            return self.script(kw)

        def get_by_siren(self, siren):
            return None

        def get_directors(self, siren):
            return []

    from oto_mcp.fod import fr as fod_fr
    from oto_mcp.tools import fr

    def _make(script):
        monkeypatch.setattr(fod_fr, "entreprises", _Entreprises(script))
        reg = _Reg()
        fr.register(reg)
        return reg.tools["fr_search"]

    return _make, calls


# ── détection du préfixe ──

def test_prefix_detection():
    assert _split_legal_form("SCI ASC") == ("SCI", "ASC")
    assert _split_legal_form("sarl dupont et fils") == ("SARL", "dupont et fils")
    assert _split_legal_form("SCI") is None            # pas de nom à chercher
    assert _split_legal_form("ASCENSEURS RENOV") is None  # pas un préfixe isolé
    assert _split_legal_form(None) is None


# ── comportement du tool ──

def test_legal_form_hits_are_appended_and_flagged(fr_search):
    make, calls = fr_search

    def script(kw):
        if kw.get("nature_juridique"):
            return {"results": [TARGET], "total_results": 98}
        return {"results": list(LITERAL), "total_results": 38}

    out = make(script)(query="SCI ASC")

    sirens = [r["siren"] for r in out["results"]]
    assert sirens[:2] == [c["siren"] for c in LITERAL]   # littéraux d'abord
    assert "921960159" in sirens                          # …et la cible enfin visible
    assert out["results"][-1]["matched_by"] == "legal_form"
    assert out["legal_form_retry"] == {
        "form": "SCI", "query": "ASC",
        "nature_juridique": ["6540", "6541", "6542", "6543", "6544"],
        "total_results": 98, "added": 1,
    }
    assert calls[1]["query"] == "ASC" and calls[1]["nature_juridique"][0] == "6540"


def test_duplicates_are_not_appended_twice(fr_search):
    make, _ = fr_search
    out = make(lambda kw: {"results": [TARGET], "total_results": 1})(query="SCI ASC")
    assert [r["siren"] for r in out["results"]] == ["921960159"]
    assert out["legal_form_retry"]["added"] == 0


def test_explicit_nature_juridique_is_passed_through_without_retry(fr_search):
    """L'appelant qui a déjà tranché la forme n'est pas doublé."""
    make, calls = fr_search
    out = make(lambda kw: {"results": [TARGET], "total_results": 98})(
        query="ASC", nature_juridique="6540")
    assert len(calls) == 1 and calls[0]["nature_juridique"] == ["6540"]
    assert "legal_form_retry" not in out


def test_retry_only_on_first_page(fr_search):
    """Page ≥ 2 = énumération en cours : y injecter d'autres résultats casserait
    la pagination. C'est en page 1 qu'on conclut « pas trouvée »."""
    make, calls = fr_search
    out = make(lambda kw: {"results": list(LITERAL), "total_results": 38})(
        query="SCI ASC", page=2)
    assert len(calls) == 1 and "legal_form_retry" not in out


def test_plain_query_is_untouched(fr_search):
    make, calls = fr_search
    out = make(lambda kw: {"results": [TARGET], "total_results": 1})(query="ASC")
    assert len(calls) == 1 and "legal_form_retry" not in out
