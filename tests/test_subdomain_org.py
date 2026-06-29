"""Endpoint scopé par org (`<slug>--mcp.oto.ninja`) — pin + garde + hard-lock.

Couvre subdomain_org (parsing/résolution) et l'épinglage dans access.current_org /
current_group : un membre est épinglé sur l'org du sous-domaine, un non-membre
retombe sur sa maison (zéro fuite), et un override de session (`oto_use_org`) ne
peut pas sortir du lock (précédence).
"""
import pytest

from oto_mcp import subdomain_org, session_org, access, org_store, roles, group_store


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    subdomain_org._CACHE.clear()
    monkeypatch.setattr(org_store, "list_all_orgs",
                        lambda: [{"id": 42, "name": "acme"},
                                 {"id": 2, "name": "Otomata Admin"}])
    # par défaut : membre de 42 uniquement = "rep" ; "other" membre de rien.
    monkeypatch.setattr(roles, "is_org_member",
                        lambda sub, oid: sub == "rep" and oid == 42)
    monkeypatch.setattr(org_store, "get_active_org", lambda sub: 99)
    yield


def test_slug_parsing():
    assert subdomain_org._slug_from_host("acme--mcp.oto.ninja") == "acme"
    assert subdomain_org._slug_from_host("ACME--mcp.oto.ninja:443") == "acme"
    assert subdomain_org._slug_from_host("mcp.oto.ninja") is None
    assert subdomain_org._slug_from_host("") is None


def test_org_resolution_and_cache():
    assert subdomain_org.org_id_for_host("acme--mcp.oto.ninja") == 42
    assert subdomain_org.org_id_for_host("inconnu--mcp.oto.ninja") is None
    assert subdomain_org._CACHE == {"acme": 42}  # seuls les hits cachés


def test_current_org_pins_member(monkeypatch):
    tok = session_org.set_subdomain_cv(42)
    try:
        assert access.current_org("rep") == 42
    finally:
        session_org.reset_subdomain_cv(tok)


def test_current_org_non_member_falls_back(monkeypatch):
    tok = session_org.set_subdomain_cv(42)
    try:
        assert access.current_org("other") == 99   # repli maison, jamais 42
    finally:
        session_org.reset_subdomain_cv(tok)


def test_hard_lock_session_override_ignored(monkeypatch):
    # Un oto_use_org vers une autre org pose un override de session — ignoré sous lock.
    monkeypatch.setattr(session_org, "current_override", lambda: (True, 50))
    tok = session_org.set_subdomain_cv(42)
    try:
        assert access.current_org("rep") == 42   # précédence du sous-domaine
    finally:
        session_org.reset_subdomain_cv(tok)


def test_current_group_locked_to_org(monkeypatch):
    monkeypatch.setattr(group_store, "get_active_group", lambda sub: 2)
    monkeypatch.setattr(group_store, "get_group",
                        lambda gid: {"id": 2, "org_id": 42} if gid == 2 else None)
    tok = session_org.set_subdomain_cv(42)
    try:
        assert access.current_group("rep") == 2          # groupe ⊂ org épinglée
        # groupe maison dans une AUTRE org → None (pas de fuite cross-org)
        monkeypatch.setattr(group_store, "get_group", lambda gid: {"id": 2, "org_id": 7})
        assert access.current_group("rep") is None
        # non-membre → None
        assert access.current_group("other") is None
    finally:
        session_org.reset_subdomain_cv(tok)
