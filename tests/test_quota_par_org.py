"""A platform key whose daily quota counts per ORG (`Connector.grant_quota_per = "org"`).

The shared key of a connector billed at cost to the org (image generation) carries ONE
ceiling for the org the call is made in, every member together — not one per person.
Every other shared key stays counted per person (`usage(sub, tool, day)`).

What this file locks:
- `gemini_image` is the ONLY connector that declares it; an unknown value is refused;
- for such a connector the platform rung names the org whose counter the refusal reads
  (`org_counter`, the call's org whichever scope granted); without an org, not served;
- two members of one org share the ceiling: what one spent refuses the other;
- another shared key is still counted per person;
- the settlement debits the counter of the call's org;
- `/api/me` shows the org's counter;
- on a real database, two concurrent debits lose no unit, and the table goes with its org.
"""
from __future__ import annotations

import os
import threading

import pytest

from _base_jetable import base_jetable
from oto_mcp import access, providers, session_org
from oto_mcp.access import platform_grant, reglement
from oto_mcp.mcp_errors import McpError

_ORG = 42


# ── the declaration ───────────────────────────────────────────────────────────

def test_gemini_image_is_the_only_connector_counted_per_org():
    """Any other shared key stays per person: switching one changes the ceiling of every
    member of every org it serves — a decision per connector, made here."""
    declares = sorted(c.name for c in providers.REGISTRY.values() if c.grant_quota_per)
    assert declares == ["gemini_image"]
    assert providers.REGISTRY["gemini_image"].grant_quota_per == "org"


def test_an_unknown_value_is_refused_at_declaration():
    from oto_mcp.providers._model import _c

    with pytest.raises(ValueError, match="grant_quota_per"):
        _c("x", ["x"], grant_quota_per="team")


# ── the platform rung ─────────────────────────────────────────────────────────

def _instances(scopes: list[str], quota: int) -> list[dict]:
    return [{"label": "shared", "share_mode": "closed", "share_down": list(scopes),
             "share_side": [], "meta": {"rate_limit_by": {s: quota for s in scopes}}}]


@pytest.fixture
def silent_chain(monkeypatch):
    from oto_mcp import grants_chain
    monkeypatch.setattr(grants_chain, "platform_rung", lambda *a, **k: None)


def test_the_rung_names_the_org_counter(silent_chain):
    meta = platform_grant._platform_grant_meta(
        "a", "gemini_image", _ORG, instances=_instances([f"org:{_ORG}"], 5))
    assert meta == {"label": "shared", "daily_quota": 5, "org_counter": _ORG}
    # A person's own grant: the counter is still the org the call is made in.
    meta = platform_grant._platform_grant_meta(
        "a", "gemini_image", _ORG, instances=_instances(["user:a"], 5))
    assert meta["org_counter"] == _ORG


def test_another_shared_key_names_no_org_counter(silent_chain):
    meta = platform_grant._platform_grant_meta(
        "a", "elevenlabs", _ORG, instances=_instances([f"org:{_ORG}"], 5))
    assert meta == {"label": "shared", "daily_quota": 5}


def test_without_an_org_a_key_counted_per_org_is_not_served(silent_chain):
    assert platform_grant._platform_grant_meta(
        "a", "gemini_image", None, instances=_instances(["user:a"], 5)) is None


# ── the real resolution: refusal and settlement ───────────────────────────────

@pytest.fixture
def org_key(monkeypatch, sans_droit_declare):
    """The real `resolve_api_key`, the vault and the counters in memory: one closed
    platform key per provider, granted to the org with a ceiling of 1,000 units."""
    from oto_mcp import grants_chain

    counters = {"usage": {}, "org": {}}
    monkeypatch.setattr(access.db, "get_member_api_key", lambda sub, org, p: None)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access, "current_org", lambda sub: _ORG)
    monkeypatch.setattr(access.credentials_store, "list_platform_instances",
                        lambda p: _instances([f"org:{_ORG}"], 1_000))
    monkeypatch.setattr(access.credentials_store, "get_credential",
                        lambda et, eid, p, account="": "PLAT")
    monkeypatch.setattr(grants_chain.db_grants, "edges_for", lambda ref, grantees: [])
    monkeypatch.setattr("oto_mcp.links.ou_poser_la_cle", lambda *a, **k: "")
    monkeypatch.setattr(access.platform_rate, "enforce", lambda *a, **k: None)
    monkeypatch.setattr(access.credits, "reserver", lambda *a, **k: None)
    # The walk is reduced to its PLATFORM rung, computed by the real
    # `platform_grant._resolve_platform_grant` (the member, team and org rungs read the
    # vault: no key there is the case at hand).
    def platform_only(provider, sub, active_org, **kw):
        payload = platform_grant._resolve_platform_grant(sub, provider, active_org)
        return (access.CascadeRung("platform", "platform", payload["label"], payload)
                if payload else None)

    monkeypatch.setattr(access.chain_shadow, "barreau_gagnant", platform_only)

    def get_usage_today(sub, tool):
        return counters["usage"].get((sub, tool), 0)

    def increment_usage(sub, tool, amount=1):
        counters["usage"][(sub, tool)] = get_usage_today(sub, tool) + amount
        return counters["usage"][(sub, tool)]

    def ajouter(org_id, tool, amount):
        counters["org"][(org_id, tool)] = counters["org"].get((org_id, tool), 0) + amount
        return counters["org"][(org_id, tool)]

    monkeypatch.setattr(access.db, "get_usage_today", get_usage_today)
    monkeypatch.setattr(access.db, "increment_usage", increment_usage)
    monkeypatch.setattr("oto_mcp.db.usage_org.ajouter", ajouter)
    monkeypatch.setattr("oto_mcp.db.usage_org.du_jour",
                        lambda org_id, tool: counters["org"].get((org_id, tool), 0))
    return counters


def _call(sub: str, provider: str, units: int) -> dict:
    """One call by `sub` on `provider`'s key, settled as the call edge settles it."""
    holder: dict = {}
    jeton = session_org.set_call_trace(holder)
    try:
        access.resolve_credential(provider, sub=sub, units=units, emit_on_failure=False)
        session_org.note_call_trace(units=units)
    finally:
        session_org.reset_call_trace(jeton)
    reglement.regler_appel(sub, provider, holder)
    return holder


def test_two_members_of_one_org_share_the_ceiling(org_key):
    _call("a", "gemini_image", 700)
    assert org_key["org"] == {(_ORG, "gemini_image"): 700}
    # The other member has spent nothing himself: the org's 700 refuse his 400.
    with pytest.raises(McpError, match="700"):
        _call("b", "gemini_image", 400)
    _call("b", "gemini_image", 300)
    assert org_key["org"] == {(_ORG, "gemini_image"): 1_000}


def test_the_ceiling_reached_refuses(org_key):
    _call("a", "gemini_image", 1_000)
    with pytest.raises(McpError, match="exceeded today"):
        _call("b", "gemini_image", 1)
    assert org_key["org"] == {(_ORG, "gemini_image"): 1_000}


def test_another_shared_key_is_still_counted_per_person(org_key):
    _call("a", "elevenlabs", 700)
    assert org_key["org"] == {}
    _call("b", "elevenlabs", 700)      # b's own count is 0: served
    with pytest.raises(McpError):
        _call("a", "elevenlabs", 400)  # a's own count is 700


# ── /api/me ───────────────────────────────────────────────────────────────────

def test_api_me_shows_the_org_counter(monkeypatch):
    from oto_mcp import db
    from oto_mcp.access import status as A_status

    monkeypatch.setattr(db, "KEY_PROVIDERS", ("gemini_image",))
    monkeypatch.setattr(access, "get_user_role", lambda s: "member")
    monkeypatch.setattr(A_status.group_store, "list_groups_for_user", lambda s, o: [])
    monkeypatch.setattr(access, "group_secret_map", lambda g: {})
    monkeypatch.setattr(access, "preloaded_presence_probe",
                        lambda s, org=None, groups=None: A_status.cascade.PRESENCE_PROBE)
    monkeypatch.setattr(A_status.db, "usage_today_map", lambda s: {"gemini_image": 3})
    monkeypatch.setattr(access, "reachable_team_key", lambda *a, **k: None)
    monkeypatch.setattr(A_status.credentials_store, "statuts_mono_compte",
                        lambda *a, **k: {})
    monkeypatch.setattr(access.db_entitlements, "valeurs_posees",
                        lambda oid, sub, droit, now=None:
                        access.db_entitlements.Posees(None, None))
    lus = []
    monkeypatch.setattr("oto_mcp.db.usage_org.du_jour",
                        lambda org_id, tool: lus.append((org_id, tool)) or 640)
    rung = access.CascadeRung("platform", "platform", "shared",
                              {"label": "shared", "daily_quota": 1_000,
                               "org_counter": _ORG})
    monkeypatch.setattr(A_status.chain_shadow, "resolution_rungs",
                        lambda sub, provider, **kw: [rung])
    st = A_status.status_for("b", org=_ORG, group=None)["providers"]["gemini_image"]
    assert st["quota_used_today"] == 640 and st["quota_daily"] == 1_000
    assert lus == [(_ORG, "gemini_image")]


# ── on a real database ────────────────────────────────────────────────────────

@pytest.fixture
def base(pg_dsn, monkeypatch):
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", os.urandom(32).hex())
    with base_jetable(pg_dsn):
        from oto_mcp.db import init_db
        init_db()
        yield


def _org(name: str) -> int:
    from oto_mcp.db import _connect
    with _connect() as conn:
        return int(conn.execute("INSERT INTO orgs (name) VALUES (%s) RETURNING id",
                                (name,)).fetchone()["id"])


def test_two_concurrent_debits_lose_no_unit(base):
    from oto_mcp.db import usage_org

    org = _org("concurrent")
    start = threading.Barrier(8)

    def debit():
        start.wait()
        for _ in range(25):
            usage_org.ajouter(org, "gemini_image", 3)

    threads = [threading.Thread(target=debit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert usage_org.du_jour(org, "gemini_image") == 8 * 25 * 3
    assert usage_org.du_jour(org, "elevenlabs") == 0


def test_the_counter_goes_with_its_org(base):
    from oto_mcp.db import _connect, usage_org

    org = _org("leaving")
    usage_org.ajouter(org, "gemini_image", 5)
    with _connect() as conn:
        conn.execute("DELETE FROM orgs WHERE id = %s", (org,))
    assert usage_org.du_jour(org, "gemini_image") == 0


def test_a_debit_is_a_positive_amount(base):
    from oto_mcp.db import usage_org

    with pytest.raises(ValueError):
        usage_org.ajouter(_org("zero"), "gemini_image", 0)
