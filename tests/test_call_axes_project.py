"""Axe-contexte d'appel `project=` (#108/#112) — slots de tableau (ADR 0035).

Contrats : exposition sur les tools `data_*` (slots), garde `can_access` + dérivation
et co-pose de l'org propriétaire, rejet de l'anonyme AVANT toute pose, et lecture par
le seam `access.current_project` (l'axe prime sur le bracelet de session)."""
import pytest

from oto_mcp import access, call_axes, ownership, session_org
from oto_mcp import group_store, org_store


def _params(name):
    return {a.param for a in call_axes.axes_for(name)}


def test_project_axis_applies_to_data_tools_only():
    assert "project" in _params("data_write")
    assert "project" in _params("data_rows")
    # connecteurs : épinglage d'identité fail-soft → hors périmètre de l'axe
    assert "project" not in _params("zoho_get")
    assert "project" not in _params("folk_search")
    assert "project" not in _params("oto_whoami")


def _project_axis():
    return next(a for a in call_axes.AXES if a.param == "project")


@pytest.mark.asyncio
async def test_pin_project_guards_access_and_coposes_org(monkeypatch):
    monkeypatch.setattr(call_axes, "require_axis_sub", lambda axis: "u")
    monkeypatch.setattr(session_org, "current_subdomain_candidate", lambda: None)
    monkeypatch.setattr(ownership, "can_access",
                        lambda sub, rt, rid, want="read": True)
    monkeypatch.setattr(ownership, "owner_of", lambda rt, rid: ("org", "42"))

    undo = await _project_axis().pin(9)
    try:
        assert session_org.current_call_project() == 9
        assert session_org.current_call_org() == 42      # org propriétaire co-posée
    finally:
        for reset, tok in reversed(undo):
            reset(tok)
    assert session_org.current_call_project() is None    # reset LIFO
    assert session_org.current_call_org() is None


@pytest.mark.asyncio
async def test_pin_project_personal_owner_uses_owner_personal_org(monkeypatch):
    monkeypatch.setattr(call_axes, "require_axis_sub", lambda axis: "actor")
    monkeypatch.setattr(session_org, "current_subdomain_candidate", lambda: None)
    monkeypatch.setattr(ownership, "can_access", lambda *a, **k: True)
    monkeypatch.setattr(ownership, "owner_of", lambda rt, rid: ("user", "owner-sub"))
    # l'org co-posée = org perso du PROPRIÉTAIRE, jamais du sub acteur
    monkeypatch.setattr(org_store, "get_personal_org",
                        lambda s: 77 if s == "owner-sub" else 999)

    undo = await _project_axis().pin(5)
    try:
        assert session_org.current_call_org() == 77
    finally:
        for reset, tok in reversed(undo):
            reset(tok)


@pytest.mark.asyncio
async def test_pin_project_rejects_inaccessible(monkeypatch):
    from mcp.shared.exceptions import McpError
    monkeypatch.setattr(call_axes, "require_axis_sub", lambda axis: "u")
    monkeypatch.setattr(session_org, "current_subdomain_candidate", lambda: None)
    monkeypatch.setattr(ownership, "can_access", lambda *a, **k: False)

    with pytest.raises(McpError):
        await _project_axis().pin(9)
    # aucune pose ne fuit après un refus
    assert session_org.current_call_project() is None
    assert session_org.current_call_org() is None


@pytest.mark.asyncio
async def test_pin_project_rejects_anon(monkeypatch):
    from mcp.shared.exceptions import McpError
    # sub anonyme → require_axis_sub (le vrai) lève AVANT toute pose/DB
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: None)
    called = {"owner_of": False}
    monkeypatch.setattr(ownership, "owner_of",
                        lambda *a: called.__setitem__("owner_of", True))
    with pytest.raises(McpError):
        await _project_axis().pin(9)
    assert called["owner_of"] is False        # rejet avant toute DB
    assert session_org.current_call_project() is None


@pytest.mark.asyncio
async def test_pin_project_subdomain_lock_rejects_foreign_org(monkeypatch):
    from mcp.shared.exceptions import McpError
    monkeypatch.setattr(call_axes, "require_axis_sub", lambda axis: "u")
    monkeypatch.setattr(session_org, "current_subdomain_candidate", lambda: 42)
    monkeypatch.setattr(ownership, "can_access", lambda *a, **k: True)
    monkeypatch.setattr(ownership, "owner_of", lambda rt, rid: ("org", "99"))  # ≠ 42
    with pytest.raises(McpError):
        await _project_axis().pin(9)


def test_current_project_reads_call_axis(monkeypatch):
    tok = session_org.set_call_project(123)
    try:
        assert access.current_project() == 123   # axe prime, déjà gardé à la pose
    finally:
        session_org.reset_call_project(tok)
