"""ADR 0038 B3 — le scope groupe/projet est porté par l'appel, plus par la session.

Axe GROUPE : le bracelet (`oto_use_group`) est retiré — `current_group` résout
`jeton group= (déjà gardé à la pose) ?? consultation ?? équipe maison`, en tenant
l'invariant « groupe ⊂ org » face aux jetons d'org (`org=`/`project=`).

Axe PROJET (B3b) : même modèle — `current_project` = jeton d'appel `project=`
(déjà gardé `can_access` + org co-posée à la pose), le bracelet (`oto_use_project`)
n'est plus lu.

On exerce le vrai chemin de résolution (pas de stub des seams eux-mêmes) — leçon
row-shape/fail-open (CLAUDE.md).
"""
import pytest

from oto_mcp import access, group_store, session_org


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


# ── current_project (B3b) : jeton d'appel seul, bracelet ignoré ─────────────
def test_call_project_token_honored():
    tok = session_org.set_call_project(7)
    try:
        assert access.current_project() == 7
    finally:
        session_org.reset_call_project(tok)


def test_project_bracelet_ignored(monkeypatch):
    # Bracelet résiduel vers 7 (store inerte, ADR 0038 B3b) → IGNORÉ : hors projet.
    monkeypatch.setattr(session_org, "current_project_override", lambda: 7)
    assert access.current_project() is None


def test_no_project_token_is_none():
    assert access.current_project() is None
