"""Injection de la doctrine de base de l'org au `initialize` (otomata-private#49).

Le composer `instructions.compose_with_org_doctrine` (fail-open) + le middleware
`DynamicInstructionsMiddleware` qui réécrit `result.instructions` par-(sub, org).
Style `asyncio.run` + monkeypatch des seams, comme les autres tests du repo.
"""
import asyncio
import types

import oto_mcp.access as access
import oto_mcp.org_store as org_store
from oto_mcp import instructions as instr
from oto_mcp import middleware as mw


# ── composer ─────────────────────────────────────────────────────────────────
def test_compose_appends_when_doctrine_exists(monkeypatch):
    monkeypatch.setattr(org_store, "get_instruction",
                        lambda oid, slug: {"body_md": "Règle: toujours vérifier le SIREN."})
    monkeypatch.setattr(org_store, "get_org", lambda oid: {"name": "Acme"})
    out = instr.compose_with_org_doctrine("BASE", 7)
    assert out.startswith("BASE")
    assert "## Doctrine de ton organisation (Acme)" in out
    assert "toujours vérifier le SIREN" in out


def test_compose_noop_without_org():
    assert instr.compose_with_org_doctrine("BASE", None) == "BASE"


def test_compose_noop_when_body_empty(monkeypatch):
    monkeypatch.setattr(org_store, "get_instruction", lambda oid, slug: {"body_md": "   "})
    assert instr.compose_with_org_doctrine("BASE", 7) == "BASE"
    monkeypatch.setattr(org_store, "get_instruction", lambda oid, slug: None)
    assert instr.compose_with_org_doctrine("BASE", 7) == "BASE"


def test_compose_fail_open_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(org_store, "get_instruction", boom)
    assert instr.compose_with_org_doctrine("BASE", 7) == "BASE"


# ── middleware ───────────────────────────────────────────────────────────────
def _run(result, sub, monkeypatch):
    monkeypatch.setattr(mw, "current_user_sub_from_token", lambda: sub)

    async def call_next(ctx):
        return result
    return asyncio.run(mw.DynamicInstructionsMiddleware().on_initialize(object(), call_next))


def test_middleware_composes_for_org(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    monkeypatch.setattr(instr, "compose_with_org_doctrine",
                        lambda base, org: f"{base}\n[DOC org={org}]")
    res = types.SimpleNamespace(instructions="BASE")
    out = _run(res, "u1", monkeypatch)
    assert out.instructions == "BASE\n[DOC org=7]"


def test_middleware_noop_without_sub(monkeypatch):
    res = types.SimpleNamespace(instructions="BASE")
    out = _run(res, None, monkeypatch)
    assert out.instructions == "BASE"


def test_middleware_noop_when_no_instructions(monkeypatch):
    # instructions vide/None → on ne touche rien (et on ne lève pas).
    res = types.SimpleNamespace(instructions="")
    out = _run(res, "u1", monkeypatch)
    assert out.instructions == ""
