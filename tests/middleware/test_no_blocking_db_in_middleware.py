"""Garde-fou perf (gel de prod du 15/08) : un MIDDLEWARE ne touche pas la base
depuis le thread de l'event loop.

Pourquoi un garde-fou À PART de `test_no_blocking_async_handlers` : celui-là
énumère les `@mcp.tool` et accepte un `async def` dès qu'il contient un `await`
dans son propre scope. Un middleware fastmcp échappe aux DEUX critères — il n'est
pas un tool (jamais énuméré), et son hook DOIT `await call_next(context)` (donc il
passerait le critère même si on l'énumérait). C'est par cette porte qu'est passé
`DynamicInstructionsMiddleware.on_initialize`, qui composait l'artefact de session
— soit la cascade de statut de TOUS les connecteurs — dans la boucle : sous ~8
clients lourds, chaque handshake gelait le serveur entier (502 en rafale, py-spy
en flagrant délit sur le MainThread). Cf. `docs/event-loop-perf.md`.

Détection : on ne lit pas le source, on OBSERVE. Le seam unique d'emprunt de
connexion (`db._conn._get_pool`) est remplacé par un mouchard qui note le thread
appelant puis refuse la connexion — chaque appelant retombe alors sur son
fail-open. Un chemin est vert ssi (a) il a réellement tenté d'atteindre la base
(sinon la garde est inerte et son vert ne vaut rien) et (b) aucune de ces
tentatives ne vient du thread de la boucle. Auto-maintenu à profondeur
quelconque : peu importe par combien d'appels intermédiaires le middleware
descend jusqu'à `psycopg`, c'est le thread au moment de l'emprunt qui tranche.

Couvre aujourd'hui le chemin CHAUD relevé en production (`on_initialize`).
Ajouter un hook = une fonction de plus ici, même montage.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
import types

import pytest

from oto_mcp.middleware import dynamic_instructions as mw
from oto_mcp.middleware import disabled_tools as dis_mw
from oto_mcp.db import _conn


class _PasDeBaseEnTest(RuntimeError):
    """Refus volontaire du mouchard : la suite ne dispose d'aucun PostgreSQL."""


class _MouchardDePool:
    """Faux pool : note le thread qui emprunte une connexion, puis refuse.

    `_connect()` fait `pool = _get_pool()` puis `with pool.connection()` — lever
    dès `connection()` suffit, et remonte à l'appelant comme un incident DB
    ordinaire (donc absorbé par les fail-open du chemin, ce qu'on veut : le test
    mesure le THREAD, pas le résultat)."""

    def __init__(self) -> None:
        self.threads: list[threading.Thread] = []

    def connection(self):
        self.threads.append(threading.current_thread())
        raise _PasDeBaseEnTest("le mouchard ne sert jamais de connexion")


@pytest.fixture
def mouchard(monkeypatch) -> _MouchardDePool:
    m = _MouchardDePool()
    monkeypatch.setattr(_conn, "_get_pool", lambda: m)
    return m


def _joue(fabrique_coro):
    """Exécute la coroutine dans une boucle neuve et rend le thread de CETTE boucle."""
    porteur: dict = {}

    async def _run():
        porteur["thread"] = threading.current_thread()
        return await fabrique_coro()

    asyncio.run(_run())
    return porteur["thread"]


def _assert_hors_boucle(mouchard: _MouchardDePool, boucle, chemin: str) -> None:
    assert mouchard.threads, (
        f"garde INERTE sur {chemin} : le chemin n'a même pas tenté d'atteindre la "
        "base — revoir le montage du test avant de conclure au vert")
    coupables = [t for t in mouchard.threads if t is boucle]
    assert not coupables, (
        f"{len(coupables)} accès DB depuis le thread de l'event loop dans {chemin} : "
        "le serveur est mono-loop, toute résolution/composition sync d'un middleware "
        "doit passer par `run_in_threadpool` (cf. docs/event-loop-perf.md — gel de "
        "prod du 15/08, 502 en rafale sous charge de campagne)")


def test_on_initialize_ne_touche_pas_la_base_dans_la_boucle(mouchard, monkeypatch):
    """Le handshake compose l'artefact A/C (cascade de statut de tous les
    connecteurs) — hors boucle, sinon il gèle tout le serveur le temps de la
    composition."""
    monkeypatch.setattr(mw, "current_user_sub_from_token", lambda: "u-perf")

    async def call_next(ctx):
        return types.SimpleNamespace(instructions="BASE")

    boucle = _joue(
        lambda: mw.DynamicInstructionsMiddleware().on_initialize(object(), call_next))
    _assert_hors_boucle(mouchard, boucle,
                        "DynamicInstructionsMiddleware.on_initialize")


def test_on_list_tools_ne_touche_pas_la_base_dans_la_boucle(mouchard, monkeypatch):
    """`tools/list` appende à deux descriptions d'outils l'index de prose chargeable —
    résolution d'org, puis un index par outil, chacun cumulant plusieurs paliers
    (perso, org, équipe depuis le 09/09/2026). Autant de lectures SYNC, sur un hook
    joué à chaque connexion et à chaque `tools/list_changed`.

    Ce hook a échappé à la garde jusqu'ici : `on_initialize` était le seul couvert,
    et l'asymétrie ne se voyait nulle part."""
    monkeypatch.setattr(mw, "current_user_sub_from_token", lambda: "u-perf")

    class _Tool:
        def __init__(self, name):
            self.name, self.description = name, "desc"

        def model_copy(self, update):
            return self

    async def call_next(ctx):
        return [_Tool("oto_procedure"), _Tool("oto_guide")]

    boucle = _joue(
        lambda: mw.DynamicInstructionsMiddleware().on_list_tools(object(), call_next))
    _assert_hors_boucle(mouchard, boucle,
                        "DynamicInstructionsMiddleware.on_list_tools")


def test_le_mouchard_mord(mouchard):
    """Contrôle : le MÊME travail appelé nûment dans la boucle EST attrapé.

    Sans ce contrôle, le test ci-dessus pourrait rester vert pour de mauvaises
    raisons (mouchard mal branché, chemin qui ne touche plus la base)."""
    from oto_mcp import access

    async def naif():
        # Ce que faisait le middleware avant le correctif : résolution d'org sync,
        # dans la boucle.
        with contextlib.suppress(Exception):
            access.current_org("u-perf")

    boucle = _joue(lambda: naif())
    assert any(t is boucle for t in mouchard.threads), (
        "le mouchard n'a rien vu sur un accès DB pourtant fait DANS la boucle : "
        "la détection est cassée, les autres tests de ce module ne prouvent rien")


def test_on_call_tool_avec_un_run_ne_touche_pas_la_base_dans_la_boucle(mouchard, monkeypatch):
    """#639 : un appel qui porte `_run_id` (sans `_org`) fait résoudre l'org du run —
    une lecture de `runs` et une garde d'appartenance — par le middleware, donc sur le
    chemin inbound de CHAQUE appel du runner. Hors boucle, comme la garde de `_org=`."""
    import uuid

    from oto_mcp import call_axes
    from oto_mcp.middleware.call_context import CallContextMiddleware

    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: "u-perf")
    ctx = types.SimpleNamespace(message=types.SimpleNamespace(
        name="data_rows", arguments={"_run_id": "run-perf-" + uuid.uuid4().hex[:8]}))

    async def call_next(c):
        return None

    async def go():
        # Le mouchard refuse la connexion : la résolution échoue en refus propre
        # (McpError), ce qui est le comportement voulu — ici on ne mesure que le thread.
        with contextlib.suppress(Exception):
            await CallContextMiddleware().on_call_tool(ctx, call_next)

    boucle = _joue(go)
    _assert_hors_boucle(mouchard, boucle, "CallContextMiddleware.on_call_tool (_run_id)")


def test_user_disabled_tools_on_initialize_ne_touche_pas_la_base_dans_la_boucle(
        mouchard, monkeypatch):
    """`UserDisabledToolsMiddleware.on_initialize` → `session_visibility.
    compute_hidden_layers` : résolution d'org, deux listes de toggles, le rôle
    plateforme, puis (après le seul `await` du calcul, `ctx.fastmcp.list_tools`)
    les denylists admin/équipe, l'activation connecteur, le RBAC org et équipe, la
    sélection marketplace (qui peut ÉCRIRE le seed) et l'option bêta — une bonne
    dizaine de lectures/écritures SQL synchrones, sur le hook joué à CHAQUE
    handshake `initialize`. Nommé « reste à traiter » dans `docs/event-loop-perf.md`
    (mode n°2) depuis le 15/08 ; c'est le même mode de gel que
    `DynamicInstructionsMiddleware`, resté ouvert parce que ce hook-ci n'était
    couvert par aucun des deux garde-fous existants."""
    monkeypatch.setattr(dis_mw, "current_user_sub_from_token", lambda: "u-perf")

    async def _list_tools(*, run_middleware=False):
        return []

    fastmcp_ctx = types.SimpleNamespace(
        fastmcp=types.SimpleNamespace(list_tools=_list_tools))
    context = types.SimpleNamespace(fastmcp_context=fastmcp_ctx)

    async def call_next(ctx):
        return types.SimpleNamespace()

    boucle = _joue(
        lambda: dis_mw.UserDisabledToolsMiddleware().on_initialize(context, call_next))
    _assert_hors_boucle(mouchard, boucle, "UserDisabledToolsMiddleware.on_initialize")
