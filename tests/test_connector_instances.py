"""Tests B4 — projection lecture « instances de connecteur » (ADR 0038 §B).

Étages (patron test_agent_context / test_authz_combinators) :
1. grammaire de ref (roundtrip + malformés) ;
2. handler direct, seams monkeypatchés dans le namespace du module ;
3. garde RUNTIME de non-déchiffrement (règle dure 1 prouvée) ;
4. enregistrement de la capacité ; 5. smoke autz SUB_ONLY.
"""
import inspect
import json
import logging
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from oto_mcp import credentials_store, crypto, instance_refs
from oto_mcp.db import keys as db_keys
from oto_mcp.capabilities import connectors_instances as ci
from oto_mcp.capabilities._authz import SUB_ONLY
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx

SUB = "usr_x"
ORG = 8


# ─── 1. Grammaire de ref ─────────────────────────────────────────────────────

def test_ref_roundtrip_member():
    ref = instance_refs.make_member_ref(8, "usr_x", "zoho", "alexandra")
    assert ref == "member:8:usr_x:zoho:alexandra"
    p = instance_refs.parse_ref(ref)
    assert (p.level, p.org_id, p.sub, p.connector, p.account) == \
        ("member", 8, "usr_x", "zoho", "alexandra")


def test_ref_roundtrip_group_org_platform():
    g = instance_refs.parse_ref(instance_refs.make_group_ref(3, "hunter"))
    assert (g.level, g.group_id, g.connector, g.account) == ("group", 3, "hunter", "")
    o = instance_refs.parse_ref(instance_refs.make_org_ref(8, "bridge", "prod"))
    assert (o.level, o.org_id, o.connector, o.account) == ("org", 8, "bridge", "prod")
    pk = instance_refs.parse_ref(instance_refs.make_platform_ref(42))
    assert (pk.level, pk.platform_key_id, pk.connector) == ("platform", 42, None)


def test_ref_account_percent_encoded_roundtrip():
    # Un account libre peut contenir `:` et `@` — le percent-encoding garde le
    # split(":") non-ambigu et le roundtrip EXACT.
    acc = "team:sales@corp.io"
    ref = instance_refs.make_member_ref(8, "usr_x", "google", acc)
    assert ":" not in ref.split(":", 4)[4].replace("%3A", "")  # segment encodé
    assert "%3A" in ref and "%40" in ref
    assert instance_refs.parse_ref(ref).account == acc
    ref2 = instance_refs.make_group_ref(3, "zoho", acc)
    assert instance_refs.parse_ref(ref2).account == acc


def test_ref_empty_account_segment_omitted():
    ref = instance_refs.make_member_ref(8, "usr_x", "zoho")
    assert ref == "member:8:usr_x:zoho"          # 4 segments, pas de sentinel
    assert instance_refs.parse_ref(ref).account == ""


@pytest.mark.parametrize("bad", [
    "",                       # vide
    "bogus:1:zoho",           # level inconnu
    "member:xx:usr:zoho",     # org_id non-int
    "org:abc:zoho",           # id non-int
    "platform:1:zoho",        # arité fausse (platform = exactement 2)
    "platform:nope",          # id non-int
    "member:8:sub",           # arité fausse (member = 4-5)
    "group:3",                # arité fausse (group = 3-4)
    "org:8:zoho:acc:extra",   # arité fausse
    "member:8::zoho",         # segment vide
    "org:١٢:zoho",            # chiffres Unicode non-ASCII (isdigit les accepte —
                              # refs alias non-canoniques, revue B4)
])
def test_ref_malformed(bad):
    with pytest.raises(ValueError, match="invalid_instance_ref"):
        instance_refs.parse_ref(bad)


def test_ref_connector_percent_encoded_roundtrip():
    # Slug avec `:` (aucun au registre aujourd'hui, mais la grammaire est blindée
    # pour B5 où le ref devient un input) : encodé → split non-ambigu, roundtrip exact.
    ref = instance_refs.make_org_ref(8, "weird:slug")
    assert ref == "org:8:weird%3Aslug"
    p = instance_refs.parse_ref(ref)
    assert p.connector == "weird:slug" and p.org_id == 8


# ─── 2. Handler direct — seams monkeypatchés ─────────────────────────────────

def _row(connector, account="", secret_kind="api_key", set_by=SUB,
         set_at="2026-07-01 10:00:00", meta=None):
    """Ligne du coffre, forme `list_credentials`."""
    return {"connector": connector, "account": account, "secret_kind": secret_kind,
            "set_by": set_by, "set_at": set_at, "meta": meta or {}}


_PLATFORM_MODES = frozenset({"byo_user", "byo_org", "platform"})
_FAKE_REGISTRY = {
    "zoho": SimpleNamespace(label="Zoho", platform_key_open=False,
                            auth_modes=_PLATFORM_MODES),
    "hunter": SimpleNamespace(label="Hunter", platform_key_open=False,
                              auth_modes=_PLATFORM_MODES),
    "google": SimpleNamespace(label="Google", platform_key_open=False,
                              auth_modes=_PLATFORM_MODES),
    "bridge": SimpleNamespace(label="Bridge", platform_key_open=False,
                              auth_modes=_PLATFORM_MODES),
    "serpapi": SimpleNamespace(label="SerpAPI", platform_key_open=True,
                               auth_modes=_PLATFORM_MODES),
    # byo-only : le palier plateforme ne doit JAMAIS être projeté (gate auth_modes,
    # miroir de resolve_credential — revue B4, finding major).
    "brevo": SimpleNamespace(label="Brevo", platform_key_open=False,
                             auth_modes=frozenset({"byo_user", "byo_org"})),
}


@pytest.fixture()
def seams(monkeypatch):
    """Environnement neutre : coffre vide, aucun groupe/grant, RBAC ouvert,
    pas de super_admin. Chaque test relève les seams qui l'intéressent en
    peuplant `env.vault` / re-monkeypatchant dans le namespace du module."""
    vault: dict = {}   # (entity_type, entity_id) -> [rows]
    monkeypatch.setattr(ci.credentials_store, "list_credentials",
                        lambda et, eid: list(vault.get((et, eid), [])))
    monkeypatch.setattr(ci.group_store, "list_groups_for_user",
                        lambda sub, org_id=None: [])
    monkeypatch.setattr(ci.db, "list_grants_for_user", lambda sub: [])
    monkeypatch.setattr(ci.db, "list_org_grants", lambda org_id: [])
    monkeypatch.setattr(ci.db, "list_platform_keys_meta",
                        lambda provider=None: [])
    monkeypatch.setattr(ci.db, "org_restricted_connectors", lambda org_id: set())
    monkeypatch.setattr(ci.db, "member_allowed_connectors",
                        lambda sub, org_id: set())
    monkeypatch.setattr(ci.access, "is_super_admin", lambda sub: False)
    import oto_mcp.roles as roles_mod
    monkeypatch.setattr(roles_mod, "is_org_admin", lambda sub, org: False)
    monkeypatch.setattr(ci, "providers", SimpleNamespace(REGISTRY=_FAKE_REGISTRY))
    return SimpleNamespace(vault=vault, monkeypatch=monkeypatch)


def _run(connector=None, level=None, org_id=ORG, sub=SUB):
    ctx = ResolvedCtx(sub=sub, org_id=org_id)
    return ci._list_instances(ctx, ci.ListInstancesInput(connector=connector,
                                                         level=level))


def _member_key():
    return (credentials_store.MEMBER, credentials_store.member_id(ORG, SUB))


def test_member_multi_account(seams):
    seams.vault[_member_key()] = [
        _row("zoho", "alexandra", meta={"data_center": "eu", "is_default": True}),
        _row("zoho", "2 Zoho", set_by="usr_y"),
    ]
    out = _run()
    assert out["count"] == 2
    a, b = out["instances"]                       # tri par account : "2 Zoho" < "alexandra"
    assert a["account"] == "2 Zoho" and b["account"] == "alexandra"
    assert a["ref"] != b["ref"]
    assert b["ref"] == "member:8:usr_x:zoho:alexandra"
    assert a["level"] == b["level"] == "member"
    assert a["owner"] == {"type": "user", "id": SUB}
    assert a["secret_kind"] == "api_key" and a["set_by"] == "usr_y"
    assert a["name"] == "Zoho · 2 Zoho"           # nom dérivé, rien de stocké
    assert b["config"] == {"data_center": "eu"}   # label/is_default extraits
    assert b["is_default"] is True and "is_default" not in a
    assert a["via"] == "credential"


def test_groups_all_mine_no_active_flag(seams):
    seams.monkeypatch.setattr(ci.group_store, "list_groups_for_user",
                              lambda sub, org_id=None: [
                                  {"group_id": 3, "name": "Sales"},
                                  {"group_id": 5, "name": "Ops"},
                              ])
    seams.vault[("group", "3")] = [_row("hunter", set_by="chef")]
    seams.vault[("group", "5")] = [_row("zoho")]
    seams.vault[("group", "9")] = [_row("google")]   # groupe tiers → absent
    out = _run(level="group")
    assert out["count"] == 2
    by_conn = {i["connector"]: i for i in out["instances"]}
    assert by_conn["hunter"]["owner"] == {"type": "group", "id": 3, "label": "Sales"}
    assert by_conn["zoho"]["owner"] == {"type": "group", "id": 5, "label": "Ops"}
    assert "google" not in by_conn
    # PAS de flag « groupe actif » (état de session démonté en B3).
    assert "active_group" not in json.dumps(out)


def test_org_row_and_bridge_base_url(seams):
    seams.vault[("org", str(ORG))] = [
        _row("bridge", meta={"base_url": "https://mm-bridge.example"}),
    ]
    out = _run()
    (inst,) = out["instances"]
    assert inst["level"] == "org"
    assert inst["owner"] == {"type": "org", "id": ORG}
    assert inst["config"]["base_url"] == "https://mm-bridge.example"
    assert inst["ref"] == "org:8:bridge"


def test_platform_user_grant_wins_over_org_grant(seams):
    grant = {"platform_key_id": 11, "provider": "serpapi", "label": "main",
             "granted_at": "2026-07-01", "granted_by": "adm", "daily_quota": 100}
    seams.monkeypatch.setattr(ci.db, "list_grants_for_user", lambda sub: [grant])
    seams.monkeypatch.setattr(ci.db, "list_org_grants", lambda org_id: [dict(grant)])
    out = _run()
    assert out["count"] == 1                     # dédup par PROVIDER (1 clé résolue max)
    (inst,) = out["instances"]
    assert inst["via"] == "user_grant"
    assert inst["owner"] == {"type": "platform", "id": 11, "label": "main"}
    assert inst["name"] == "SerpAPI · main"
    assert inst["daily_quota"] == 100 and inst["granted_by"] == "adm"
    assert inst["ref"] == "platform:11"
    assert "secret_kind" not in inst and "config" not in inst


def test_platform_org_grant_alone(seams):
    seams.monkeypatch.setattr(ci.db, "list_org_grants", lambda org_id: [
        {"platform_key_id": 12, "provider": "zoho", "label": "shared",
         "granted_at": "2026-07-02", "granted_by": "adm", "daily_quota": None}])
    out = _run()
    (inst,) = out["instances"]
    assert inst["via"] == "org_grant" and inst["connector"] == "zoho"


def test_platform_free_tier_last_open_key_only(seams):
    seams.monkeypatch.setattr(ci.db, "list_platform_keys_meta", lambda provider=None: [
        # serpapi = platform_key_open → SEULE la dernière (created_at ASC) sort.
        {"id": 21, "provider": "serpapi", "label": "old", "created_at": "2026-01-01"},
        {"id": 22, "provider": "serpapi", "label": "open", "created_at": "2026-06-01"},
        # zoho n'est PAS platform_key_open → pas de free_tier.
        {"id": 23, "provider": "zoho", "label": "closed", "created_at": "2026-06-01"},
    ])
    out = _run()
    assert out["count"] == 1
    (inst,) = out["instances"]
    assert inst["via"] == "free_tier"
    assert inst["owner"] == {"type": "platform", "id": 22, "label": "open"}
    assert inst["created_at"] == "2026-06-01"


def test_platform_free_tier_deduped_by_grant(seams):
    seams.monkeypatch.setattr(ci.db, "list_grants_for_user", lambda sub: [
        {"platform_key_id": 22, "provider": "serpapi", "label": "open",
         "granted_at": "2026-07-01", "granted_by": None, "daily_quota": None}])
    seams.monkeypatch.setattr(ci.db, "list_platform_keys_meta", lambda provider=None: [
        {"id": 22, "provider": "serpapi", "label": "open", "created_at": "2026-06-01"}])
    out = _run()
    assert out["count"] == 1
    assert out["instances"][0]["via"] == "user_grant"   # priorité au grant


def test_platform_no_free_tier_phantom_after_rotation(seams):
    # Grant posé sur l'ANCIENNE clé (11), clé ouverte courante = 22 (rotation) :
    # la cascade ne résout qu'UNE clé plateforme par provider → pas de free-tier
    # fantôme en plus du grant (dédup par PROVIDER, revue B4).
    seams.monkeypatch.setattr(ci.db, "list_grants_for_user", lambda sub: [
        {"platform_key_id": 11, "provider": "serpapi", "label": "old",
         "granted_at": "2026-07-01", "granted_by": None, "daily_quota": None}])
    seams.monkeypatch.setattr(ci.db, "list_platform_keys_meta", lambda provider=None: [
        {"id": 22, "provider": "serpapi", "label": "new", "created_at": "2026-06-01"}])
    out = _run()
    assert out["count"] == 1
    assert out["instances"][0]["via"] == "user_grant"


def test_platform_gated_on_auth_modes(seams):
    # brevo = byo-only : la résolution refuse le chemin plateforme par construction
    # (gate auth_modes) → grant, org_grant et free-tier ne sont PAS projetés.
    seams.monkeypatch.setattr(ci.db, "list_grants_for_user", lambda sub: [
        {"platform_key_id": 31, "provider": "brevo", "label": "k",
         "granted_at": "x", "granted_by": None, "daily_quota": None}])
    seams.monkeypatch.setattr(ci.db, "list_org_grants", lambda org_id: [
        {"platform_key_id": 32, "provider": "brevo", "label": "k2",
         "granted_at": "x", "granted_by": None, "daily_quota": None}])
    assert _run()["count"] == 0
    # La clé BYO membre du même provider, elle, se projette normalement.
    seams.vault[_member_key()] = [_row("brevo")]
    out = _run()
    assert out["count"] == 1 and out["instances"][0]["level"] == "member"


def test_rbac_masks_restricted_without_allow(seams):
    seams.vault[_member_key()] = [_row("zoho")]
    seams.vault[("org", str(ORG))] = [_row("zoho"), _row("hunter")]
    seams.monkeypatch.setattr(ci.db, "list_grants_for_user", lambda sub: [
        {"platform_key_id": 11, "provider": "zoho", "label": "k",
         "granted_at": "x", "granted_by": None, "daily_quota": None}])
    seams.monkeypatch.setattr(ci.db, "org_restricted_connectors",
                              lambda org_id: {"zoho"})
    out = _run()
    # zoho masqué sur les 4 familles ; hunter (non restreint) reste.
    assert {i["connector"] for i in out["instances"]} == {"hunter"}
    # Avec allow → visible.
    seams.monkeypatch.setattr(ci.db, "member_allowed_connectors",
                              lambda sub, org_id: {"zoho"})
    assert {i["connector"] for i in _run()["instances"]} == {"hunter", "zoho"}


def test_rbac_super_admin_bypasses(seams):
    seams.vault[_member_key()] = [_row("zoho")]
    seams.monkeypatch.setattr(ci.db, "org_restricted_connectors",
                              lambda org_id: {"zoho"})
    seams.monkeypatch.setattr(ci.access, "is_super_admin", lambda sub: True)
    assert _run()["count"] == 1


def test_rbac_fail_open_logged(seams, caplog):
    seams.vault[_member_key()] = [_row("zoho")]

    def _boom(org_id):
        raise RuntimeError("db down")
    seams.monkeypatch.setattr(ci.db, "org_restricted_connectors", _boom)
    with caplog.at_level(logging.WARNING, logger=ci.logger.name):
        out = _run()
    assert out["count"] == 1                       # fail-open : tout listé
    assert any("fail-open" in r.message for r in caplog.records)


def test_meta_bearer_never_serialized(seams):
    # Défense en profondeur : même si une ligne arrivait avec un bearer dans
    # `meta` (list_credentials filtre normalement à la source), il ne sort pas.
    seams.vault[_member_key()] = [
        _row("google", "a@ex.io",
             meta={"access_token": "SECRET-BEARER", "scopes": "gmail",
                   "label": "Pro", "is_default": True}),
    ]
    out = _run()
    dumped = json.dumps(out)
    assert "SECRET-BEARER" not in dumped and "access_token" not in dumped
    (inst,) = out["instances"]
    assert inst["name"] == "Pro"                   # meta.label prime
    assert inst["config"] == {"scopes": "gmail"}   # label/is_default extraits
    assert inst["is_default"] is True


def test_sort_and_filters(seams):
    seams.vault[_member_key()] = [_row("zoho", "b"), _row("zoho", "a")]
    seams.monkeypatch.setattr(ci.group_store, "list_groups_for_user",
                              lambda sub, org_id=None: [{"group_id": 3, "name": "S"}])
    seams.vault[("group", "3")] = [_row("zoho")]
    seams.vault[("org", str(ORG))] = [_row("hunter"), _row("zoho")]
    seams.monkeypatch.setattr(ci.db, "list_grants_for_user", lambda sub: [
        {"platform_key_id": 11, "provider": "zoho", "label": "k",
         "granted_at": "x", "granted_by": None, "daily_quota": None}])
    out = _run()
    flat = [(i["connector"], i["level"], i["account"] if "account" in i else "")
            for i in out["instances"]]
    assert flat == [
        ("hunter", "org", ""),
        ("zoho", "member", "a"), ("zoho", "member", "b"),   # membre < groupe < org < platform
        ("zoho", "group", ""), ("zoho", "org", ""), ("zoho", "platform", ""),
    ]
    # Filtres connector= / level=.
    assert {i["connector"] for i in _run(connector="hunter")["instances"]} == {"hunter"}
    assert {i["level"] for i in _run(level="member")["instances"]} == {"member"}
    assert _run(connector="hunter", level="member")["count"] == 0


def test_level_invalid_rejected_by_pydantic():
    with pytest.raises(ValidationError):
        ci.ListInstancesInput(level="bogus")


def test_no_org_active_platform_only(seams):
    # ADR 0033 : sans org active, jamais de repli org-agnostique — familles
    # coffre vides, PAS d'org_grants (le seam ne fournit pas d'org).
    seams.vault[_member_key()] = [_row("zoho")]        # ne doit PAS sortir
    seams.monkeypatch.setattr(
        ci.db, "list_org_grants",
        lambda org_id: (_ for _ in ()).throw(AssertionError("org_grants sans org")))
    seams.monkeypatch.setattr(ci.db, "list_grants_for_user", lambda sub: [
        {"platform_key_id": 11, "provider": "zoho", "label": "k",
         "granted_at": "x", "granted_by": None, "daily_quota": None}])
    seams.monkeypatch.setattr(ci.db, "list_platform_keys_meta", lambda provider=None: [
        {"id": 22, "provider": "serpapi", "label": "open", "created_at": "y"}])
    out = _run(org_id=None)
    assert {(i["level"], i["via"]) for i in out["instances"]} == \
        {("platform", "user_grant"), ("platform", "free_tier")}


# ─── 3. Garde runtime de non-déchiffrement (règle dure 1) ────────────────────

def test_projection_never_decrypts(seams, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("decrypt appelé pendant la projection")
    monkeypatch.setattr(credentials_store, "get_credential", _boom)
    monkeypatch.setattr(credentials_store, "get_credential_with_meta", _boom)
    monkeypatch.setattr(db_keys, "_pk_reveal", _boom)
    monkeypatch.setattr(crypto, "decrypt", _boom)
    # Fixtures des 4 familles — le handler complet déroule sans lever.
    seams.vault[_member_key()] = [_row("zoho", "alexandra")]
    seams.monkeypatch.setattr(ci.group_store, "list_groups_for_user",
                              lambda sub, org_id=None: [{"group_id": 3, "name": "S"}])
    seams.vault[("group", "3")] = [_row("hunter")]
    seams.vault[("org", str(ORG))] = [_row("bridge", meta={"base_url": "https://b"})]
    seams.monkeypatch.setattr(ci.db, "list_grants_for_user", lambda sub: [
        {"platform_key_id": 11, "provider": "zoho", "label": "k",
         "granted_at": "x", "granted_by": None, "daily_quota": None}])
    seams.monkeypatch.setattr(ci.db, "list_platform_keys_meta", lambda provider=None: [
        {"id": 22, "provider": "serpapi", "label": "open", "created_at": "y"}])
    out = _run()
    assert out["count"] == 5


# ─── 4. Enregistrement de la capacité ────────────────────────────────────────

def test_capability_registered():
    from oto_mcp.capabilities.registry import CAPABILITIES
    caps = {c.key: c for c in CAPABILITIES}
    cap = caps["connectors.instances.list"]
    assert cap.mcp == "oto_connector_instances"
    (binding,) = cap.rest_bindings()
    assert (binding.verb, binding.path) == ("GET", "/api/me/connector-instances")
    assert cap.authz is SUB_ONLY
    # Règle mono-loop : handler sans await = def sync (threadpool).
    assert not inspect.iscoroutinefunction(cap.handler)


# ─── 5. Autz — smoke (la règle est couverte dans test_authz_combinators) ────

def test_sub_only_requires_auth():
    with pytest.raises(AuthzDenied) as ei:
        SUB_ONLY(RawCtx(sub=None))
    assert ei.value.status == 401 and ei.value.code == "auth_required"
