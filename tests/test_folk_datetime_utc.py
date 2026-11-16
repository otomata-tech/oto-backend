"""Dates-heures écrites chez Folk : UTC avec un `Z` final, rien d'autre.

Le cas vécu (usage signal 1476, neuf occurrences) : un agent ferme une tâche avec
`fields={"completedAt": "2026-10-20T14:06:15+02:00"}` — un ISO 8601 parfaitement
valide, avec décalage, comme le docstring (« <ISO 8601> ») le laissait croire.
Folk répond 422 `Invalid datetime`, sans dire que seule la forme UTC `…Z` passe.

Ce fichier verrouille ce que l'outil fait désormais AVANT l'appel, sur les trois
champs date-heure qu'il écrit (`completedAt` de mark_done, `date_time` d'une
interaction créée, `dateTime` d'une interaction modifiée) :
- une valeur déjà en `…Z` part telle quelle, octet pour octet (rien de ce qui
  marchait ne change) ;
- une valeur avec décalage (`+02:00`, `-05:00`, `+00:00`) est convertie vers le
  MÊME instant en UTC `…Z` — aucune information perdue ;
- une valeur sans fuseau (ou une date seule) est refusée, nommément, avant tout
  appel : deviner un fuseau à la place de l'appelant déplacerait l'instant.
"""
import asyncio
from unittest.mock import patch

import pytest
from oto_mcp.mcp_errors import McpError


def _tool(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import folk as F

    m = FastMCP("t")
    F.register(m)
    return asyncio.run(m.get_tool(name)).fn


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr(
        "oto_mcp.access.resolve_api_key", lambda provider, account=None: ("k", False)
    )


@pytest.fixture
def client():
    with patch("oto.tools.folk.client.FolkClient") as cls:
        yield cls.return_value


# --- la normalisation elle-même ---------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    # le cas du signal : décalage positif
    ("2026-10-20T14:06:15+02:00", "2026-10-20T12:06:15.000Z"),
    # décalage négatif qui fait changer de jour
    ("2026-11-13T23:30:00-05:00", "2026-11-14T04:30:00.000Z"),
    # +00:00 est déjà UTC, mais Folk ne veut que le `Z`
    ("2026-11-13T09:00:00+00:00", "2026-11-13T09:00:00.000Z"),
    # décalage sans deux-points, secondes absentes, espace au lieu de T
    ("2026-11-13T10:00+0100", "2026-11-13T09:00:00.000Z"),
    ("2026-11-13 10:00:00+01:00", "2026-11-13T09:00:00.000Z"),
    # fraction au-delà de la milliseconde : tronquée (l'interaction plafonne à
    # 24 caractères, soit exactement `YYYY-MM-DDTHH:MM:SS.mmmZ`)
    ("2026-11-13T10:00:00.123456+01:00", "2026-11-13T09:00:00.123Z"),
    ("2026-11-13T10:00:00.5+01:00", "2026-11-13T09:00:00.500Z"),
    # `z` minuscule : même instant, forme canonique
    ("2026-11-13T09:00:00z", "2026-11-13T09:00:00.000Z"),
])
def test_offset_is_converted_to_the_same_instant_in_utc(value, expected):
    from oto_mcp.tools.folk import _folk_datetime

    out = _folk_datetime(value, "completedAt")
    assert out == expected
    assert len(out) <= 24


@pytest.mark.parametrize("value", [
    "2026-11-13T09:00:00Z",
    "2026-11-13T09:00:00.000Z",
    "2026-08-26T10:00:00.000Z",
])
def test_a_utc_z_value_passes_through_byte_for_byte(value):
    from oto_mcp.tools.folk import _folk_datetime

    assert _folk_datetime(value, "completedAt") is value


@pytest.mark.parametrize("value,match", [
    ("2026-11-13T09:00:00", "no timezone"),
    ("2026-11-13T09:00", "no timezone"),
    ("2026-11-13", "no timezone"),
    ("13/11/2026 09:00", "ISO 8601"),
    ("yesterday", "ISO 8601"),
    ("2026-13-40T09:00:00+01:00", "ISO 8601"),
    (1763024400, "ISO 8601"),
])
def test_naive_or_malformed_value_is_refused_naming_the_expected_form(value, match):
    from oto_mcp.tools.folk import _folk_datetime

    with pytest.raises(McpError, match=match) as exc:
        _folk_datetime(value, "completedAt")
    msg = str(exc.value)
    assert "completedAt" in msg and "2026-11-13T09:00:00Z" in msg


# --- op="mark_done" : completedAt ---------------------------------------------------

def test_mark_done_converts_an_offset_before_calling_folk(client):
    """Le cas exact du signal."""
    _tool("folk_record")(entity="task", op="mark_done", id="tsk_1",
                         fields={"completedAt": "2026-10-20T14:06:15+02:00"})
    client.mark_task_done.assert_called_once_with(
        "tsk_1", completed_at="2026-10-20T12:06:15.000Z")


def test_mark_done_bulk_converts_once_for_every_task(client):
    client.mark_task_done.side_effect = lambda tid, **kw: {"id": tid}
    out = _tool("folk_record")(entity="task", op="mark_done",
                               ids=["tsk_1", "tsk_2"],
                               fields={"completedAt": "2026-10-20T14:06:15+02:00"})
    assert out["succeeded"] == 2
    for call in client.mark_task_done.call_args_list:
        assert call.kwargs["completed_at"] == "2026-10-20T12:06:15.000Z"


def test_mark_done_naive_is_refused_once_before_any_call_even_in_bulk(client):
    """Refusé AVANT `_bulk_run` : sinon 50 ids rendraient 50 échecs identiques
    par élément au lieu d'un refus qui nomme la forme attendue."""
    with pytest.raises(McpError, match="no timezone"):
        _tool("folk_record")(entity="task", op="mark_done",
                             ids=["tsk_1", "tsk_2"],
                             fields={"completedAt": "2026-10-20T14:06:15"})
    client.mark_task_done.assert_not_called()


def test_mark_done_dry_run_refuses_what_the_real_call_would(client):
    """Un dry_run ne doit pas sembler bon quand l'écriture réelle échouerait."""
    with pytest.raises(McpError, match="no timezone"):
        _tool("folk_record")(entity="task", op="mark_done", id="tsk_1",
                             dry_run=True,
                             fields={"completedAt": "2026-10-20"})
    client.get_task.assert_not_called()
    client.mark_task_done.assert_not_called()


def test_mark_done_without_timestamp_still_defaults_to_now(client):
    _tool("folk_record")(entity="task", op="mark_done", id="tsk_1")
    client.mark_task_done.assert_called_once_with("tsk_1", completed_at=None)


# --- entity="interaction" : date_time (create) / dateTime (update) ------------------

def test_create_interaction_converts_an_offset(client):
    _tool("folk_record")(entity="interaction", op="create",
                         item={"entity_id": "per_1", "type": "call", "title": "t",
                               "date_time": "2026-11-13T10:00:00+01:00"})
    assert client.create_interaction.call_args.kwargs["date_time"] == \
        "2026-11-13T09:00:00.000Z"


def test_create_interaction_z_value_and_omitted_value_are_unchanged(client):
    _tool("folk_record")(entity="interaction", op="create",
                         item={"entity_id": "per_1", "type": "call", "title": "t",
                               "date_time": "2026-11-13T09:00:00.000Z"})
    assert client.create_interaction.call_args.kwargs["date_time"] == \
        "2026-11-13T09:00:00.000Z"
    client.create_interaction.reset_mock()
    _tool("folk_record")(entity="interaction", op="create",
                         item={"entity_id": "per_1", "type": "call", "title": "t"})
    assert "date_time" not in client.create_interaction.call_args.kwargs


def test_create_interaction_bulk_converts_each_item(client):
    client.create_interaction.side_effect = lambda **kw: {"id": "lit_x"}
    _tool("folk_record")(entity="interaction", op="create", items=[
        {"entity_id": "per_1", "type": "call", "title": "a",
         "date_time": "2026-11-13T10:00:00+01:00"},
        {"entity_id": "per_2", "type": "call", "title": "b",
         "date_time": "2026-11-13T09:00:00Z"},
    ])
    sent = sorted(c.kwargs["date_time"]
                  for c in client.create_interaction.call_args_list)
    assert sent == ["2026-11-13T09:00:00.000Z", "2026-11-13T09:00:00Z"]


def test_create_interaction_naive_is_refused_before_the_call(client):
    with pytest.raises(McpError, match="date_time"):
        _tool("folk_record")(entity="interaction", op="create",
                             item={"entity_id": "per_1", "type": "call",
                                   "title": "t",
                                   "date_time": "2026-11-13T10:00:00"})
    client.create_interaction.assert_not_called()


def test_create_interaction_dry_run_previews_the_converted_value(client):
    out = _tool("folk_record")(entity="interaction", op="create", dry_run=True,
                               item={"entity_id": "per_1", "type": "call",
                                     "title": "t",
                                     "date_time": "2026-11-13T10:00:00+01:00"})
    assert out["would_create"]["date_time"] == "2026-11-13T09:00:00.000Z"
    client.create_interaction.assert_not_called()


def test_update_interaction_converts_an_offset_on_datetime(client):
    _tool("folk_record")(entity="interaction", op="update", id="lit_1",
                         entity_id="per_A",
                         fields={"dateTime": "2026-11-13T10:00:00+01:00"})
    client.update_interaction.assert_called_once_with(
        "lit_1", "per_A", dateTime="2026-11-13T09:00:00.000Z")


def test_update_interaction_naive_datetime_is_refused(client):
    with pytest.raises(McpError, match="dateTime"):
        _tool("folk_record")(entity="interaction", op="update", id="lit_1",
                             entity_id="per_A",
                             fields={"dateTime": "2026-11-13T10:00:00"})
    client.update_interaction.assert_not_called()


# --- le texte servi le dit ----------------------------------------------------------

def test_the_description_names_the_utc_z_form():
    from fastmcp import FastMCP
    from oto_mcp.tools import folk as F

    m = FastMCP("t")
    F.register(m)
    desc = asyncio.run(m.get_tool("folk_record")).description
    assert "<ISO 8601>" not in desc
    assert "2026-11-13T09:00:00Z" in desc
