"""Capacité d'édition du bloc plateforme A (#50). La prose init plateforme vit
désormais dans `guides` (delivery='init', ADR 0042) : on monkeypatche le seam
`db.{get,set}_init_guide_db` — pas de vraie DB. (Le bloc onboarding a disparu :
l'onboarding est un projet, ADR 0032 §7.)
"""
import pytest

from oto_mcp import instructions
from oto_mcp.capabilities import platform_instructions as P
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="admin1", org_id=None)


@pytest.fixture
def store(monkeypatch):
    import oto_mcp.db as db
    rows: dict[tuple, dict] = {}

    def _get(scope, owner, slug):
        return rows.get((scope, owner, slug))

    def _set(scope, owner, slug, body_md):
        row = {"scope": scope, "owner_id": owner, "slug": slug,
               "body_md": body_md or "", "delivery": "init", "updated_at": "2026-06-30"}
        rows[(scope, owner, slug)] = row
        return row

    monkeypatch.setattr(db, "get_init_guide_db", _get)
    monkeypatch.setattr(db, "set_init_guide_db", _set)
    return rows


def test_get_returns_seed_when_absent(store):
    out = P._platform_instructions(CTX, P.PlatformInstrInput(op="get", key="secret_sauce"))
    assert out["is_seed"] is True
    assert "TA boîte à outils" in out["body_md"]            # le seed constant
    assert out["default_md"] == instructions.default_block("secret_sauce")


def test_set_then_get(store):
    P._platform_instructions(CTX, P.PlatformInstrInput(
        op="set", key="secret_sauce", body_md="NOUVELLE PROSE"))
    out = P._platform_instructions(CTX, P.PlatformInstrInput(op="get", key="secret_sauce"))
    assert out["is_seed"] is False and out["body_md"] == "NOUVELLE PROSE"
    assert out["updated_by"] is None                       # guides ne porte pas d'auteur
    # le défaut reste accessible (bouton « rétablir »)
    assert "TA boîte à outils" in out["default_md"]


def test_list_covers_block(store):
    out = P._platform_instructions(CTX, P.PlatformInstrInput(op="list"))
    assert out["keys"] == ["secret_sauce"]
    assert {b["key"] for b in out["blocks"]} == {"secret_sauce"}


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
