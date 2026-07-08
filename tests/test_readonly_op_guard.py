"""Garde LECTURE SEULE op-aware (consultation d'org / view-as). Le dashboard LIT en
POST `{op:…}` : la garde ne peut pas se baser sur la méthode HTTP, elle lit l'`op` du
corps (`_peek_op`) et n'autorise que les ops de lecture (`_READ_OPS`), en rejouant le
corps intact au handler aval.
"""
import json

import pytest

from oto_mcp.api_routes import _peek_op, _READ_OPS


def _single_body(body: bytes):
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}
    return receive


@pytest.mark.asyncio
async def test_extracts_read_op_and_replays_body():
    body = json.dumps({"op": "list", "project_id": 8}).encode()
    op, replay = await _peek_op(_single_body(body))
    assert op == "list" and op in _READ_OPS
    assert (await replay())["body"] == body  # corps rejoué INTACT au handler


@pytest.mark.asyncio
async def test_write_op_not_in_read_set():
    op, _ = await _peek_op(_single_body(b'{"op":"create","name":"x"}'))
    assert op == "create" and op not in _READ_OPS  # → rejeté par le middleware


@pytest.mark.asyncio
async def test_no_op_is_treated_as_write():
    # POST sans `op` (ex. setLocale {locale}, upload) → op=None ∉ _READ_OPS → rejeté.
    op, _ = await _peek_op(_single_body(b'{"locale":"fr"}'))
    assert op is None and op not in _READ_OPS


@pytest.mark.asyncio
async def test_malformed_body_is_write():
    op, _ = await _peek_op(_single_body(b'not json'))
    assert op is None and op not in _READ_OPS


@pytest.mark.asyncio
async def test_multi_chunk_body_reassembled():
    chunks = [
        {"type": "http.request", "body": b'{"op":', "more_body": True},
        {"type": "http.request", "body": b'"get"}', "more_body": False},
    ]
    it = iter(chunks)

    async def receive():
        return next(it)
    op, replay = await _peek_op(receive)
    assert op == "get"
    # rejeu : les deux chunks reviennent dans l'ordre
    assert (await replay())["body"] == b'{"op":'
    assert (await replay())["body"] == b'"get"}'


def test_read_ops_cover_dashboard_reads():
    for o in ("list", "get", "search", "revisions", "inventory", "list_templates"):
        assert o in _READ_OPS
