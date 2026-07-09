"""ADR 0044 §H — surface admin connecteur-centrique « accès plateforme ».

Endpoints `/api/admin/connectors/{provider}/platform-access` (GET lecture · POST acte
unique). On injecte les collaborateurs via `make_routes` (auth/json stubbés) et on
monkeypatch les fns store — pas de DB, pas de HTTP réel.
"""
import asyncio
import types

import pytest

from oto_mcp import api_routes_connectors as arc
import oto_mcp.credentials_store as cs


class FakeReq:
    def __init__(self, path_params=None, body=None):
        self.path_params = path_params or {}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _handlers(monkeypatch, *, super_admin=True):
    async def authenticate(request, verifier):
        return "admin", None

    def json_response(request, payload, status=200):
        return ("ok", status, payload)

    def json_error(request, status, code, *a, **k):
        return ("err", status, code)

    async def options_handler(request):
        return "opt"

    monkeypatch.setattr(arc.access, "is_platform_operator", lambda s: True)
    monkeypatch.setattr(arc.access, "is_super_admin", lambda s: super_admin)
    monkeypatch.setattr(arc.providers, "REGISTRY", {"unipile": object()})

    routes = arc.make_routes(None, authenticate, json_response, json_error, options_handler)
    idx = {}
    for r in routes:
        for m in r.methods:
            idx[(r.path, m)] = r.endpoint
    return idx


PATH = "/api/admin/connectors/{provider}/platform-access"


def test_read_assembles_beneficiaries(monkeypatch):
    """share_down (clé) ∪ option_comps (option), labels résolus, drapeaux has_key/has_option."""
    h = _handlers(monkeypatch)[(PATH, "GET")]
    monkeypatch.setattr(arc.access, "paid_option_for", lambda p: "unipile")
    monkeypatch.setattr(cs, "list_platform_instances",
                        lambda p: [{"label": "env", "share_mode": "closed",
                                    "share_down": ["org:42", "user:u1"], "share_side": [], "meta": {}}])
    monkeypatch.setattr(arc.db, "list_option_comps_for_option",
                        lambda opt: [{"entity_type": "org", "entity_id": "42"},
                                     {"entity_type": "user", "entity_id": "u2"}])
    monkeypatch.setattr(arc.org_store, "get_org", lambda oid: {"id": oid, "name": f"Org{oid}"})
    monkeypatch.setattr(arc.org_store, "effective_logo_url", lambda o: None)
    monkeypatch.setattr(arc.db, "get_user",
                        lambda s: {"name": None, "email": f"{s}@x.io"})

    kind, status, payload = asyncio.run(h(FakeReq(path_params={"provider": "unipile"})))
    assert kind == "ok"
    assert payload["paid_option"] == "unipile"
    assert payload["platform_key"] is True
    by = {(b["scope"], b["id"]): b for b in payload["beneficiaries"]}
    # org 42 : clé (share_down) ET option (comp) → les deux drapeaux
    assert by[("org", "42")]["has_key"] and by[("org", "42")]["has_option"]
    assert by[("org", "42")]["label"] == "Org42"
    # user u1 : clé seule ; user u2 : option seule
    assert by[("user", "u1")]["has_key"] and not by[("user", "u1")]["has_option"]
    assert by[("user", "u2")]["has_option"] and not by[("user", "u2")]["has_key"]
    assert by[("user", "u2")]["email"] == "u2@x.io"


def test_read_unknown_connector_404(monkeypatch):
    h = _handlers(monkeypatch)[(PATH, "GET")]
    kind, status, code = asyncio.run(h(FakeReq(path_params={"provider": "nope"})))
    assert (kind, status, code) == ("err", 404, "unknown_connector")


def test_write_on_composes_comp_and_grant(monkeypatch):
    """Acte unique ON : pose l'option comp ET le grant de clé (connecteur à option + clé)."""
    h = _handlers(monkeypatch)[(PATH, "POST")]
    monkeypatch.setattr(arc.access, "paid_option_for", lambda p: "unipile")
    monkeypatch.setattr(cs, "list_platform_instances", lambda p: [{"label": "env"}])
    monkeypatch.setattr(arc.org_store, "get_org", lambda oid: {"id": oid})
    rec = {"comp": [], "grant": [], "clear": [], "revoke": []}
    monkeypatch.setattr(arc.db, "set_option_comp",
                        lambda et, eid, opt, granted_by=None: rec["comp"].append((et, eid, opt)))
    monkeypatch.setattr(cs, "platform_grant",
                        lambda prov, scope, **k: rec["grant"].append((prov, scope)))

    kind, status, payload = asyncio.run(h(FakeReq(
        path_params={"provider": "unipile"}, body={"scope": "org", "id": "42", "on": True})))
    assert kind == "ok"
    assert rec["comp"] == [("org", "42", "unipile")]
    assert rec["grant"] == [("unipile", "org:42")]


def test_write_off_clears_and_revokes(monkeypatch):
    h = _handlers(monkeypatch)[(PATH, "POST")]
    monkeypatch.setattr(arc.access, "paid_option_for", lambda p: "unipile")
    monkeypatch.setattr(cs, "list_platform_instances", lambda p: [{"label": "env"}])
    monkeypatch.setattr(arc.db, "get_user", lambda s: {"sub": s})
    rec = {"clear": [], "revoke": []}
    monkeypatch.setattr(arc.db, "clear_option_comp",
                        lambda et, eid, opt: rec["clear"].append((et, eid, opt)))
    monkeypatch.setattr(cs, "platform_revoke",
                        lambda prov, scope: rec["revoke"].append((prov, scope)))

    kind, status, payload = asyncio.run(h(FakeReq(
        path_params={"provider": "unipile"}, body={"scope": "user", "id": "u1", "on": False})))
    assert kind == "ok"
    assert rec["clear"] == [("user", "u1", "unipile")]
    assert rec["revoke"] == [("unipile", "user:u1")]


def test_write_no_option_no_key_400(monkeypatch):
    """Connecteur sans option payante ET sans clé plateforme → rien à ouvrir."""
    h = _handlers(monkeypatch)[(PATH, "POST")]
    monkeypatch.setattr(arc.access, "paid_option_for", lambda p: None)
    monkeypatch.setattr(cs, "list_platform_instances", lambda p: [])
    monkeypatch.setattr(arc.org_store, "get_org", lambda oid: {"id": oid})
    kind, status, code = asyncio.run(h(FakeReq(
        path_params={"provider": "unipile"}, body={"scope": "org", "id": "42", "on": True})))
    assert (kind, status, code) == ("err", 400, "no_platform_access")


def test_write_requires_super_admin(monkeypatch):
    h = _handlers(monkeypatch, super_admin=False)[(PATH, "POST")]
    kind, status, code = asyncio.run(h(FakeReq(
        path_params={"provider": "unipile"}, body={"scope": "org", "id": "42", "on": True})))
    assert (kind, status, code) == ("err", 403, "forbidden")
