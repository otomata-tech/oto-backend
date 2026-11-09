"""A described connector for the reader's tests (`tests/test_decrits.py`): a served description, as
`oto.connectors.descriptions.load` returns it, each function with the `call` the engine runs, and a fake vendor that
answers the engine's requests without network.

Invented values only (`example.test`), no vendor.
"""
from __future__ import annotations

import copy
import io
import json
from typing import Any, Callable, Union
from urllib.parse import parse_qsl, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3 import HTTPResponse

_STRICT = {"type": "object", "additionalProperties": False}
BASE_URL = "https://api.example.test"


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
        "base_url": BASE_URL, "auth": {"kind": "api_key", "in": "header", "name": "X-Key", "key": "api_key"},
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
        _f("get_account", "read", call={"method": "GET", "path": "/account"}),
        _f("list_things", "read", {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "cursor": {"type": "string"},
            "all_pages": {"type": "boolean"}, "max_pages": {"type": "integer", "minimum": 1, "maximum": 3},
        }, ["limit"], example={"limit": 10},
           pagination={"kind": "cursor", "request_param": "cursor", "next": "next", "max_pages": 3},
           output={"items": "data", "strip": ["secret", "owner.photo"], "projection": ["id", "owner"]},
           call={"method": "GET", "path": "/things", "query": {"limit": "limit", "cursor": "cursor"}}),
        _f("get_thing", "read", {"thing_id": {"type": "integer", "minimum": 1}}, ["thing_id"], example={"thing_id": 1},
           refusals=[{"code": "thing_not_found", "when": 404, "message": "No thing with this id."}],
           output={"strip": ["owner.photo"]}, call={"method": "GET", "path": "/things/{thing_id}"}),
        _f("save_draft", "write", {"text": {"type": "string"}}, ["text"], act=False, example={"text": "x"},
           call={"method": "POST", "path": "/drafts", "body": {"text": "text"}}),
        _f("update_thing", "write", {"thing_id": {"type": "integer"}, "name": {"type": "string", "personal": True}},
           ["thing_id"], example={"thing_id": 1},
           call={"method": "PATCH", "path": "/things/{thing_id}", "body": {"name": "name"}}),
        _f("add_people", "sensitive", {
            "list_id": {"type": "integer"},
            "people": {"type": "array", "minItems": 1, "maxItems": 20, "items": {
                **_STRICT, "required": ["email"], "properties": {"email": {"type": "string", "personal": True}}}},
        }, ["list_id", "people"], effect="send", example={"list_id": 1, "people": [{"email": "a@example.test"}]},
           call={"method": "POST", "path": "/lists/{list_id}/people", "body": {"people": "people"}}),
        _f("tag_things", "write", {"ids": {"type": "array", "items": {"type": "integer"}},
                                   "tags": {"type": "array", "items": {"type": "string"}}}, ["ids", "tags"],
           example={"ids": [1], "tags": ["t"]}, call={"method": "POST", "path": "/tags", "body": {"ids": "ids", "tags": "tags"}}),
        _f("delete_thing", "sensitive", {"thing_id": {"type": "integer"}}, ["thing_id"], effect="destroy",
           example={"thing_id": 1}, call={"method": "DELETE", "path": "/things/{thing_id}"}),
    ],
}


def description() -> dict:
    return copy.deepcopy(DESCRIPTION)


# --- the fake vendor ----------------------------------------------------------------------------------------------

Answer = Union[tuple, Callable[[requests.PreparedRequest], tuple]]


class Vendeur(HTTPAdapter):
    """Answers the engine's requests in order: `(status, JSON body)`, or a function of the request that returns one;
    notes each request. One request too many fails."""

    def __init__(self, *answers: Answer):
        super().__init__()
        self.answers = list(answers)
        self.requests: list[requests.PreparedRequest] = []

    def send(self, request, **kwargs):
        self.requests.append(request)
        if not self.answers:
            raise AssertionError(f"one request too many: {request.method} {request.url}")
        spec = self.answers.pop(0)
        status, body = spec(request) if callable(spec) else spec
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        raw = HTTPResponse(body=io.BytesIO(data), headers={"content-type": "application/json"}, status=status,
                           preload_content=False, decode_content=False)
        return self.build_response(request, raw)

    def session(self) -> requests.Session:
        """A session of the engine's kind, mounted on this vendor."""
        built = requests.Session()
        built.trust_env = False
        built.mount("https://", self)
        return built


def envoyee(request: requests.PreparedRequest) -> dict:
    """A request the engine sent, in parts: `method`, `path`, `query` (decoded pairs), `headers` (lower case) and
    `body` (JSON read, the text of a form, or `None`)."""
    parts = urlsplit(request.url)
    body: Any = request.body
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    if body is not None and request.headers.get("content-type") == "application/json":
        body = json.loads(body)
    return {"method": request.method, "url": f"{parts.scheme}://{parts.netloc}{parts.path}", "path": parts.path,
            "query": parse_qsl(parts.query, keep_blank_values=True),
            "headers": {k.lower(): v for k, v in request.headers.items()}, "body": body}
