"""Pin d'appel `account=`/`identity=` sur le compte opéré Unipile (ADR 0051 B2).

Le compte épinglé POUR L'APPEL prime sur le pointeur maison, gardé (accordé #55 OU
propre), éphémère, sans repli muet."""
import pytest

from oto_mcp import connector_identities as ci, session_org, db


def _grants(mapping):
    return lambda sub, prov: dict(mapping)


def _own(ids):
    return lambda sub: [{"provider": "LINKEDIN", "account_id": a} for a in ids]


def test_pin_granted_account_wins(monkeypatch):
    monkeypatch.setattr(db, "granted_accounts_for", _grants({"acc_alexis": {"owner_sub": "alexis"}}))
    monkeypatch.setattr(db, "list_unipile_accounts", _own(["acc_own"]))
    monkeypatch.setattr(db, "get_operated_account", lambda s, p: None)
    tok = session_org.set_call_account("acc_alexis")
    try:
        assert ci.resolve_operated_account_id("alexandra", "LINKEDIN") == "acc_alexis"
    finally:
        session_org.reset_call_account(tok)


def test_pin_own_account_ok(monkeypatch):
    monkeypatch.setattr(db, "granted_accounts_for", _grants({}))
    monkeypatch.setattr(db, "list_unipile_accounts", _own(["acc_own"]))
    monkeypatch.setattr(db, "get_operated_account", lambda s, p: None)
    tok = session_org.set_call_account("acc_own")
    try:
        assert ci.resolve_operated_account_id("me", "LINKEDIN") == "acc_own"
    finally:
        session_org.reset_call_account(tok)


def test_pin_not_operable_raises(monkeypatch):
    monkeypatch.setattr(db, "granted_accounts_for", _grants({}))
    monkeypatch.setattr(db, "list_unipile_accounts", _own(["acc_own"]))
    monkeypatch.setattr(db, "get_operated_account", lambda s, p: None)
    tok = session_org.set_call_account("acc_stranger")
    try:
        with pytest.raises(ValueError):
            ci.resolve_operated_account_id("me", "LINKEDIN")
    finally:
        session_org.reset_call_account(tok)


def test_pin_overrides_home_pointer(monkeypatch):
    # pointeur maison = acc_home, mais pin = acc_alexis (accordé) → le pin gagne
    monkeypatch.setattr(db, "granted_accounts_for", _grants({"acc_alexis": {}}))
    monkeypatch.setattr(db, "list_unipile_accounts", _own([]))
    monkeypatch.setattr(db, "get_operated_account", lambda s, p: {"account_id": "acc_home"})
    tok = session_org.set_call_account("acc_alexis")
    try:
        assert ci.resolve_operated_account_id("x", "LINKEDIN") == "acc_alexis"
    finally:
        session_org.reset_call_account(tok)


def test_no_pin_uses_home_pointer(monkeypatch):
    monkeypatch.setattr(db, "granted_accounts_for", _grants({"acc_home": {}}))
    monkeypatch.setattr(db, "list_unipile_accounts", _own([]))
    monkeypatch.setattr(db, "get_operated_account", lambda s, p: {"account_id": "acc_home"})
    assert ci.resolve_operated_account_id("x", "LINKEDIN") == "acc_home"
