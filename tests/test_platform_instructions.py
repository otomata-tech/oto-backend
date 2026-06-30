"""Capacité d'édition des blocs plateforme A/B (#50). On monkeypatche les seams DB
(get/set/list_platform_instruction) — pas de vraie DB.
"""
import pytest

from oto_mcp import instructions
from oto_mcp.capabilities import platform_instructions as P
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="admin1", org_id=None)


@pytest.fixture
def store(monkeypatch):
    rows: dict[str, dict] = {}
    monkeypatch.setattr(P.db, "get_platform_instruction", lambda key: rows.get(key))
    monkeypatch.setattr(P.db, "list_platform_instructions",
                        lambda: [dict(v) for v in rows.values()])

    def _set(key, body_md, updated_by=None):
        rows[key] = {"key": key, "body_md": body_md, "updated_at": "2026-06-30",
                     "updated_by": updated_by}
    monkeypatch.setattr(P.db, "set_platform_instruction", _set)
    return rows


def test_get_returns_seed_when_absent(store):
    out = P._platform_instructions(CTX, P.PlatformInstrInput(op="get", key="secret_sauce"))
    assert out["is_seed"] is True
    assert "TA boîte à outils" in out["body_md"]            # le seed constant
    assert out["default_md"] == instructions.default_block("secret_sauce")


def test_set_then_get(store):
    P._platform_instructions(CTX, P.PlatformInstrInput(
        op="set", key="onboarding", body_md="NOUVELLE PROSE"))
    out = P._platform_instructions(CTX, P.PlatformInstrInput(op="get", key="onboarding"))
    assert out["is_seed"] is False and out["body_md"] == "NOUVELLE PROSE"
    assert out["updated_by"] == "admin1"
    # le défaut reste accessible (bouton « rétablir »)
    assert "oto_onboarding()" in out["default_md"]


def test_list_covers_both_blocks(store):
    out = P._platform_instructions(CTX, P.PlatformInstrInput(op="list"))
    assert out["keys"] == ["secret_sauce", "onboarding"]
    assert {b["key"] for b in out["blocks"]} == {"secret_sauce", "onboarding"}


def test_unknown_key_rejected(store):
    with pytest.raises(AuthzDenied) as e:
        P._platform_instructions(CTX, P.PlatformInstrInput(op="get", key="bogus"))
    assert e.value.code == "unknown_key"


def test_set_requires_body(store):
    with pytest.raises(AuthzDenied) as e:
        P._platform_instructions(CTX, P.PlatformInstrInput(op="set", key="secret_sauce"))
    assert e.value.code == "missing_body"


def test_capability_registered():
    from oto_mcp.capabilities.registry import CAPABILITIES
    by_key = {c.key: c for c in CAPABILITIES}
    assert by_key["platform.instructions"].mcp == "oto_admin_platform_instructions"
    rest = by_key["platform.instructions.set"].rest
    assert rest is not None and rest.verb == "PUT"
    assert rest.path == "/api/admin/platform-instructions/{key}"
