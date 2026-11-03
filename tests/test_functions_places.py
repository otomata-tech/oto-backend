"""Les places du bac à sable (`functions/places.py`, 03/11/2026) : combien d'exécutions
tournent en même temps.

Chaque exécution était bornée (mémoire, durée) mais pas leur NOMBRE : tout compte pouvait
en lancer N à la fois. Ces bancs tiennent les trois règles — un plafond par processus,
une exécution par compte, un refus IMMÉDIAT (429 `sandbox_busy` + `Retry-After`) sans
rien lancer — et la libération de la place quoi qu'il arrive à l'exécution. Ils tournent
sans Deno : ce qu'on éprouve est le compte des places, pas le bac à sable.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from oto_mcp.capabilities import _bac_occupe
from oto_mcp.capabilities import functions as cap_fonctions
from oto_mcp.capabilities import recipes as cap_recettes
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.functions import executor, places, pont

_VERSION = {"sources": {"f.py": "def executer(e):\n    return {}\n"},
            "entrypoint": "f:executer", "requirements": []}


@pytest.fixture(autouse=True)
def places_neuves(monkeypatch):
    """Chaque banc part d'un processus sans exécution en cours, plafond relu."""
    monkeypatch.delenv(places.ENV_MAX, raising=False)
    places.plafond.cache_clear()
    assert places.en_cours() == 0, "un banc précédent a gardé une place"
    yield
    places.plafond.cache_clear()
    assert places.en_cours() == 0, "une place n'a pas été rendue"


def _plafond(monkeypatch, n: int) -> None:
    monkeypatch.setenv(places.ENV_MAX, str(n))
    places.plafond.cache_clear()


# ── Le compte des places ──────────────────────────────────────────────────────

def test_le_defaut_est_quatre():
    assert places.plafond() == places.MAX_DEFAUT == 4


def test_le_plafond_global_atteint_refuse_un_autre_compte(monkeypatch):
    _plafond(monkeypatch, 2)
    with places.place("a"), places.place("b"):
        with pytest.raises(places.BacASableOccupe) as e:
            with places.place("c"):
                pytest.fail("une troisième exécution est entrée sous un plafond de 2")
        assert e.value.retry_after == places.ATTENTE_S
        assert places.en_cours() == 2, "le refus a pris une place"
    with places.place("c"):
        pass


def test_le_meme_compte_deux_fois_la_seconde_est_refusee():
    with places.place("a"):
        with pytest.raises(places.BacASableOccupe, match="this account"):
            with places.place("a"):
                pytest.fail("un compte a tenu deux exécutions à la fois")
        assert places.en_cours() == 1


def test_deux_comptes_sous_le_plafond_passent(monkeypatch):
    _plafond(monkeypatch, 2)
    with places.place("a"), places.place("b"):
        assert places.en_cours() == 2


def test_le_verrou_tient_entre_fils(monkeypatch):
    """L'aller simple prend sa place dans un fil (`run_in_threadpool`) : des fils qui se
    disputent la dernière place n'en prennent qu'une."""
    _plafond(monkeypatch, 1)
    depart, entres, refuses = threading.Barrier(8), [], []
    libere = threading.Event()

    def essayer(sub):
        depart.wait()
        try:
            with places.place(sub):
                entres.append(sub)
                libere.wait(5)
        except places.BacASableOccupe:
            refuses.append(sub)

    fils = [threading.Thread(target=essayer, args=(f"s{i}",)) for i in range(8)]
    for f in fils:
        f.start()
    while len(entres) + len(refuses) < 8 and len(refuses) < 7:
        threading.Event().wait(0.01)
    libere.set()
    for f in fils:
        f.join()
    assert len(entres) == 1 and len(refuses) == 7


@pytest.mark.parametrize("brut", ["abc", "0", "-1", "2.5"])
def test_un_reglage_illisible_leve(monkeypatch, brut):
    monkeypatch.setenv(places.ENV_MAX, brut)
    places.plafond.cache_clear()
    with pytest.raises(places.ReglageInvalide, match=places.ENV_MAX):
        places.verifier_configuration()


# ── La place est rendue quoi qu'il arrive ─────────────────────────────────────

def test_la_place_est_rendue_apres_une_exception():
    with pytest.raises(RuntimeError):
        with places.place("a"):
            raise RuntimeError("le code a planté")
    assert places.en_cours() == 0


def test_l_aller_simple_rend_sa_place_apres_un_delai_depasse(monkeypatch, tmp_path):
    monkeypatch.setattr(executor, "_repertoire", lambda: tmp_path)
    monkeypatch.setattr(executor, "_roues", lambda racine, requirements: [])
    monkeypatch.setattr(executor, "lancement", lambda racine, script, duree: None)

    def delai(lance, entree):
        assert places.en_cours() == 1, "le processus tourne sans tenir de place"
        raise executor.ExecutionEchouee(f"délai dépassé ({executor.DELAI_S} s)")

    monkeypatch.setattr(executor, "_jouer", delai)
    with pytest.raises(executor.ExecutionEchouee, match="délai"):
        executor.executer(mode="run", sub="a", **_VERSION)
    assert places.en_cours() == 0


def test_le_pont_rend_sa_place_apres_un_delai_et_une_annulation(monkeypatch):
    async def delai(**_kw):
        assert places.en_cours() == 1
        raise executor.ExecutionEchouee("délai dépassé (10 s)")

    monkeypatch.setattr(pont, "_executer_avec_pont", delai)
    with pytest.raises(executor.ExecutionEchouee):
        asyncio.run(pont.executer_avec_pont(sources={}, entrypoint="r:run",
                                            appeler=None, sub="a"))
    assert places.en_cours() == 0

    async def sans_fin(**_kw):
        await asyncio.sleep(30)

    monkeypatch.setattr(pont, "_executer_avec_pont", sans_fin)

    async def annuler():
        tache = asyncio.ensure_future(pont.executer_avec_pont(
            sources={}, entrypoint="r:run", appeler=None, sub="a"))
        await asyncio.sleep(0.05)
        assert places.en_cours() == 1
        tache.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tache

    asyncio.run(annuler())
    assert places.en_cours() == 0


# ── Le refus servi : 429 `sandbox_busy`, Retry-After, rien de lancé ────────────

def test_oto_function_refuse_en_429_sans_rien_lancer(monkeypatch, tmp_path):
    _plafond(monkeypatch, 1)
    monkeypatch.setattr(executor, "_repertoire", lambda: tmp_path)
    monkeypatch.setattr(executor, "_roues", lambda racine, requirements: [])
    lances = []
    monkeypatch.setattr(executor, "lancement", lambda *a: lances.append(a))
    with places.place("autre"):
        with pytest.raises(AuthzDenied) as e:
            cap_fonctions._executer(ResolvedCtx(sub="u"), _VERSION, "test")
    refus = e.value
    assert (refus.status, refus.code) == (429, "sandbox_busy")
    assert refus.retry_after == places.ATTENTE_S
    assert refus.details == {"retryable": True, "retry_after_seconds": places.ATTENTE_S}
    assert lances == [], "un processus a été lancé malgré le refus"


def test_une_recette_code_refuse_en_429_sans_rien_lancer(monkeypatch, tmp_path):
    from oto_mcp import outils_du_travail
    monkeypatch.setattr(outils_du_travail, "manquants", lambda cites: None)
    monkeypatch.setattr(cap_recettes, "_instance", lambda: object())
    monkeypatch.setattr(executor, "repertoire", lambda: tmp_path)
    monkeypatch.setattr(executor, "roues", lambda racine, requirements: [])
    monkeypatch.setattr(executor, "chemin_pyodide", lambda racine: tmp_path)
    lances = []
    monkeypatch.setattr(executor, "lancement", lambda *a: lances.append(a))
    corps = {"kind": "code", "sources": {"r.py": "async def run(oto, p):\n    return 1\n"},
             "entrypoint": "r:run", "params": {}, "writes": [],
             "limits": {"max_units": 10, "max_seconds": 10, "max_writes": 0}}
    with places.place("u"):
        with pytest.raises(AuthzDenied) as e:
            asyncio.run(cap_recettes._executer(ResolvedCtx(sub="u"),
                                               cap_recettes.RecipeInput(op="test"),
                                               corps, ecrire=False))
    assert (e.value.status, e.value.code, e.value.retry_after) == (
        429, "sandbox_busy", places.ATTENTE_S)
    assert lances == []


def test_la_face_rest_pose_retry_after():
    from pydantic import BaseModel

    from oto_mcp.api.base import _json, _json_error
    from oto_mcp.capabilities import _rest_adapter
    from oto_mcp.capabilities._types import Capability, RestBinding
    from starlette.requests import Request

    class _In(BaseModel):
        op: str = "run"

    def _plein(ctx, inp):
        raise _bac_occupe.refus(places.BacASableOccupe("sandbox_busy: full"))

    cap = Capability(key="test.occupe", Input=_In, handler=_plein,
                     authz=lambda raw, inp: ResolvedCtx(sub=raw.sub),
                     rest=RestBinding(verb="POST", path="/api/test/occupe"))

    async def _auth(request, verifier):
        return "u-1", None

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    handler = _rest_adapter._make_handler(cap, cap.rest, object(), _auth, _json,
                                          _json_error)
    rep = asyncio.run(handler(Request({
        "type": "http", "method": "POST", "path": "/api/test/occupe", "headers": [],
        "query_string": b"", "root_path": "", "scheme": "http",
        "server": ("test", 80)}, _receive)))
    assert rep.status_code == 429
    assert rep.headers["Retry-After"] == str(places.ATTENTE_S)
    import json
    corps = json.loads(bytes(rep.body))
    assert corps["error"] == "sandbox_busy"
    assert corps["details"]["retry_after_seconds"] == places.ATTENTE_S


def test_la_face_mcp_porte_retry_after_dans_ses_donnees():
    from fastmcp import Client, FastMCP
    from pydantic import BaseModel

    from oto_mcp.capabilities import _mcp_adapter
    from oto_mcp.capabilities._types import Capability

    class _In(BaseModel):
        op: str = "run"

    def _plein(ctx, inp):
        raise _bac_occupe.refus(places.BacASableOccupe("sandbox_busy: full"))

    mcp = FastMCP("occupe")
    _mcp_adapter.register(mcp, [Capability(
        key="test.occupe", Input=_In, handler=_plein,
        authz=lambda raw, inp: ResolvedCtx(sub="u1"), mcp="test_occupe")])

    async def appeler():
        async with Client(mcp) as c:
            return await c.call_tool_mcp("test_occupe", {"op": "run"})

    try:
        rendu = asyncio.run(appeler())
    except Exception as e:  # noqa: BLE001 — la forme dépend du client : on lit l'erreur
        erreur = getattr(e, "error", None)
        assert erreur is not None, repr(e)
        assert erreur.data == {"code": "sandbox_busy",
                               "retry_after_seconds": places.ATTENTE_S}
        return
    assert rendu.isError
    assert "sandbox_busy" in rendu.content[0].text
