"""Surcharge d'identité connecteur par projet actif (ADR 0032 §4, B2.2).

`access.project_pinned_identity(connector)` lit la config PRÉFAITE du lien connecteur
du projet de session (bracelet) → identity_id, ou None (repli défaut user). On
monkeypatche les seams (`current_project_override`, `list_project_links`), pas de DB.
"""
import pytest

from oto_mcp import access


@pytest.fixture
def wire(monkeypatch):
    state = {"pid": None, "links": []}
    monkeypatch.setattr(access.session_org, "current_project_override", lambda: state["pid"])
    monkeypatch.setattr(access.db, "list_project_links", lambda pid: state["links"])
    return state


def test_none_when_no_active_project(wire):
    wire["pid"] = None
    wire["links"] = [{"target_type": "connecteur", "target_ref": "google",
                      "config": {"identity_id": "a@x.co"}}]
    assert access.project_pinned_identity("google") is None


def test_returns_pinned_identity(wire):
    wire["pid"] = 7
    wire["links"] = [{"target_type": "connecteur", "target_ref": "google",
                      "identity_ref": "a@x.co", "config": {}}]
    assert access.project_pinned_identity("google") == "a@x.co"


def test_none_when_multiple_bindings_ambiguous(wire):
    # #57 : plusieurs bindings du même connecteur ⇒ ambigu ⇒ None (l'agent précise account=).
    wire["pid"] = 7
    wire["links"] = [{"target_type": "connecteur", "target_ref": "unipile", "identity_ref": "acc_A", "config": {}},
                     {"target_type": "connecteur", "target_ref": "unipile", "identity_ref": "acc_B", "config": {}}]
    assert access.project_pinned_identity("unipile") is None


def test_none_when_connector_not_pinned(wire):
    wire["pid"] = 7
    wire["links"] = [{"target_type": "connecteur", "target_ref": "unipile",
                      "config": {"identity_id": "acc_1"}}]
    assert access.project_pinned_identity("google") is None


def test_none_when_config_has_no_identity(wire):
    wire["pid"] = 7
    wire["links"] = [{"target_type": "connecteur", "target_ref": "google",
                      "config": {"instructions_md": "filtre thème mutuelle"}}]
    assert access.project_pinned_identity("google") is None


def test_explicit_project_id_overrides_session(wire):
    wire["pid"] = None  # pas de projet de session…
    wire["links"] = [{"target_type": "connecteur", "target_ref": "google",
                      "identity_ref": "b@x.co", "config": {}}]
    assert access.project_pinned_identity("google", project_id=7) == "b@x.co"


def test_fail_soft_on_error(wire, monkeypatch):
    wire["pid"] = 7
    monkeypatch.setattr(access.db, "list_project_links",
                        lambda pid: (_ for _ in ()).throw(RuntimeError("db down")))
    assert access.project_pinned_identity("google") is None   # jamais d'exception


# ── Câblage : google_oauth.credentials_for honore le projet actif (incrément B) ──

def test_credentials_for_applies_project_pin(monkeypatch):
    from oto_mcp import google_oauth
    seen = {}
    monkeypatch.setattr(access, "project_pinned_identity", lambda connector: "pinned@x.co")
    monkeypatch.setattr(access, "current_org", lambda sub: 39)
    monkeypatch.setattr(google_oauth.db, "get_google_oauth",
                        lambda sub, org, account=None: seen.update(account=account) or None)
    with pytest.raises(RuntimeError):              # pas de compte → erreur actionnable
        google_oauth.credentials_for("u1")        # account non passé → pin du projet
    assert seen["account"] == "pinned@x.co"        # le compte épinglé a bien été ciblé


def test_credentials_for_explicit_account_wins(monkeypatch):
    from oto_mcp import google_oauth
    seen = {}
    # Un compte explicite passé par l'appelant prime sur le pin du projet (jamais lu).
    monkeypatch.setattr(access, "project_pinned_identity",
                        lambda connector: (_ for _ in ()).throw(AssertionError("ne doit pas être lu")))
    monkeypatch.setattr(access, "current_org", lambda sub: 39)
    monkeypatch.setattr(google_oauth.db, "get_google_oauth",
                        lambda sub, org, account=None: seen.update(account=account) or None)
    with pytest.raises(RuntimeError):
        google_oauth.credentials_for("u1", account="explicit@x.co")
    assert seen["account"] == "explicit@x.co"


def test_credentials_for_no_project_keeps_default(monkeypatch):
    from oto_mcp import google_oauth
    seen = {}
    monkeypatch.setattr(access, "project_pinned_identity", lambda connector: None)  # pas de projet/pin
    monkeypatch.setattr(access, "current_org", lambda sub: 39)
    monkeypatch.setattr(google_oauth.db, "get_google_oauth",
                        lambda sub, org, account=None: seen.update(account=account) or None)
    with pytest.raises(RuntimeError):
        google_oauth.credentials_for("u1")
    assert seen["account"] is None                 # repli sur le défaut user (is_default)
