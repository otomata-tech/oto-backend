"""Consoles B2/B3 (ADR 0047, *_op) : oto_procedure, oto_admin_doctrine,
oto_admin_signal, oto_org, oto_org_settings, oto_group, oto_scheduled_emails.

Routage op → handler de domaine réutilisé + champs requis + bascule des
bindings MCP (console ON, capacités d'origine OFF, REST conservé). Même modèle
que test_admin_console / test_connectors_console : on monkeypatch les handlers,
jamais la logique métier (couverte par leurs propres tests).
"""
import asyncio

import pytest

from oto_mcp.capabilities import admin_console as ac
from oto_mcp.capabilities import org_console as oc
from oto_mcp.capabilities import procedure_console as pc
from oto_mcp.capabilities import (
    doctrine_library,
    groups,
    groups_doctrine,
    groups_members,
    orgs,
    orgs_email_settings,
    orgs_field_filters,
    orgs_instructions,
    orgs_invites,
    orgs_mfa,
    orgs_update,
    scheduled_emails,
    usage,
)
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="alice", org_id=1)


def _tag(name):
    return lambda ctx, inp: {"called": name, "inp": inp}


def _atag(name):
    async def h(ctx, inp):
        return {"called": name, "inp": inp}
    return h


# ── oto_procedure ────────────────────────────────────────────────────────────
def test_procedure_routes(monkeypatch):
    monkeypatch.setattr(orgs_instructions, "_get_doctrine", _atag("get"))
    monkeypatch.setattr(orgs_instructions, "_list_doctrines", _tag("list"))
    monkeypatch.setattr(orgs_instructions, "_set_instruction", _atag("set"))
    monkeypatch.setattr(orgs_instructions, "_delete_instruction", _tag("delete"))
    monkeypatch.setattr(doctrine_library, "_list", _tag("library_list"))
    monkeypatch.setattr(doctrine_library, "_get", _tag("library_get"))
    monkeypatch.setattr(doctrine_library, "_publish", _tag("publish"))
    monkeypatch.setattr(doctrine_library, "_fork", _tag("fork"))
    monkeypatch.setattr(doctrine_library, "_unpublish", _tag("unpublish"))
    P = pc.ProcedureInput
    run = lambda inp: asyncio.run(pc._procedure(CTX, inp))
    out = run(P(op="get", slug="s1", scope=None))
    assert out["called"] == "get" and out["inp"].scope == "org"   # défaut re-posé
    assert run(P(op="list", query="q"))["called"] == "list"
    out = run(P(op="set", slug="s1", body_md="x", org=7))
    assert out["called"] == "set" and out["inp"].org == 7
    assert run(P(op="delete", slug="s1"))["called"] == "delete"
    assert run(P(op="library_list"))["called"] == "library_list"
    assert run(P(op="library_get", slug="pub"))["called"] == "library_get"
    out = run(P(op="publish", slug="s1"))
    assert out["called"] == "publish" and out["inp"].visibility == "public"
    assert run(P(op="fork", slug="pub", new_slug="mine"))["called"] == "fork"
    assert run(P(op="unpublish", id=3))["called"] == "unpublish"


def test_procedure_required_fields():
    P = pc.ProcedureInput
    run = lambda inp: asyncio.run(pc._procedure(CTX, inp))
    for inp, code in [
        (P(op="delete"), "missing_slug"),
        (P(op="library_get"), "missing_slug"),
        (P(op="publish"), "missing_slug"),
        (P(op="fork"), "missing_slug"),
        (P(op="unpublish"), "missing_id"),
    ]:
        with pytest.raises(AuthzDenied) as e:
            run(inp)
        assert e.value.code == code


def test_procedure_carries_skills_index_anchor():
    """L'index des skills est appendu par le middleware sur `_DOCTRINE_GET_TOOL` —
    il DOIT pointer le tool console (sinon l'index n'est plus servi nulle part),
    et rester un tool MCP réellement monté (garde du bug d'origine)."""
    from oto_mcp import middleware
    assert orgs_instructions._DOCTRINE_GET_TOOL == "oto_procedure"
    assert middleware._DOCTRINE_GET_TOOL == "oto_procedure"


# ── oto_admin_doctrine / oto_admin_signal ────────────────────────────────────
def test_admin_doctrine_routes(monkeypatch):
    monkeypatch.setattr(orgs_instructions, "_get_doctrine", _atag("get"))
    monkeypatch.setattr(orgs_instructions, "_list_doctrines", _tag("list"))
    monkeypatch.setattr(orgs_instructions, "_set_instruction", _atag("set"))
    monkeypatch.setattr(orgs_instructions, "_delete_instruction", _tag("delete"))
    D = ac.DoctrineAdminInput
    run = lambda inp: asyncio.run(ac._doctrine(CTX, inp))
    out = run(D(op="get", org_id=5))
    assert out["called"] == "get" and out["inp"].org_id == 5
    assert run(D(op="list", org_id=5))["called"] == "list"
    assert run(D(op="set", org_id=5, slug="s", body_md="x"))["called"] == "set"
    assert run(D(op="delete", org_id=5, slug="s"))["called"] == "delete"
    with pytest.raises(AuthzDenied) as e:
        run(D(op="delete", org_id=5))
    assert e.value.code == "missing_slug"


def test_admin_signal_routes(monkeypatch):
    monkeypatch.setattr(usage, "_signals", _tag("list"))
    monkeypatch.setattr(usage, "_resolve_signal", _tag("resolve"))
    S = ac.SignalAdminInput
    out = ac._signal(CTX, S(op="list", signal="gap", status="open"))
    assert out["called"] == "list" and out["inp"].signal == "gap"
    out = ac._signal(CTX, S(op="resolve", signal_id=9, note="fixed"))
    assert out["called"] == "resolve" and out["inp"].resolved is True
    with pytest.raises(AuthzDenied) as e:
        ac._signal(CTX, S(op="resolve"))
    assert e.value.code == "missing_signal_id"


# ── oto_org ──────────────────────────────────────────────────────────────────
def test_org_routes(monkeypatch):
    monkeypatch.setattr(orgs, "_create_org", _tag("create"))
    monkeypatch.setattr(orgs_update, "_update_org", _tag("update"))
    monkeypatch.setattr(orgs_update, "_archive_org", _tag("archive"))
    monkeypatch.setattr(orgs_invites, "_invite_create", _tag("invite"))
    monkeypatch.setattr(orgs_invites, "_invite_accept", _tag("accept"))
    O = oc.OrgInput
    assert oc._org(CTX, O(op="create", name="Acme"))["called"] == "create"
    assert oc._org(CTX, O(op="update", org_id=5, name="N"))["called"] == "update"
    assert oc._org(CTX, O(op="archive", org_id=5))["called"] == "archive"
    out = oc._org(CTX, O(op="invite", org_id=5, email="a@b.co"))
    assert out["called"] == "invite" and out["inp"].role == "org_member"
    assert oc._org(CTX, O(op="accept_invite", code="XYZ"))["called"] == "accept"
    with pytest.raises(AuthzDenied) as e:
        oc._org(CTX, O(op="create"))
    assert e.value.code == "missing_name"
    with pytest.raises(AuthzDenied) as e:
        oc._org(CTX, O(op="invite"))
    assert e.value.code == "missing_org"


# ── oto_org_settings ─────────────────────────────────────────────────────────
def test_org_settings_routes(monkeypatch):
    monkeypatch.setattr(orgs_email_settings, "_get_email_settings", _tag("email_get"))
    monkeypatch.setattr(orgs_email_settings, "_set_email_settings", _tag("email_set"))
    monkeypatch.setattr(orgs_mfa, "_get_org_mfa", _tag("mfa_get"))
    monkeypatch.setattr(orgs_mfa, "_set_org_mfa", _tag("mfa_set"))
    monkeypatch.setattr(orgs_field_filters, "_get_field_filters", _tag("ff_get"))
    monkeypatch.setattr(orgs_field_filters, "_set_field_filter", _tag("ff_set"))
    monkeypatch.setattr(orgs_field_filters, "_preview_field_filter", _tag("ff_preview"))
    S = oc.OrgSettingsInput
    assert oc._org_settings(CTX, S(op="get", domain="email", org_id=1))["called"] == "email_get"
    assert oc._org_settings(CTX, S(op="set", domain="email", org_id=1,
                                   connector="resend"))["called"] == "email_set"
    assert oc._org_settings(CTX, S(op="get", domain="mfa", org_id=1))["called"] == "mfa_get"
    out = oc._org_settings(CTX, S(op="set", domain="mfa", org_id=1, require=False))
    assert out["called"] == "mfa_set" and out["inp"].require is False   # False ≠ manquant
    assert oc._org_settings(CTX, S(op="get", domain="field_filters", org_id=1,
                                   include_schemas=True))["called"] == "ff_get"
    assert oc._org_settings(CTX, S(op="set", domain="field_filters", org_id=1,
                                   service="folk"))["called"] == "ff_set"
    assert oc._org_settings(CTX, S(op="preview", domain="field_filters", org_id=1,
                                   service="folk", payload={"a": 1}))["called"] == "ff_preview"


def test_org_settings_guards():
    S = oc.OrgSettingsInput
    with pytest.raises(AuthzDenied) as e:
        oc._org_settings(CTX, S(op="preview", domain="email", org_id=1))
    assert e.value.code == "unsupported_op"
    with pytest.raises(AuthzDenied) as e:
        oc._org_settings(CTX, S(op="set", domain="mfa", org_id=1))
    assert e.value.code == "missing_require"
    with pytest.raises(AuthzDenied) as e:
        oc._org_settings(CTX, S(op="preview", domain="field_filters", org_id=1, service="folk"))
    assert e.value.code == "missing_payload"


# ── oto_group ────────────────────────────────────────────────────────────────
def test_group_routes(monkeypatch):
    monkeypatch.setattr(groups, "_create_group", _tag("create"))
    monkeypatch.setattr(groups, "_list_my_groups", _tag("list_mine"))
    monkeypatch.setattr(groups_members, "_add_member", _tag("add"))
    monkeypatch.setattr(groups_members, "_remove_member", _tag("remove"))
    monkeypatch.setattr(groups_doctrine, "_set", _tag("set_instruction"))
    G = oc.GroupInput
    assert oc._group(CTX, G(op="create", org_id=1, name="Ventes"))["called"] == "create"
    # list = MES équipes (org active), sémantique de l'ex-oto_list_groups (list_mine).
    assert oc._group(CTX, G(op="list"))["called"] == "list_mine"
    out = oc._group(CTX, G(op="add_member", group_id=3, target="b@c.co"))
    assert out["called"] == "add" and out["inp"].role == "group_member"
    assert oc._group(CTX, G(op="remove_member", group_id=3, target="b@c.co"))["called"] == "remove"
    assert oc._group(CTX, G(op="set_instruction", group_id=3, slug="s",
                            body_md="x"))["called"] == "set_instruction"
    with pytest.raises(AuthzDenied) as e:
        oc._group(CTX, G(op="set_instruction", group_id=3, slug="s"))
    assert e.value.code == "missing_body"


# ── oto_scheduled_emails ─────────────────────────────────────────────────────
def test_scheduled_emails_routes(monkeypatch):
    monkeypatch.setattr(scheduled_emails, "_scheduled_list", _tag("list"))
    monkeypatch.setattr(scheduled_emails, "_scheduled_cancel", _tag("cancel"))
    E = oc.ScheduledEmailsInput
    out = oc._scheduled_emails(CTX, E(op="list", org_id=1, status="all"))
    assert out["called"] == "list" and out["inp"].status == "all"
    assert oc._scheduled_emails(CTX, E(op="cancel", org_id=1, email_id=4))["called"] == "cancel"
    with pytest.raises(AuthzDenied) as e:
        oc._scheduled_emails(CTX, E(op="cancel", org_id=1))
    assert e.value.code == "missing_email_id"


# ── Bascule des bindings MCP (B2+B3) ─────────────────────────────────────────
def test_consoles_carry_the_mcp_surface():
    from oto_mcp.capabilities.registry import CAPABILITIES
    caps = {c.key: c for c in CAPABILITIES}
    expected = {
        "org.procedure.console": "oto_procedure",
        "admin.doctrine": "oto_admin_doctrine",
        "admin.signal": "oto_admin_signal",
        "org.console": "oto_org",
        "org.settings.console": "oto_org_settings",
        "group.console": "oto_group",
        "org.scheduled_emails.console": "oto_scheduled_emails",
    }
    for key, mcp in expected.items():
        assert caps[key].mcp == mcp, key
    demoted = [
        "org.doctrine.get", "org.instruction.set", "org.instruction.delete",
        "org.doctrine.admin_get", "org.doctrine.admin_list",
        "org.instruction.admin_set", "org.instruction.admin_delete",
        "library.list", "library.get", "library.publish", "library.fork", "library.unpublish",
        "usage.signals", "usage.resolve_signal",
        "org.create", "org.update", "org.archive", "org.invite.create", "org.invite.accept",
        "org.email_settings.get", "org.email_settings.set",
        "org.mfa.get", "org.mfa.set",
        "org.field_filters.get", "org.field_filters.set", "org.field_filters.preview",
        "group.create", "group.list", "group.member.add", "group.member.remove",
        "group.instruction.set",
        "group.secret.set",   # retrait SEC : secret brut jamais en argument MCP
        "org.scheduled_email.list", "org.scheduled_email.cancel",
    ]
    for key in demoted:
        assert key in caps, key
        assert caps[key].mcp is None, key
        assert caps[key].rest_bindings(), key   # la face REST reste
    # Échappatoires anti-lockout intouchées.
    for key, mcp in [("org.use_org", "oto_use_org"), ("org.clear", "oto_clear_org"),
                     ("group.use", "oto_use_group"), ("group.clear", "oto_clear_group")]:
        assert caps[key].mcp == mcp, key
