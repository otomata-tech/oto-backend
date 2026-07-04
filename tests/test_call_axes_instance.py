"""Axe d'appel `instance=` (ADR 0038 §C / B6) — pin gardé + résolution EN DUR.

Deux moitiés : (1) la POSE (`_pin_instance`) refuse ref malformé / platform /
mismatch connecteur-tool / instance d'un autre membre / groupe non-lu, et co-pose
l'org de l'instance ; (2) la RÉSOLUTION (`_resolve_credential_impl`) lit exactement
la ligne désignée — l'explicite bat la proximité (org gagne même si une clé membre
existe), ligne absente = McpError SANS fallback, ref d'un autre provider = ignoré.
"""
import asyncio

import pytest
from mcp.shared.exceptions import McpError

from oto_mcp import access, call_axes, credentials_store, group_store, instance_refs, roles, session_org


@pytest.fixture(autouse=True)
def _sub(monkeypatch):
    # `call_axes` et `access` importent/résolvent le sub différemment — les deux.
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: "u")
    yield


def _unpin(undo):
    for reset, tok in reversed(undo):
        reset(tok)


# ─── 1. Pose (_pin_instance) ─────────────────────────────────────────────────

def test_pin_instance_member_ok_copins_org(monkeypatch):
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    ref = instance_refs.make_member_ref(8, "u", "zoho", "alexandra")

    async def _scenario():
        undo = await call_axes._pin_instance(ref, "zoho_records")
        try:
            pinned = session_org.current_call_instance()
            assert pinned.level == "member" and pinned.account == "alexandra"
            assert session_org.current_call_org() == 8       # org co-posée
        finally:
            _unpin(undo)
        assert session_org.current_call_instance() is None
        assert session_org.current_call_org() is None

    asyncio.run(_scenario())


def test_pin_instance_rejects_malformed():
    with pytest.raises(McpError, match="invalide"):
        asyncio.run(call_axes._pin_instance("n'importe:quoi", "zoho_records"))


def test_pin_instance_rejects_platform_ref():
    with pytest.raises(McpError, match="platform"):
        asyncio.run(call_axes._pin_instance("platform:11", "serpapi_search"))


def test_pin_instance_rejects_connector_mismatch():
    # Ref zoho passé sur un tool hunter → refus AVANT toute garde DB.
    ref = instance_refs.make_org_ref(8, "zoho")
    with pytest.raises(McpError, match="hunter"):
        asyncio.run(call_axes._pin_instance(ref, "hunter_domain_search"))


def test_pin_instance_rejects_other_members_instance(monkeypatch):
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    ref = instance_refs.make_member_ref(8, "quelqu_un_d_autre", "zoho")
    with pytest.raises(McpError, match="autre membre"):
        asyncio.run(call_axes._pin_instance(ref, "zoho_records"))
    assert session_org.current_call_instance() is None


def test_pin_instance_rejects_group_non_reader(monkeypatch):
    monkeypatch.setattr(roles, "can_read_group", lambda sub, gid: False)
    ref = instance_refs.make_group_ref(3, "zoho")
    with pytest.raises(McpError, match="groupe"):
        asyncio.run(call_axes._pin_instance(ref, "zoho_records"))


def test_pin_instance_group_ok_copins_parent_org(monkeypatch):
    monkeypatch.setattr(roles, "can_read_group", lambda sub, gid: True)
    monkeypatch.setattr(group_store, "get_group", lambda gid: {"id": gid, "org_id": 42})
    ref = instance_refs.make_group_ref(3, "zoho")

    async def _scenario():
        undo = await call_axes._pin_instance(ref, "zoho_records")
        try:
            assert session_org.current_call_instance().group_id == 3
            assert session_org.current_call_org() == 42
        finally:
            _unpin(undo)

    asyncio.run(_scenario())


def test_pin_instance_none_is_inert():
    assert asyncio.run(call_axes._pin_instance(None, "zoho_records")) == []


# ─── 2. Résolution (_resolve_credential_impl, instance épinglée) ─────────────

@pytest.fixture()
def resolution(monkeypatch):
    """Neutralise les à-côtés de la résolution ; le coffre est un dict contrôlé."""
    monkeypatch.setattr(access, "require_connector_access", lambda p, s: None)
    vault: dict = {}   # (etype, eid, provider, account) -> secret
    monkeypatch.setattr(credentials_store, "get_credential",
                        lambda et, eid, prov, acc="": vault.get((et, eid, prov, acc)))
    return vault


def _pin(ref_str):
    return session_org.set_call_instance(instance_refs.parse_ref(ref_str))


def test_resolution_org_instance_beats_member_key(resolution, monkeypatch):
    # L'EXPLICITE bat la proximité : une instance org demandée est servie même si
    # une clé membre existe — et le palier membre n'est même pas consulté.
    resolution[("org", "5", "zoho", "")] = "SECRET-ORG"
    from oto_mcp import db
    monkeypatch.setattr(db, "get_member_api_key",
                        lambda *a, **k: pytest.fail("palier membre consulté"))
    tok = _pin("org:5:zoho")
    try:
        rc = access._resolve_credential_impl("zoho", "auto", "u")
    finally:
        session_org.reset_call_instance(tok)
    assert rc.secret == "SECRET-ORG" and rc.mode == "org"
    assert (rc.entity_type, rc.entity_id) == ("org", "5")


def test_resolution_member_instance_with_account(resolution):
    eid = credentials_store.member_id(8, "u")
    resolution[(credentials_store.MEMBER, eid, "zoho", "alexandra")] = "SECRET-ALX"
    tok = _pin("member:8:u:zoho:alexandra")
    try:
        rc = access._resolve_credential_impl("zoho", "auto", "u")
    finally:
        session_org.reset_call_instance(tok)
    assert rc.secret == "SECRET-ALX" and rc.mode == "user"
    assert rc.account == "alexandra"


def test_resolution_missing_instance_hard_error_no_fallback(resolution, monkeypatch):
    # La ligne n'existe plus : erreur actionnable, PAS de repli vers un autre palier
    # (l'org 5 a pourtant un secret… d'un autre compte — il ne doit PAS servir).
    resolution[("org", "5", "zoho", "")] = "SECRET-AUTRE"
    tok = _pin("org:5:zoho:parti")   # account `parti` retiré du coffre
    try:
        with pytest.raises(McpError, match="ne résout plus"):
            access._resolve_credential_impl("zoho", "auto", "u")
    finally:
        session_org.reset_call_instance(tok)


# ── Binding de projet (ADR 0038 B5) : le projet fournit le ref ──────────────

def _bind_project(monkeypatch, pid, links):
    monkeypatch.setattr(session_org, "current_call_project", lambda: pid)
    from oto_mcp import db
    monkeypatch.setattr(db, "list_project_links", lambda p: links)


def test_resolution_project_binding_resolves_hard(resolution, monkeypatch):
    # project=P porte un binding zoho → instance org:5 servie, re-gardée pour l'APPELANT.
    resolution[("org", "5", "zoho", "")] = "SECRET-BOUND"
    _bind_project(monkeypatch, 7, [
        {"target_type": "connecteur", "target_ref": "zoho",
         "config": {"instance_ref": "org:5:zoho"}}])
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    rc = access._resolve_credential_impl("zoho", "auto", "u")
    assert rc.secret == "SECRET-BOUND" and rc.mode == "org"


def test_resolution_project_binding_reguards_caller(resolution, monkeypatch):
    # L'appelant du projet partagé n'est PAS membre de l'org de l'instance bindée →
    # refus actionnable, jamais le credential (le binding ne rouvre pas l'org).
    resolution[("org", "5", "zoho", "")] = "SECRET-BOUND"
    _bind_project(monkeypatch, 7, [
        {"target_type": "connecteur", "target_ref": "zoho",
         "config": {"instance_ref": "org:5:zoho"}}])
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: False)
    with pytest.raises(McpError, match="pas membre"):
        access._resolve_credential_impl("zoho", "auto", "u")


def test_resolution_explicit_instance_beats_project_binding(resolution, monkeypatch):
    # `instance=` explicite (jeton le plus spécifique) prime sur le binding du projet.
    eid = credentials_store.member_id(8, "u")
    resolution[(credentials_store.MEMBER, eid, "zoho", "perso")] = "SECRET-EXPLICITE"
    _bind_project(monkeypatch, 7, [
        {"target_type": "connecteur", "target_ref": "zoho",
         "config": {"instance_ref": "org:5:zoho"}}])
    tok = _pin("member:8:u:zoho:perso")
    try:
        rc = access._resolve_credential_impl("zoho", "auto", "u")
    finally:
        session_org.reset_call_instance(tok)
    assert rc.secret == "SECRET-EXPLICITE"


def test_resolution_multiple_bindings_actionable_error(resolution, monkeypatch):
    # 2 bindings zoho dans le projet, pas d'instance= → erreur qui LISTE les choix.
    _bind_project(monkeypatch, 7, [
        {"target_type": "connecteur", "target_ref": "zoho",
         "config": {"instance_ref": "org:5:zoho"}},
        {"target_type": "connecteur", "target_ref": "zoho",
         "config": {"instance_ref": "member:5:u:zoho:alx"}}])
    with pytest.raises(McpError, match="PLUSIEURS instances"):
        access._resolve_credential_impl("zoho", "auto", "u")


def test_resolution_foreign_provider_ref_ignored(resolution, monkeypatch):
    # Ref zoho épinglé mais résolution du provider hunter (résolution auxiliaire) :
    # le ref est ignoré → cascade normale (ici : rien ne résout → erreur STANDARD,
    # pas l'erreur d'instance).
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    tok = _pin("org:5:zoho")
    try:
        with pytest.raises(McpError) as ei:
            access._resolve_credential_impl("hunter", "byo", "u")
    finally:
        session_org.reset_call_instance(tok)
    assert "ne résout plus" not in str(ei.value)   # erreur cascade, pas instance
