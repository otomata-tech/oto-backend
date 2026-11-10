"""Jev connector (TypeSafe via OpenRouter).

Locks: registry entry (no personal key, no free tier), the shared-key-only rule (tenant
or instance platform key; closer keys refused, naming who removes them), billing in real micro-dollars, the two
MCP tools, the verify probe, and batch behaviour (order, per-item errors, stop on key or
balance problems without losing billing, send window, state size cap).
"""
import asyncio
import json
import time
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.mcp_errors import McpError
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import jev

EXPECTED_TOOLS = {"jev_ask", "jev_items", "jev_rows"}

NOUL = {"type": "noul", "instructions": "Does the condition hold?",
        "criteria": {"true": "yes", "false": "no"}}


def _answer(cost=2.0e-05, tokens=341):
    return {"model": "typesafe/jev-1.13-20260917",
            "answers": {"q": {"type": "noul", "noul": 0.9}},
            "usage": {"input_tokens": tokens, "output_tokens": 21, "cost": cost}}


class _Rung:
    """What the cascade returns: the winning key and its rung."""

    def __init__(self, mode="tenant", key="sk-or-test"):
        self.mode = mode
        self.key = key
        self.is_platform = mode == "platform"


@pytest.fixture()
def monte(monkeypatch):
    """Mount the module with the client mocked INSIDE the patch (else `register()`
    captures the real class), the cascade on the tenant rung, and billing captured."""
    from fastmcp import FastMCP

    releve = {}

    def resoudre(provider, want="auto", **kw):
        releve["units"] = kw.get("units")
        return _Rung()

    monkeypatch.setattr("oto_mcp.access.resolve_credential", resoudre)
    monkeypatch.setattr("oto_mcp.session_org.note_call_trace",
                        lambda **kw: releve.update(kw))
    with patch("oto.tools.jev.client.JevClient") as cls:
        cls.return_value.decide.return_value = _answer()
        m = FastMCP("t")
        jev.register(m)
        tools = {t.name: t for t in asyncio.run(m._list_tools())}
        yield m, cls.return_value, tools, releve


def _fn(m, name):
    return asyncio.run(m.get_tool(name)).fn


# --- registry ---------------------------------------------------------------

def test_registry_no_personal_key_no_free_tier():
    c = providers.REGISTRY["jev"]
    assert c.kind == "tools" and c.keyed and c.secret_kind == "api_key"
    # No `byo_user` (not a personal key). `platform`: the primary tenant of an instance
    # has no tenant key, its shared key is a platform instance.
    assert c.auth_modes == frozenset({"byo_org", "platform"})
    # No free tier: the platform key serves only the orgs it is granted to.
    assert c.platform_key_open is False
    assert c.cardinality == "mono"
    assert "jev" in providers.KEY_PROVIDERS
    assert c.category == "AI & automation" and c.publisher_name == "TypeSafe"


def test_org_shareable_because_it_is_the_tenant_rung_door():
    """⚠️ `byo_org` is required by `require_credential("tenant", …)`, not so orgs set keys."""
    assert providers.REGISTRY["jev"].org_shareable is True


def test_doc_how_to_present():
    kinds = {s.kind for s in providers.REGISTRY["jev"].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


# --- surface MCP ------------------------------------------------------------

def test_both_tools_mounted_with_description(monte):
    _, _, tools, _ = monte
    assert EXPECTED_TOOLS <= set(tools)
    for name in EXPECTED_TOOLS:
        assert tools[name].description, f"{name} has no description"
        assert namespace_of(name) == "jev"


def test_verify_probe_registered(monte):
    assert connector_verify.supports("jev")


def test_probe_fails_for_a_key_that_would_be_refused(monte):
    """A key that is not shared fails the probe WITHOUT a call (a green card would lie)."""
    with patch("oto.tools.jev.client.JevClient") as cls:
        with pytest.raises(ValueError, match="TENANT"):
            jev._verify({"key": "sk-or-x"}, {}, instance=("org", "7", ""))
        assert cls.call_count == 0
        jev._verify({"key": "sk-or-x"}, {}, instance=("tenant", "pilote", ""))
        jev._verify({"key": "sk-or-x"}, {}, instance=("platform", "Main", ""))
        jev._verify({"key": "sk-or-x"}, {})  # before saving: key only
        assert cls.return_value.decide.call_count == 3


# --- the rule: shared key only (tenant or instance platform key) ------------

@pytest.mark.parametrize("mode, who", [("org", "an org admin"),
                                       ("user", "its owner"),
                                       ("group", "a team admin")])
def test_a_key_shadowing_the_tenant_key_is_refused_naming_who_removes_it(
        monte, monkeypatch, mode, who):
    m, client, _, _ = monte
    monkeypatch.setattr("oto_mcp.access.resolve_credential",
                        lambda provider, want="auto", **kw: _Rung(mode=mode))
    with pytest.raises(McpError) as e:
        _fn(m, "jev_ask")({"a": "b"}, {"q": NOUL})
    msg = str(e.value)
    assert mode in msg and "shadows" in msg and "TENANT" in msg and who in msg
    assert client.decide.call_count == 0


def test_tenant_rung_passes(monte):
    m, client, _, _ = monte
    r = _fn(m, "jev_ask")({"a": "b"}, {"q": NOUL})
    assert r["answers"]["q"]["noul"] == 0.9
    assert r["model"] == "typesafe/jev-1.13-20260917"


def test_platform_rung_passes_without_a_tenant_key(monte, monkeypatch):
    """The primary tenant of an instance never carries a tenant key: its shared Jev key
    is a PLATFORM instance, and the cascade lands there. Served, on that key."""
    m, client, _, _ = monte
    monkeypatch.setattr("oto_mcp.access.resolve_credential",
                        lambda provider, want="auto", **kw: _Rung(mode="platform",
                                                                  key="sk-or-instance"))
    r = _fn(m, "jev_ask")({"a": "b"}, {"q": NOUL})
    assert r["answers"]["q"]["noul"] == 0.9
    assert client.decide.call_count == 1


def test_no_key_refusal_names_the_only_path(monte, monkeypatch):
    """The generic "set your own key" refusal is replaced by the tenant-key path."""
    from mcp.types import ErrorData
    from oto_mcp.access.resolve import CredentialUnavailable
    m, _, _, _ = monte

    def none_(provider, want="auto", **kw):
        raise CredentialUnavailable(ErrorData(code=-32602, message="Set your own key"))

    monkeypatch.setattr("oto_mcp.access.resolve_credential", none_)
    with pytest.raises(CredentialUnavailable) as e:
        _fn(m, "jev_ask")({"a": "b"}, {"q": NOUL})
    msg = str(e.value)
    assert "TENANT" in msg and "Set your own key" not in msg


# --- billing ----------------------------------------------------------------

def test_billing_is_real_cost_in_microdollars(monte):
    m, client, _, releve = monte
    client.decide.return_value = _answer(cost=2.03e-05)
    _fn(m, "jev_ask")({"a": "b"}, {"q": NOUL})
    # 20.3 µ$ → 21: rounded UP, a paid call never bills 0.
    assert releve["quantity"] == 21


def test_batch_bills_the_sum_not_one_call(monte):
    m, client, _, releve = monte
    client.decide.return_value = _answer(cost=1.0e-05)
    _fn(m, "jev_items")([{"a": 1}, {"a": 2}, {"a": 3}], {"q": NOUL})
    # ⚠️ Regression: float sum 3.0000000000000004e-05 made bare `ceil` bill 31 µ$.
    assert releve["quantity"] == 30  # 3 × 10 µ$, not one call, not 31


def test_batch_checks_quota_for_all_items(monte):
    m, _, _, releve = monte
    _fn(m, "jev_items")([{"n": i} for i in range(7)], {"q": NOUL})
    assert releve["units"] == 7


def test_undeclared_cost_is_not_billed(monte):
    m, client, _, releve = monte
    client.decide.return_value = {"model": "x", "answers": {}, "usage": {}}
    _fn(m, "jev_ask")({"a": "b"}, {"q": NOUL})
    assert "quantity" not in releve


# --- batch behaviour ------------------------------------------------------

def test_batch_returns_answers_in_sent_order_with_caller_key(monte):
    m, client, _, _ = monte
    r = _fn(m, "jev_items")([{"key": "a", "state": {"n": 1}},
                             {"key": "b", "state": {"n": 2}}], {"q": NOUL})
    assert [x["index"] for x in r["answers"]] == [0, 1]
    assert [x["key"] for x in r["answers"]] == ["a", "b"]
    assert r["usage"]["decided"] == 2 and r["failed"] == 0


def test_a_refused_state_is_an_item_error_not_a_batch_error(monte):
    from oto.tools.common.errors import UpstreamHTTPError
    m, client, _, _ = monte
    calls = {"n": 0}

    def decide(state, questions, model=None, **kw):
        calls["n"] += 1
        if state.get("n") == 2:
            raise UpstreamHTTPError(400, {"detail": {"error_type": "max_tokens_exceeded"}},
                                    service="jev")
        return _answer()

    client.decide.side_effect = decide
    r = _fn(m, "jev_items")([{"n": 1}, {"n": 2}, {"n": 3}], {"q": NOUL})
    assert r["failed"] == 1 and r["usage"]["decided"] == 2
    failed = [x for x in r["answers"] if "error" in x][0]
    assert failed["index"] == 1 and "max_tokens_exceeded" in failed["error"]


@pytest.mark.parametrize("status", [401, 402, 403])
def test_key_or_balance_problem_stops_the_batch(monte, status):
    from oto.tools.common.errors import UpstreamHTTPError
    m, client, _, _ = monte
    client.decide.side_effect = UpstreamHTTPError(status, {"message": "nope"}, service="jev")
    with pytest.raises(McpError) as e:
        _fn(m, "jev_items")([{"n": 1}, {"n": 2}], {"q": NOUL})
    # One message, not N copies.
    assert "key" in str(e.value) or "credits" in str(e.value)


def test_a_402_mid_batch_bills_what_was_already_paid(monte):
    """⚠️ Regression: the refusal escaped via `ex.map` and billing was never recorded."""
    from oto.tools.common.errors import UpstreamHTTPError
    m, client, _, releve = monte

    def decide(state, questions, model=None, **kw):
        if state["n"] >= 3:
            raise UpstreamHTTPError(402, {"message": "no credits"}, service="jev")
        return _answer(cost=1.0e-05)

    client.decide.side_effect = decide
    with pytest.raises(McpError) as e:
        _fn(m, "jev_items")([{"n": i} for i in range(6)], {"q": NOUL}, parallel=1)
    assert releve["quantity"] == 30  # 3 paid answers, billed
    assert "3 answer(s) already given and billed" in str(e.value)


def test_unexpected_thread_exception_bills_then_raises(monte):
    m, client, _, releve = monte

    def decide(state, questions, model=None, **kw):
        if state["n"] == 1:
            raise KeyError("unreadable body")
        return _answer(cost=1.0e-05)

    client.decide.side_effect = decide
    with pytest.raises(KeyError):
        _fn(m, "jev_items")([{"n": 0}, {"n": 1}], {"q": NOUL}, parallel=1)
    assert releve["quantity"] == 10


def test_after_the_window_unsent_states_go_to_retry(monte, monkeypatch):
    m, client, _, _ = monte
    monkeypatch.setattr(jev, "LOT_FENETRE_S", 0.2)

    def decide(state, questions, model=None, **kw):
        time.sleep(0.15)
        return _answer()

    client.decide.side_effect = decide
    r = _fn(m, "jev_items")([{"n": i} for i in range(5)], {"q": NOUL}, parallel=1)
    assert r["usage"]["decided"] == 2
    assert r["retry"] == [2, 3, 4]
    assert r["failed"] == 3 and all("send it again" in x["error"]
                                    for x in r["answers"] if "error" in x)


def test_window_fits_under_rest_cap():
    # The worst last-moment call (10 s connect + read) ends before the 45 s REST cap.
    assert jev.LOT_FENETRE_S + 10 + jev.LECTURE_S < jev.REST_CALL_LIMIT_S


def test_complete_batch_has_nothing_to_retry(monte):
    m, _, _, _ = monte
    r = _fn(m, "jev_items")([{"n": 1}, {"n": 2}], {"q": NOUL})
    assert r["retry"] == []


def test_oversized_state_refused_before_any_call(monte):
    m, client, _, _ = monte
    big = {"doc": "x" * (jev.MAX_ETAT_OCTETS + 1)}
    with pytest.raises(McpError, match="bytes"):
        _fn(m, "jev_ask")(big, {"q": NOUL})
    with pytest.raises(McpError, match=r"items\[1\]"):
        _fn(m, "jev_items")([{"n": 1}, big], {"q": NOUL})
    assert client.decide.call_count == 0


def test_malformed_parallel_is_a_named_error(monte):
    m, _, _, _ = monte
    with pytest.raises(McpError, match="parallel"):
        _fn(m, "jev_items")([{"n": 1}], {"q": NOUL}, parallel="lots")


def test_batch_is_bounded(monte):
    m, _, _, _ = monte
    with pytest.raises(McpError, match="max is"):
        _fn(m, "jev_items")([{"n": i} for i in range(jev.MAX_ITEMS + 1)], {"q": NOUL})
    with pytest.raises(McpError, match="at least one"):
        _fn(m, "jev_items")([], {"q": NOUL})


def test_rubric_checked_once_per_batch(monte):
    m, client, _, _ = monte
    client.check_questions.side_effect = ValueError("question 'q': `criteria` required")
    with pytest.raises(McpError, match="criteria"):
        _fn(m, "jev_items")([{"n": 1}, {"n": 2}], {"q": {"type": "noul"}})
    assert client.decide.call_count == 0


# --- jev_rows (fake store; the real write path is in tests/datastore/test_jev_rows_db.py) ---

from oto_mcp.datastore.errors import (  # noqa: E402
    RevisionConflict, RowLocked, RowValidationError)
from oto_mcp.datastore.outils import _decode_cursor, _encode_cursor  # noqa: E402

SCHEMA = {"fields": [
    {"key": "company", "type": "text"}, {"key": "description", "type": "text"},
    {"key": "q_fit", "type": "number"}, {"key": "q_fit_p", "type": "number"},
    {"key": "q_seg", "type": "enum", "options": ["A", "B"]},
    {"key": "q_seg_p", "type": "number"}, {"key": "q_ok", "type": "number"},
    {"key": "q_model", "type": "text"}]}
QS = {"fit": {"type": "score", "instructions": "fit?", "criteria": ["no", "yes"]},
      "seg": {"type": "choice", "instructions": "seg?", "criteria": {"A": "a", "B": "b"}}}
OUT = {"fit": "q_fit", "seg": "q_seg"}


def _rows_answer(cost=4.8e-05, conf=0.8):
    return {"model": "typesafe/jev-1.13-20260917",
            "answers": {"fit": {"type": "score", "score": 3.34, "confidence": conf},
                        "seg": {"type": "choice", "choice": "A", "confidence": conf,
                                "probabilities": {"A": 0.9, "B": 0.1}}},
            "usage": {"input_tokens": 1100, "cost": cost}}


class FakeStore:
    """Keyset pages over rows sorted by `_id`; the empty-marker clause is honoured."""

    def __init__(self, n=5, schema=SCHEMA):
        self.schema = schema
        self.rows = {f"r{i:03d}": {"_id": f"r{i:03d}", "company": f"Co {i}",
                                   "description": "d" * 300, "_revision": "1"}
                     for i in range(n)}
        self.writes: list = []
        self.patches: list = []
        self.locked: set = set()
        self.changed: set = set()
        #: rows whose ANSWER the schema refuses (the model_column mark still lands)
        self.refused: set = set()

    def _resolve(self, adresse, write=False):
        return 1

    def get_schema(self, adresse):
        return self.schema

    def patch_schema(self, adresse, *, fields):
        self.patches.append([f["key"] for f in fields])
        self.schema = {**self.schema, "fields": list(self.schema["fields"]) + list(fields)}
        return {"added": [f["key"] for f in fields]}

    def _match(self, r, filters):
        for c in filters or []:
            if c["op"] == "empty" and r.get(c["field"]) not in (None, ""):
                return False
        return True

    def cursor_rows(self, adresse, *, filter=None, filters=None, limit=100,
                    cursor=None, fields=None):
        after = _decode_cursor(cursor) if cursor else ""
        ids = [i for i in sorted(self.rows) if i > after
               and self._match(self.rows[i], filters)]
        page = [dict(self.rows[i]) for i in ids[:limit]]
        nxt = _encode_cursor(page[-1]["_id"]) if len(page) == limit else None
        return {"rows": page, "next_cursor": nxt}

    def count_rows(self, adresse, *, filter=None, filters=None):
        return sum(1 for r in self.rows.values() if self._match(r, filters))

    def update_row(self, adresse, rid, patch, expected_revision=None):
        if rid in self.locked:
            raise RowLocked(rid, claimed_by="w", claimed_until="later")
        if rid in self.changed:
            raise RevisionConflict(rid, expected_revision, "2")
        if rid in self.refused and set(patch) - {"q_model"}:
            raise RowValidationError(["q_fit: refused"], row=rid)
        self.writes.append((rid, dict(patch)))
        self.rows[rid].update(patch)
        return self.rows[rid]


@pytest.fixture()
def rows_env(monte, monkeypatch):
    m, client, tools, releve = monte
    store = FakeStore()
    monkeypatch.setattr("oto_mcp.datastore.core.make_store", lambda sub: store)
    monkeypatch.setattr("oto_mcp.datastore.par_reference.make_store", lambda sub: store)
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-t")
    client.decide.return_value = _rows_answer()
    client.decide.side_effect = None
    return _fn(m, "jev_rows"), client, store, releve


def _call(fn, **kw):
    args = dict(datastore="42", questions=QS, state_fields=["company", {"description": 50}],
                output=OUT, model_column="q_model")
    args.update(kw)
    return fn(**args)


def test_rows_writes_answer_confidence_and_model(rows_env):
    fn, client, store, releve = rows_env
    r = _call(fn)
    assert r["decided"] == 5 and r["done"] is True and r["next_cursor"] is None
    rid, patch = store.writes[0]
    assert patch == {"q_fit": 3.34, "q_fit_p": 0.8, "q_seg": "A", "q_seg_p": 0.8,
                     "q_model": "typesafe/jev-1.13-20260917"}
    sent = client.decide.call_args_list[0].args[0]
    assert sent == {"company": "Co 0", "description": "d" * 50}, "projection + max_chars"
    assert releve["quantity"] == 240, "5 × 48 µ$, one billing line per call"


def test_rows_rerun_is_idempotent_and_free(rows_env):
    fn, client, store, releve = rows_env
    _call(fn)
    calls = client.decide.call_count
    releve.clear()
    r = _call(fn)
    assert r["decided"] == 0 and client.decide.call_count == calls
    assert "quantity" not in releve


def test_rows_limit_pages_with_a_watermark_cursor(rows_env):
    fn, _, store, _ = rows_env
    r = _call(fn, limit=2)
    assert r["decided"] == 2 and r["remaining"] == 3 and r["done"] is False
    assert _decode_cursor(r["next_cursor"]) == "r001"
    r = _call(fn, limit=2, cursor=r["next_cursor"])
    assert [w[0] for w in store.writes] == ["r000", "r001", "r002", "r003"]


def test_rows_transient_error_holds_the_watermark_and_stays_undecided(rows_env):
    from oto.tools.common.errors import UpstreamHTTPError
    fn, client, store, _ = rows_env
    err = UpstreamHTTPError(503, "down") if _takes_two() else UpstreamHTTPError("down")
    err.status_code = 503

    def decide(state, questions, **kw):
        if state["company"] == "Co 1":
            raise err
        return _rows_answer()
    client.decide.side_effect = decide
    r = _call(fn)
    assert r["decided"] == 4 and r["error_count"] == 1 and r["jev_errors"] == 0
    assert _decode_cursor(r["next_cursor"]) == "r000", "never past an unhandled row"
    assert store.rows["r001"].get("q_model") is None, "retried next call"
    assert r["remaining"] == 1


def _takes_two() -> bool:
    import inspect
    from oto.tools.common.errors import UpstreamHTTPError
    return len(inspect.signature(UpstreamHTTPError.__init__).parameters) > 2


def test_rows_upstream_400_and_oversized_state_are_marked_not_replayed(rows_env):
    from oto.tools.common.errors import UpstreamHTTPError
    fn, client, store, _ = rows_env
    store.rows["r002"]["description"] = "x" * 20_000
    err = UpstreamHTTPError(400, "max_tokens_exceeded") if _takes_two() \
        else UpstreamHTTPError("max_tokens_exceeded")
    err.status_code, err.body = 400, "max_tokens_exceeded"

    def decide(state, questions, **kw):
        if state["company"] == "Co 1":
            raise err
        return _rows_answer()
    client.decide.side_effect = decide
    r = _call(fn, state_fields=["company", "description"])
    assert r["jev_errors"] == 2 and r["decided"] == 3 and r["done"] is True
    assert store.rows["r001"]["q_model"].startswith("jev_error: ")
    assert "bytes" in store.rows["r002"]["q_model"]
    n = client.decide.call_count
    _call(fn, state_fields=["company", "description"])
    assert client.decide.call_count == n, "marked rows are not replayed"


def test_rows_leased_and_changed_rows_are_never_overwritten(rows_env, monkeypatch):
    fn, client, store, _ = rows_env
    store.rows["r000"].update({"_claimed_by": "w9", "_claimed_until": "x",
                               "_claimed_run": "other"})
    store.locked.add("r001")          # lease taken after the read
    store.changed.add("r002")
    r = _call(fn)
    assert r["skipped_leased"] == 2 and r["skipped_changed"] == 1 and r["decided"] == 2
    assert {w[0] for w in store.writes} == {"r003", "r004"}
    sent = [c.args[0]["company"] for c in client.decide.call_args_list]
    assert "Co 0" not in sent, "a leased row is not paid for"


def test_rows_dry_run_writes_nothing_and_is_billed(rows_env):
    fn, client, store, releve = rows_env
    r = _call(fn, dry_run=True, limit=100)
    assert store.writes == [] and r["dry_run"] is True
    assert len(r["rows"]) == 5 and r["rows"][0]["answers"]["seg"]["choice"] == "A"
    assert releve["quantity"] == 240


def test_rows_overwrite_rejudges_decided_rows(rows_env):
    fn, client, store, _ = rows_env
    _call(fn)
    n = len(store.writes)
    r = _call(fn, overwrite=True)
    assert r["decided"] == 5 and len(store.writes) == n + 5 and r["remaining"] is None


@pytest.mark.parametrize("kw, needle", [
    (dict(questions={"fit": {"type": "score", "instructions": "x"}}, output={"fit": "q_fit"}),
     "`criteria` is required"),
    (dict(output={"fit": "company", "seg": "q_seg"}), "`company` is 'text'"),
    (dict(output={"fit": "q_fit"}), "missing ['seg']"),
    (dict(output={"fit": "q_seg", "seg": "q_fit"}), "needs"),
    (dict(model_column="q_fit"), "model_column"),
    (dict(state_fields=["company", "ghost"]), "ghost"),
])
def test_rows_bad_calls_are_refused_before_any_upstream_call(rows_env, kw, needle):
    fn, client, store, releve = rows_env
    with pytest.raises(McpError) as e:
        _call(fn, **kw)
    assert needle in str(e.value)
    assert client.decide.call_count == 0 and "quantity" not in releve


def test_rows_existing_enum_missing_an_option_is_refused_never_widened(rows_env):
    fn, client, store, _ = rows_env
    store.schema = {"fields": [dict(f, options=["A"]) if f["key"] == "q_seg" else f
                               for f in SCHEMA["fields"]]}
    with pytest.raises(McpError) as e:
        _call(fn)
    assert "lacks option(s) B" in str(e.value)
    assert store.patches == [] and client.decide.call_count == 0


BARE = {"fields": [{"key": "company", "type": "text"}, {"key": "description", "type": "text"}]}


def test_rows_creates_missing_output_columns_typed_from_the_questions(rows_env):
    fn, client, store, _ = rows_env
    store.schema = dict(BARE)
    r = _call(fn, output={"fit": "q_fit", "seg": "q_seg"})
    assert r["created_columns"] == ["q_fit", "q_fit_p", "q_seg", "q_seg_p", "q_model"]
    types = {f["key"]: (f["type"], f.get("options")) for f in store.schema["fields"]}
    assert types["q_fit"] == ("number", None) and types["q_fit_p"] == ("number", None)
    assert types["q_seg"] == ("enum", ["A", "B"]) and types["q_model"] == ("text", None)
    assert r["decided"] == 5 and store.writes[0][1]["q_seg"] == "A"


def test_rows_column_creation_is_idempotent(rows_env):
    fn, _, store, _ = rows_env
    store.schema = dict(BARE)
    _call(fn)
    r = _call(fn)
    assert r["created_columns"] == [] and len(store.patches) == 1


def test_rows_dry_run_creates_nothing_and_says_what_it_would(rows_env):
    fn, _, store, _ = rows_env
    store.schema = dict(BARE)
    r = _call(fn, dry_run=True)
    assert store.patches == [] and store.writes == []
    assert r["would_create_columns"] == ["q_fit", "q_fit_p", "q_seg", "q_seg_p", "q_model"]
    assert len(r["rows"]) == 5


def test_rows_noul_creates_no_p_column(rows_env):
    fn, client, store, _ = rows_env
    store.schema = dict(BARE)
    client.decide.return_value = {"model": "m", "answers": {"ok": {"type": "noul", "noul": 0.7}},
                                  "usage": {"cost": 1e-05}}
    r = _call(fn, questions={"ok": NOUL}, output={"ok": "q_ok"})
    assert r["created_columns"] == ["q_ok", "q_model"]


def test_rows_noul_writes_p_and_needs_no_p_column(rows_env):
    fn, client, store, _ = rows_env
    client.decide.return_value = {"model": "m", "answers": {"ok": {"type": "noul", "noul": 0.7}},
                                  "usage": {"cost": 1e-05}}
    r = _call(fn, questions={"ok": NOUL}, output={"ok": "q_ok"})
    assert r["decided"] == 5 and store.writes[0][1] == {"q_ok": 0.7, "q_model": "m"}


def test_rows_quota_checked_per_page(rows_env, monkeypatch):
    fn, _, store, releve = rows_env
    from oto_mcp.tools import jev_rows as jr
    monkeypatch.setattr(jr, "PAGE", 2)
    units = []

    def resoudre(provider, want="auto", **kw):
        units.append(kw.get("units"))
        return _Rung()
    monkeypatch.setattr("oto_mcp.access.resolve_credential", resoudre)
    _call(fn)
    assert units == [1, 2, 2, 1], "the key is checked once before any side effect, then per page"


def test_rows_counts_low_confidence_and_thin_states(rows_env):
    fn, client, store, _ = rows_env
    client.decide.return_value = _rows_answer(conf=0.4)
    for r in store.rows.values():
        r["description"] = ""
    r = _call(fn)
    assert r["low_confidence"] == 10, "two answers per row"
    assert r["thin_state"] == 5
    assert all(w[1]["q_fit"] == 3.34 for w in store.writes), "counts never change a verdict"


def test_rows_key_failure_stops_and_bills_what_was_given(rows_env):
    from oto.tools.common.errors import UpstreamHTTPError
    fn, client, store, releve = rows_env
    err = UpstreamHTTPError(402, "no credits") if _takes_two() else UpstreamHTTPError("x")
    err.status_code, err.body = 402, "no credits"
    seen = []

    def decide(state, questions, **kw):
        seen.append(1)
        if len(seen) > 2:
            raise err
        return _rows_answer()
    client.decide.side_effect = decide
    with pytest.raises(McpError) as e:
        _call(fn, parallel=1)
    assert "402" in str(e.value) and "2 row(s) already written" in str(e.value)
    assert releve["quantity"] == 96 and len(store.writes) == 2


#: Real upstream answers (typesafe/jev-1.13-20260917, a public trade-show exhibitor),
#: legends and zero probabilities trimmed.
REAL = {"model": "typesafe/jev-1.13-20260917",
        "answers": {
            "fit": {"type": "score", "score": 3.34, "confidence": 0.71,
                    "probabilities": {"3": 0.66, "4": 0.34},
                    "legend": {"3": "Strong", "4": "Ideal"}},
            "segment": {"type": "choice", "choice": "Printer OEM", "confidence": 1,
                        "probabilities": {"Printer OEM": 1, "Materials": 0}}},
        "usage": {"input_tokens": 1150, "cost": 4.83e-05}}


def test_rows_real_answers_land_in_the_right_cells(rows_env):
    fn, client, store, releve = rows_env
    store.schema = {"fields": [
        {"key": "company", "type": "text"}, {"key": "description", "type": "text"},
        {"key": "q_fit", "type": "number"}, {"key": "q_fit_p", "type": "number"},
        {"key": "q_segment", "type": "enum", "options": ["Printer OEM", "Materials"]},
        {"key": "q_segment_p", "type": "number"}, {"key": "q_model", "type": "text"}]}
    store.rows = {"r000": {"_id": "r000", "company": "Acme Printers, Inc.",
                           "description": "Micro-precision 3D printers.", "_revision": "1"}}
    client.decide.return_value = REAL
    qs = {"fit": QS["fit"],
          "segment": {"type": "choice", "instructions": "segment?",
                      "criteria": {"Printer OEM": "printers", "Materials": "powders"}}}
    r = _call(fn, questions=qs, output={"fit": "q_fit", "segment": "q_segment"})
    assert r["decided"] == 1
    row = store.rows["r000"]
    assert row["q_fit"] == 3.34 and row["q_fit_p"] == 0.71
    assert row["q_segment"] == "Printer OEM" and row["q_segment_p"] == 1
    assert row["q_model"] == "typesafe/jev-1.13-20260917"
    assert releve["quantity"] == 49


# --- jev_rows: nothing happens before the key and the rubric pass ---------------------------

def _no_side_effect_setup(store):
    store.schema = dict(BARE)                           # columns would be created
    store.rows["r002"]["description"] = "x" * 20_000    # a row would be marked


def _key_refusals():
    from mcp.types import ErrorData, INVALID_PARAMS
    from oto_mcp.access.resolve import CredentialUnavailable

    def aucune(provider, want="auto", **kw):
        raise CredentialUnavailable(ErrorData(code=INVALID_PARAMS, message="no key"))
    return [aucune, lambda provider, want="auto", **kw: _Rung(mode="org")]


@pytest.mark.parametrize("resoudre", _key_refusals(), ids=["no_key", "org_key"])
def test_rows_without_the_tenant_key_touch_nothing(rows_env, monkeypatch, resoudre):
    fn, client, store, releve = rows_env
    _no_side_effect_setup(store)
    monkeypatch.setattr("oto_mcp.access.resolve_credential", resoudre)
    with pytest.raises(McpError):
        _call(fn, state_fields=["company", "description"])
    assert store.patches == [] and store.writes == [], "no column created, no row marked"
    assert client.decide.call_count == 0 and "quantity" not in releve


@pytest.mark.parametrize("check", ["check_questions", "check_model"])
def test_rows_a_refused_rubric_or_model_touches_nothing(rows_env, check):
    fn, client, store, _ = rows_env
    _no_side_effect_setup(store)
    getattr(client, check).side_effect = ValueError("question 'fit': refused")
    with pytest.raises(McpError) as e:
        _call(fn, state_fields=["company", "description"])
    assert "refused" in str(e.value)
    assert store.patches == [] and store.writes == []
    assert client.decide.call_count == 0


def test_rows_a_request_400_stops_the_batch_and_bills_what_was_given(rows_env):
    from oto.tools.common.errors import UpstreamHTTPError
    fn, client, store, releve = rows_env
    err = UpstreamHTTPError(400, "unknown model") if _takes_two() else UpstreamHTTPError("x")
    err.status_code, err.body = 400, "unknown model"
    seen = []

    def decide(state, questions, **kw):
        seen.append(1)
        if len(seen) > 2:
            raise err
        return _rows_answer()
    client.decide.side_effect = decide
    with pytest.raises(McpError) as e:
        _call(fn, parallel=1)
    assert "400" in str(e.value) and "2 row(s) already written" in str(e.value)
    assert releve["quantity"] == 96 and len(store.writes) == 2
    assert not any(str(r.get("q_model") or "").startswith("jev_error")
                   for r in store.rows.values()), "a request error never poisons rows"


def test_rows_the_reply_carries_no_value_read_from_the_table(rows_env):
    fn, _, store, _ = rows_env
    for r in store.rows.values():
        r["company"] = "SECRET-" + r["company"]
    store.refused.add("r003")
    dry = _call(fn, dry_run=True)
    assert "SECRET" not in json.dumps(dry, default=str)
    assert set(dry["rows"][0]) == {"_id", "answers"}
    r = _call(fn)
    assert "SECRET" not in json.dumps(r, default=str)
    assert set(r["sample"][0]) == {"_id", "written"}
    assert r["errors"] == [{"_id": "r003", "code": "writeback_refused"}]


def test_rows_an_answer_the_table_refuses_is_marked_and_never_paid_again(rows_env):
    fn, client, store, _ = rows_env
    store.refused.add("r001")
    r = _call(fn)
    assert r["decided"] == 4 and r["jev_errors"] == 1 and r["done"] is True
    assert store.rows["r001"]["q_model"].startswith("jev_error: ")
    n = client.decide.call_count
    r = _call(fn)
    assert client.decide.call_count == n and r["decided"] == 0


def test_rows_an_empty_state_is_counted_never_marked_and_ends_the_pass(rows_env):
    fn, client, store, _ = rows_env
    store.rows["r001"].update(company="", description="")
    r = _call(fn)
    assert r["empty_state"] == 1 and r["jev_errors"] == 0 and r["decided"] == 4
    assert store.rows["r001"].get("q_model") is None, "no definitive mark"
    assert r["remaining"] == 1 and r["done"] is True and r["next_cursor"] is None
    store.rows["r001"]["company"] = "Co 1"
    r = _call(fn)
    assert r["decided"] == 1 and r["remaining"] == 0
