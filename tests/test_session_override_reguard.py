"""ADR 0038 B3 — le scope groupe/projet est porté par l'appel, plus par la session.

Axe GROUPE : le bracelet (`oto_use_group`) est retiré — `current_group` résout
`jeton group= (déjà gardé à la pose) ?? consultation ?? équipe maison`, en tenant
l'invariant « groupe ⊂ org » face aux jetons d'org (`org=`/`project=`).

Axe PROJET : le bracelet reste (B3b à venir) — sa re-garde à la RÉSOLUTION (#108)
est conservée : un Mcp-Session-Id réutilisé par un AUTRE compte ne doit pas hériter
du projet de session du précédent (`ownership.can_access`, ADR 0030).

On exerce le vrai chemin de résolution (pas de stub des seams eux-mêmes) — leçon
row-shape/fail-open (CLAUDE.md).
"""
import pytest

from oto_mcp import access, auth_hooks, group_store, ownership, session_org


@pytest.fixture(autouse=True)
def _neutralise_branches_annexes(monkeypatch):
    # Isole l'axe testé : pas de sous-domaine, pas de view-as.
    monkeypatch.setattr(session_org, "current_subdomain_candidate", lambda: None)
    monkeypatch.setattr(session_org, "current_view_group", lambda: None)
    monkeypatch.setattr(session_org, "current_view_org", lambda: None)
    yield


# ── current_group : jeton d'appel ───────────────────────────────────────────
def test_call_group_token_honored():
    # `group=5` posé par l'axe (déjà gardé can_read_group à la pose) → rendu tel quel.
    tok = session_org.set_call_group(5)
    try:
        assert access.current_group("u") == 5
    finally:
        session_org.reset_call_group(tok)


def test_group_bracelet_ignored(monkeypatch):
    # Bracelet résiduel vers 5 (store inerte, ADR 0038 B3) → IGNORÉ, repli maison.
    monkeypatch.setattr(session_org, "current_group_override", lambda: (True, 5))
    monkeypatch.setattr(group_store, "get_active_group", lambda sub: 99)
    monkeypatch.setattr(group_store, "get_group", lambda gid: {"id": gid, "org_id": 1})
    assert access.current_group("u") == 99


def test_home_group_hidden_under_foreign_org_token(monkeypatch):
    # Jeton `org=7` posé, équipe maison 99 appartient à l'org 1 ≠ 7 → niveau org
    # (invariant groupe ⊂ org : jamais le home_group d'une AUTRE org sous un jeton).
    monkeypatch.setattr(group_store, "get_active_group", lambda sub: 99)
    monkeypatch.setattr(group_store, "get_group", lambda gid: {"id": gid, "org_id": 1})
    tok = session_org.set_call_org(7)
    try:
        assert access.current_group("u") is None
    finally:
        session_org.reset_call_org(tok)


def test_home_group_kept_under_matching_org_token(monkeypatch):
    # Jeton `org=1`, équipe maison 99 DANS l'org 1 → rendue (cohérence tenue).
    monkeypatch.setattr(group_store, "get_active_group", lambda sub: 99)
    monkeypatch.setattr(group_store, "get_group", lambda gid: {"id": gid, "org_id": 1})
    tok = session_org.set_call_org(1)
    try:
        assert access.current_group("u") == 99
    finally:
        session_org.reset_call_org(tok)


def test_home_group_without_any_token(monkeypatch):
    monkeypatch.setattr(group_store, "get_active_group", lambda sub: 99)
    assert access.current_group("u") == 99


# ── current_project (bracelet conservé — B3b) : re-garde #108 ───────────────
def test_project_override_honored_when_can_access(monkeypatch):
    monkeypatch.setattr(session_org, "current_project_override", lambda: 7)
    monkeypatch.setattr(auth_hooks, "current_user_sub_from_token", lambda: "u")
    monkeypatch.setattr(ownership, "can_access", lambda sub, rt, rid, want="read": True)
    assert access.current_project() == 7


def test_project_override_ignored_when_no_access(monkeypatch):
    # session_id réutilisé par « other » : le projet 7 ne lui est pas accessible.
    monkeypatch.setattr(session_org, "current_project_override", lambda: 7)
    monkeypatch.setattr(auth_hooks, "current_user_sub_from_token", lambda: "other")
    monkeypatch.setattr(ownership, "can_access", lambda sub, rt, rid, want="read": False)
    assert access.current_project() is None  # hors projet, pas de fuite


def test_project_override_honored_without_sub(monkeypatch):
    # stdio/tests (sub non identifiable, hors surface authentifiée) : pas de régression.
    monkeypatch.setattr(session_org, "current_project_override", lambda: 7)
    monkeypatch.setattr(auth_hooks, "current_user_sub_from_token", lambda: None)
    assert access.current_project() == 7


def test_no_project_override_is_none(monkeypatch):
    monkeypatch.setattr(session_org, "current_project_override", lambda: None)
    assert access.current_project() is None
