"""Le RÉSULTAT de la taxonomie dans le journal (oto#25 lot b1).

`tool_calls.error` est un texte brut tronqué — `error_taxonomy.classify(exc)` sait
déjà DÉRIVER une catégorie structurée depuis une exception (ex. `not_authorized` sur
un 401/403 amont) mais son résultat n'était écrit nulle part en base. Ce lot ajoute
la colonne `error_kind` et la fait porter par `calllog._record`.

Ce sont, comme pour le discriminant #117, des INSTRUMENTS DE MESURE : une
régression silencieuse ne casse rien, elle rend `error_kind` NULL partout — d'où des
tests de forme sur la ligne émise, pas seulement « ça ne lève pas ».

Logique pure : sink stubbé, aucun accès DB (convention `CLAUDE.md` §Tests).
"""
import asyncio
import types

import pytest

from oto_mcp import calllog


class _RefusAmont401(Exception):
    """Exception connecteur porteuse d'un statut HTTP amont (`_upstream_status`
    la reconnaît par l'attribut `status_code`, comme `UpstreamHTTPError`)."""
    status_code = 401


def _context(tool="fr_get", arguments=None):
    return types.SimpleNamespace(
        message=types.SimpleNamespace(name=tool, arguments=arguments or {}))


async def _drain():
    while calllog._PENDING:
        await asyncio.gather(*list(calllog._PENDING), return_exceptions=True)


def _logger(rows, identity=None):
    async def sink(row):
        rows.append(row)
    return calllog.ToolCallLogger(sink, server="oto",
                                  identity=identity or (lambda: {"sub": "user-1"}))


def test_classify_connu_donne_bien_not_authorized():
    """Prérequis du reste du fichier : si `error_taxonomy` change de verdict sur un
    401 amont, ce test-ci rougit en premier et dit pourquoi — pas les tests calllog."""
    from oto_mcp import error_taxonomy
    info = error_taxonomy.classify(_RefusAmont401("accès refusé"))
    assert info.code == "not_authorized"


@pytest.mark.asyncio
async def test_un_refus_amont_pose_error_kind_sur_la_ligne():
    """Le cas visé : un connecteur qui répond 401/403 laisse une trace LISIBLE,
    plus seulement un texte à interpréter."""
    rows: list = []
    mw = _logger(rows)

    async def call_next(_ctx):
        raise _RefusAmont401("accès refusé par le service amont")

    with pytest.raises(_RefusAmont401):
        await mw.on_call_tool(_context(), call_next)
    await _drain()

    assert len(rows) == 1
    assert rows[0]["ok"] is False
    assert rows[0]["error_kind"] == "not_authorized"


@pytest.mark.asyncio
async def test_un_succes_ne_rapporte_aucun_fait_derreur():
    """Rien à classer sur un succès : `error_kind` doit rester NULL, pas une valeur
    inventée du genre `ok` ou `none`."""
    rows: list = []
    mw = _logger(rows)

    async def call_next(_ctx):
        return "ok"

    await mw.on_call_tool(_context(), call_next)
    await _drain()

    assert rows[0]["ok"] is True
    assert rows[0].get("error_kind") is None


@pytest.mark.asyncio
async def test_une_erreur_interne_non_amont_reste_classee_sans_casser_le_journal():
    """Une exception de code ordinaire (`ValueError`) doit quand même produire UN
    `error_kind` (`internal`, le repli de `classify`) — jamais faire échouer la
    journalisation elle-même : le journal reste best-effort."""
    rows: list = []
    mw = _logger(rows)

    async def call_next(_ctx):
        raise ValueError("bug")

    with pytest.raises(ValueError):
        await mw.on_call_tool(_context(), call_next)
    await _drain()

    assert rows[0]["error_kind"] == "internal"


def test_l_insert_transporte_error_kind(monkeypatch):
    """Garde-fou de bout en bout, même recette que le discriminant #117
    (`test_calllog_call_discriminant.py`) : le champ collecté doit atteindre le SQL,
    avec autant de placeholders que de valeurs."""
    from oto_mcp.db import usage

    captured: dict = {}

    class _Conn:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(usage, "_connect", lambda: _Conn())
    usage.insert_tool_call({
        "tool": "fr_get", "ok": False, "sub": "user-1",
        "error": "accès refusé", "error_kind": "not_authorized",
    })

    assert "error_kind" in captured["sql"], "error_kind absent de l'INSERT"
    assert "not_authorized" in captured["params"]
    assert captured["sql"].count("%s") == len(captured["params"])


def test_get_tool_call_projette_error_kind(monkeypatch):
    """`oto_admin_monitoring op=call` lit `db.get_tool_call` : sans la colonne dans
    la projection explicite, la fiche d'un appel resterait muette sur le motif
    structuré même une fois la colonne posée en base."""
    from oto_mcp.db import usage

    captured: dict = {}

    class _Row(dict):
        pass

    class _Conn:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            return self

        def fetchone(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(usage, "_connect", lambda: _Conn())
    usage.get_tool_call(1)

    assert "error_kind" in captured["sql"], (
        "get_tool_call ne projette pas error_kind — la fiche d'un appel "
        "(oto_admin_monitoring op=call) ne le rendrait jamais")
