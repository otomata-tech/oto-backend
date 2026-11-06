"""A described connector for the reader's tests (`tests/test_decrits.py`): a served description, as
`oto.connectors.descriptions.load` returns it, and a client that follows it, without network.

Invented values only (`example.test`), no vendor.
"""
from __future__ import annotations

import copy
from typing import List, Optional

from oto.tools.common.errors import UpstreamHTTPError

_STRICT = {"type": "object", "additionalProperties": False}


def _input(properties: dict, required=()) -> dict:
    return {**_STRICT, "required": list(required), "properties": properties}


def _f(name, klass, properties=None, required=(), **extra) -> dict:
    f = {"name": name, "class": klass, "description": f"{name.replace('_', ' ').capitalize()}. More detail here.",
         "input": _input(properties or {}, required), "examples": [{"title": name, "input": extra.pop("example", {})}]}
    if klass == "sensitive":
        f["confirm"] = {"summary": f"{name}?"}
    f.update(extra)
    return f


DESCRIPTION = {
    "connector": {
        "name": "essai", "label": "Essai", "namespace": "essai", "version": "1.0.0", "api_version": "v1",
        "base_url": "https://api.example.test", "auth": {"kind": "api_key", "in": "header", "name": "X-Key", "key": "api_key"},
        "credential": [{"name": "api_key", "label": "Essai API key", "secret": True}],
        "modes": ["byo_user", "byo_org"],
        "probe": {"function": "get_account", "non_empty": ["id"], "balance": {"path": "credits", "min": 1, "unit": "credits"}},
        "errors": [
            {"status": 401, "code": "key_refused", "message": "The key was refused.", "retryable": False,
             "marks_account": "credential_rejected"},
            {"status": 402, "code": "credits_exhausted", "message": "No credits left.", "retryable": False,
             "marks_account": "quota_exhausted"},
            {"status": 404, "code": "not_found", "message": "Not found.", "retryable": False},
            {"status": 422, "body": {"path": "error", "contains": ["locked"]}, "code": "locked",
             "message": "The record is locked.", "retryable": False},
            {"status": 429, "code": "rate_limited", "message": "Too fast.", "retryable": True},
        ],
    },
    "functions": [
        _f("get_account", "read"),
        _f("list_things", "read", {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "cursor": {"type": "string"},
            "all_pages": {"type": "boolean"}, "max_pages": {"type": "integer", "minimum": 1, "maximum": 3},
        }, ["limit"], example={"limit": 10},
           pagination={"kind": "cursor", "request_param": "cursor", "next": "next", "max_pages": 3},
           output={"items": "data", "strip": ["secret", "owner.photo"], "projection": ["id", "owner"]}),
        _f("get_thing", "read", {"thing_id": {"type": "integer", "minimum": 1}}, ["thing_id"], example={"thing_id": 1},
           refusals=[{"code": "thing_not_found", "when": 404, "message": "No thing with this id."}],
           output={"strip": ["owner.photo"]}),
        _f("save_draft", "write", {"text": {"type": "string"}}, ["text"], act=False, example={"text": "x"}),
        _f("update_thing", "write", {"thing_id": {"type": "integer"}, "name": {"type": "string", "personal": True}},
           ["thing_id"], example={"thing_id": 1}),
        _f("add_people", "sensitive", {
            "list_id": {"type": "integer"},
            "people": {"type": "array", "minItems": 1, "maxItems": 20, "items": {
                **_STRICT, "required": ["email"], "properties": {"email": {"type": "string", "personal": True}}}},
        }, ["list_id", "people"], effect="send", example={"list_id": 1, "people": [{"email": "a@example.test"}]}),
        _f("tag_things", "write", {"ids": {"type": "array", "items": {"type": "integer"}},
                                   "tags": {"type": "array", "items": {"type": "string"}}}, ["ids", "tags"],
           example={"ids": [1], "tags": ["t"]}),
        _f("delete_thing", "sensitive", {"thing_id": {"type": "integer"}}, ["thing_id"], effect="destroy",
           example={"thing_id": 1}),
    ],
}


def description() -> dict:
    return copy.deepcopy(DESCRIPTION)


class ClientDEssai:
    """Follows `DESCRIPTION`: records each call, answers from `ANSWERS`, raises what `RAISES` names."""

    CALLS: list = []
    ANSWERS: dict = {}
    RAISES: dict = {}

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key

    def _call(self, _function: str, **args):
        type(self).CALLS.append((self.api_key, _function, args))
        if _function in self.RAISES:
            raise self.RAISES[_function]
        answer = self.ANSWERS.get(_function, {"ok": True})
        return answer(args) if callable(answer) else copy.deepcopy(answer)

    def get_account(self):
        return self._call("get_account")

    def list_things(self, limit: int, cursor: Optional[str] = None):
        return self._call("list_things", limit=limit, cursor=cursor)

    def get_thing(self, thing_id: int):
        return self._call("get_thing", thing_id=thing_id)

    def save_draft(self, text: str):
        return self._call("save_draft", text=text)

    def update_thing(self, thing_id: int, name: Optional[str] = None):
        if name == "bad":
            raise ValueError(f"`name` {name!r} is refused.")
        return self._call("update_thing", thing_id=thing_id, name=name)

    def add_people(self, list_id: int, people: List[dict]):
        return self._call("add_people", list_id=list_id, people=people)

    def tag_things(self, ids: List[int], tags: List[str]):
        return self._call("tag_things", ids=ids, tags=tags)

    def delete_thing(self, thing_id: int):
        return self._call("delete_thing", thing_id=thing_id)


def erreur(status: int, body=None) -> UpstreamHTTPError:
    return UpstreamHTTPError(status, body, service="essai")
