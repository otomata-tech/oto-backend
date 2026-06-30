"""Fiche « situation avec oto » (`oto_profile`) + seed du projet « Découverte ».

L'onboarding n'est plus un mode scripté : la fiche profil est entretenue au fil de
l'eau (`oto_profile`), et le projet d'accueil est semé à la création de l'org perso
(`discovery.seed_for_org`). On monkeypatche les seams DB — pas de vraie DB.
"""
import pytest

import oto_mcp.discovery as discovery
import oto_mcp.tools.profile as P


# ── seed du projet « Découverte » ────────────────────────────────────────────
def test_seed_for_org_creates_project(monkeypatch):
    rec = {}

    def create_project(ot, oid, name, brief, created_by=None):
        rec["args"] = (ot, oid, name, created_by)
        return 555
    monkeypatch.setattr("oto_mcp.db.create_project", create_project)
    monkeypatch.setattr("oto_mcp.db.log_project_activity", lambda *a, **k: None)

    pid = discovery.seed_for_org("u1", 77)
    assert pid == 555
    assert rec["args"] == ("org", "77", discovery.PROJECT_NAME, "u1")


def test_seed_for_org_best_effort(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr("oto_mcp.db.create_project", boom)
    # Un échec de seed ne lève pas (ne casse pas la création d'org) → None.
    assert discovery.seed_for_org("u1", 77) is None


# ── oto_profile (get / update) ───────────────────────────────────────────────
def _register(monkeypatch, *, state):
    """Enregistre oto_profile sur un faux mcp et renvoie le handler + un mouchard."""
    captured = {}

    class _Mcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured["fn"] = fn
                return fn
            return deco

    monkeypatch.setattr(P, "current_user_sub_from_token", lambda: "u1")
    monkeypatch.setattr(P.db, "get_account_profile", lambda sub: {"profile": dict(state)})

    def update(sub, fields=None):
        state.update(fields or {})
        return {"profile": dict(state)}
    monkeypatch.setattr(P.db, "update_account_profile", update)

    P.register(_Mcp())
    return captured["fn"]


def test_profile_get_reports_missing(monkeypatch):
    fn = _register(monkeypatch, state={"full_name": "Jean"})
    out = fn(ctx=None, op="get")
    assert out["profile"] == {"full_name": "Jean"}
    assert "full_name" not in out["missing"]      # rempli
    assert "role" in out["missing"]               # vide


def test_profile_update_persists_clean(monkeypatch):
    state = {}
    fn = _register(monkeypatch, state=state)
    out = fn(ctx=None, op="update", fields={"role": "fondateur", "crm": ""})
    assert out["profile"]["role"] == "fondateur"
    assert "crm" not in out["profile"]            # valeur vide ignorée


def test_profile_update_requires_fields(monkeypatch):
    from mcp.shared.exceptions import McpError
    fn = _register(monkeypatch, state={})
    with pytest.raises(McpError):
        fn(ctx=None, op="update", fields=None)


def test_profile_rejects_bad_op(monkeypatch):
    from mcp.shared.exceptions import McpError
    fn = _register(monkeypatch, state={})
    with pytest.raises(McpError):
        fn(ctx=None, op="bogus")
