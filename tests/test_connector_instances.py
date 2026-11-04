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
from oto_mcp.capabilities.connectors import instances as ci
from oto_mcp.capabilities._authz import SUB_ONLY
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx

SUB = "usr_x"
ORG = 8


# ─── 1. Grammaire de ref ─────────────────────────────────────────────────────

def test_ref_roundtrip_member():
    ref = instance_refs.make_member_ref(8, "usr_x", "zoho", "jane")
    assert ref == "member:8:usr_x:zoho:jane"
    p = instance_refs.parse_ref(ref)
    assert (p.level, p.org_id, p.sub, p.connector, p.account) == \
        ("member", 8, "usr_x", "zoho", "jane")


def test_ref_roundtrip_group_org_platform():
    g = instance_refs.parse_ref(instance_refs.make_group_ref(3, "hunter"))
    assert (g.level, g.group_id, g.connector, g.account) == ("group", 3, "hunter", "")
    o = instance_refs.parse_ref(instance_refs.make_org_ref(8, "bridge", "prod"))
    assert (o.level, o.org_id, o.connector, o.account) == ("org", 8, "bridge", "prod")
    pk = instance_refs.parse_ref(instance_refs.make_platform_ref("zoho", "main"))
    assert (pk.level, pk.connector, pk.label) == ("platform", "zoho", "main")


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
    "platform:zoho",          # arité fausse (ADR 0044 §F : platform = 3 segments connector:label)
    "platform:zoho:",         # segment label vide
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
    """Environnement neutre : coffre vide, aucun groupe/grant,
    pas de super_admin. Chaque test relève les seams qui l'intéressent en
    peuplant `env.vault` / re-monkeypatchant dans le namespace du module."""
    vault: dict = {}   # (entity_type, entity_id) -> [rows]
    # Lot L6 : le quadruplet de coffre → identifiant stable. Vide par défaut (une
    # base où le backfill de boot n'a encore rien nommé), peuplé par les tests qui
    # s'y intéressent. Stubbé ICI plutôt que laissé tomber en fail-open : sans ça la
    # projection réussirait sans jamais exercer le chemin qu'on croit tester.
    instances: dict = {}
    monkeypatch.setattr(
        ci.db, "instances_for_vault_rows",
        lambda keys: {k: {"id": instances[k]} for k in keys if k in instances})
    monkeypatch.setattr(ci.credentials_store, "list_credentials",
                        lambda et, eid: list(vault.get((et, eid), [])))
    monkeypatch.setattr(ci.group_store, "list_groups_for_user",
                        lambda sub, org_id=None: [])
    monkeypatch.setattr(ci.db, "list_grants_for_user", lambda sub: [])
    monkeypatch.setattr(ci.db, "list_org_grants", lambda org_id: [])
    # ADR 0044 §F : le free-tier de la projection lit credentials_store.list_platform_credentials.
    monkeypatch.setattr(ci.credentials_store, "list_platform_credentials",
                        lambda provider=None: [])
    monkeypatch.setattr(ci.access, "is_super_admin", lambda sub: False)
    import oto_mcp.roles as roles_mod
    monkeypatch.setattr(roles_mod, "is_org_admin", lambda sub, org: False)
    # The carrier translation stays the REAL one: a fake would hide the very seam the
    # `connector=` filter shares with the loan.
    from oto_mcp import providers as vrais_providers
    monkeypatch.setattr(ci, "providers", SimpleNamespace(
        REGISTRY=_FAKE_REGISTRY,
        credential_provider=vrais_providers.credential_provider))
    return SimpleNamespace(vault=vault, instances=instances, monkeypatch=monkeypatch)


def _run(connector=None, level=None, org_id=ORG, sub=SUB):
    ctx = ResolvedCtx(sub=sub, org_id=org_id)
    return ci._list_instances(ctx, ci.ListInstancesInput(connector=connector,
                                                         level=level))


def _member_key():
    return (credentials_store.MEMBER, credentials_store.member_id(ORG, SUB))


def test_member_multi_account(seams):
    seams.vault[_member_key()] = [
        _row("zoho", "jane", meta={"data_center": "eu", "is_default": True}),
        _row("zoho", "2 Zoho", set_by="usr_y"),
    ]
    out = _run()
    assert out["count"] == 2
    a, b = out["instances"]                       # tri par account : "2 Zoho" < "jane"
    assert a["account"] == "2 Zoho" and b["account"] == "jane"
    assert a["ref"] != b["ref"]
    assert b["ref"] == "member:8:usr_x:zoho:jane"
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
        _row("bridge", meta={"base_url": "https://acme-bridge.example"}),
    ]
    out = _run()
    (inst,) = out["instances"]
    assert inst["level"] == "org"
    assert inst["owner"] == {"type": "org", "id": ORG}
    assert inst["config"]["base_url"] == "https://acme-bridge.example"
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
    assert inst["owner"] == {"type": "platform", "label": "main"}
    assert inst["name"] == "SerpAPI · main"
    assert inst["daily_quota"] == 100
    assert inst["ref"] == "platform:serpapi:main"
    assert "secret_kind" not in inst and "config" not in inst


def test_platform_org_grant_alone(seams):
    seams.monkeypatch.setattr(ci.db, "list_org_grants", lambda org_id: [
        {"platform_key_id": 12, "provider": "zoho", "label": "shared",
         "granted_at": "2026-07-02", "granted_by": "adm", "daily_quota": None}])
    out = _run()
    (inst,) = out["instances"]
    assert inst["via"] == "org_grant" and inst["connector"] == "zoho"


def test_platform_free_tier_last_open_key_only(seams):
    # ADR 0044 §F : le free-tier lit list_platform_credentials (trié set_at DESC → 1re par
    # provider = la plus récente). serpapi = platform_key_open ; zoho non.
    seams.monkeypatch.setattr(ci.credentials_store, "list_platform_credentials", lambda provider=None: [
        {"provider": "serpapi", "label": "open", "set_at": "2026-06-01"},
        {"provider": "serpapi", "label": "old", "set_at": "2026-01-01"},
        {"provider": "zoho", "label": "closed", "set_at": "2026-06-01"},
    ])
    out = _run()
    assert out["count"] == 1
    (inst,) = out["instances"]
    assert inst["via"] == "free_tier"
    assert inst["owner"] == {"type": "platform", "label": "open"}
    assert inst["set_at"] == "2026-06-01"


def test_platform_free_tier_deduped_by_grant(seams):
    seams.monkeypatch.setattr(ci.db, "list_grants_for_user", lambda sub: [
        {"provider": "serpapi", "label": "open", "daily_quota": None}])
    seams.monkeypatch.setattr(ci.credentials_store, "list_platform_credentials", lambda provider=None: [
        {"provider": "serpapi", "label": "open", "set_at": "2026-06-01"}])
    out = _run()
    assert out["count"] == 1
    assert out["instances"][0]["via"] == "user_grant"   # priorité au grant


def test_platform_no_free_tier_phantom_after_rotation(seams):
    # Grant posé sur l'ANCIENNE clé (11), clé ouverte courante = 22 (rotation) :
    # la cascade ne résout qu'UNE clé plateforme par provider → pas de free-tier
    # fantôme en plus du grant (dédup par PROVIDER, revue B4).
    seams.monkeypatch.setattr(ci.db, "list_grants_for_user", lambda sub: [
        {"provider": "serpapi", "label": "old", "daily_quota": None}])
    seams.monkeypatch.setattr(ci.credentials_store, "list_platform_credentials", lambda provider=None: [
        {"provider": "serpapi", "label": "new", "set_at": "2026-06-01"}])
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


def test_filtre_par_service_retrouve_le_compte_qui_le_porte(seams):
    """Signal 1399 : `google_ads` n'a pas de ligne de coffre, il emprunte le compte
    `google`. Filtrer sur le service retrouve ce compte (même traduction que le prêt),
    jamais une liste vide."""
    seams.vault[_member_key()] = [_row("google", "moi@exemple.test"), _row("zoho")]
    for service in ("google_ads", "gmail", "google"):
        out = _run(connector=service)["instances"]
        assert [(i["connector"], i["account"]) for i in out] == [
            ("google", "moi@exemple.test")], service


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
        {"provider": "zoho", "label": "k", "daily_quota": None}])
    seams.monkeypatch.setattr(ci.credentials_store, "list_platform_credentials", lambda provider=None: [
        {"provider": "serpapi", "label": "open", "set_at": "y"}])
    out = _run(org_id=None)
    assert {(i["level"], i["via"]) for i in out["instances"]} == \
        {("platform", "user_grant"), ("platform", "free_tier")}


# ─── 3. Garde runtime de non-déchiffrement (règle dure 1) ────────────────────

def test_projection_never_decrypts(seams, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("decrypt appelé pendant la projection")
    monkeypatch.setattr(credentials_store, "get_credential", _boom)
    monkeypatch.setattr(credentials_store, "get_credential_with_meta", _boom)
    monkeypatch.setattr(crypto, "decrypt", _boom)
    # Fixtures des 4 familles — le handler complet déroule sans lever.
    seams.vault[_member_key()] = [_row("zoho", "jane")]
    seams.monkeypatch.setattr(ci.group_store, "list_groups_for_user",
                              lambda sub, org_id=None: [{"group_id": 3, "name": "S"}])
    seams.vault[("group", "3")] = [_row("hunter")]
    seams.vault[("org", str(ORG))] = [_row("bridge", meta={"base_url": "https://b"})]
    seams.monkeypatch.setattr(ci.db, "list_grants_for_user", lambda sub: [
        {"provider": "zoho", "label": "k", "daily_quota": None}])
    seams.monkeypatch.setattr(ci.credentials_store, "list_platform_credentials", lambda provider=None: [
        {"provider": "serpapi", "label": "open", "set_at": "y"}])
    out = _run()
    assert out["count"] == 5


# ─── 4. Enregistrement de la capacité ────────────────────────────────────────

def test_capability_registered():
    from oto_mcp.capabilities.registry import CAPABILITIES
    caps = {c.key: c for c in CAPABILITIES}
    cap = caps["connectors.instances.list"]
    # ADR 0047 B1 : la face MCP est portée par la console consolidée (oto_instance
    # op=list) — cette capacité ne garde que sa face REST.
    assert cap.mcp is None
    assert caps["connectors.console.instance"].mcp == "oto_instance"
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


# ─── 6. Le compte d'autrui n'est pas listé (#1499) ───────────────────────────

def test_le_compte_d_une_autre_personne_n_est_pas_liste(seams):
    """Le prêt (`share_side`) est retiré : le compte d'une autre personne sert seulement
    NOMMÉ, au niveau de ses règles ; la liste ne le montre pas, et n'a plus de
    provenance `shared_with_me` ni `personal_cross_org`."""
    seams.vault[(credentials_store.MEMBER, credentials_store.member_id(ORG, "autre"))] = [
        _row("zoho", account="pret")]
    assert _run()["count"] == 0
    assert "visible_to" not in ci.ConnectorInstance.model_fields


# ─── 6. Lot L6 — l'identifiant stable servi à côté du ref ────────────────────
# Ce que la surface gagne : `id = "inst:{n}"`, la forme qui remplacera `ref` (et le
# `connectionId` du shell). Ce qu'elle ne perd pas : `ref`, qui reste la référence à
# repasser tant que rien ne résout un `inst:`.

def test_l6_l_identifiant_stable_est_servi_a_cote_du_ref(seams):
    seams.vault[_member_key()] = [_row("zoho", "jane")]
    seams.instances[(credentials_store.MEMBER, credentials_store.member_id(ORG, SUB),
                     "zoho", "jane")] = 412
    inst = _run()["instances"][0]
    assert inst["id"] == "inst:412"
    assert inst["ref"] == "member:8:usr_x:zoho:jane", (
        "`ref` reste servi : il est déjà distribué (bindings, arêtes de `grants`) et "
        "ce lot ne le réécrit nulle part")


def test_l6_la_cle_plateforme_a_son_identifiant_elle_aussi(seams):
    """ADR 0044 §F : une clé plateforme EST une ligne du coffre (entity_type
    'platform', entity_id = son label) — donc une instance, y compris quand elle est
    atteinte par le free-tier."""
    seams.monkeypatch.setattr(
        ci.credentials_store, "list_platform_credentials",
        lambda provider=None: [{"provider": "serpapi", "label": "env",
                                "set_at": "2026-07-01"}])
    seams.instances[("platform", "env", "serpapi", "")] = 7
    inst = _run(connector="serpapi")["instances"][0]
    assert (inst["via"], inst["id"]) == ("free_tier", "inst:7")


def test_l6_une_instance_INTROUVABLE_n_a_simplement_pas_d_id(seams):
    """Depuis la pièce 2 l'instance naît à la POSE, donc ce cas n'est plus « la clé
    est trop fraîche » — c'est une ligne de coffre que la table des instances ne
    connaît pas (un écart, que la commande de maintenance répare). La surface le rend
    par une ABSENCE de champ, pas un `null` ni une erreur : le client garde `ref`."""
    seams.vault[_member_key()] = [_row("zoho")]
    inst = _run()["instances"][0]
    assert "id" not in inst and inst["ref"]


def test_l6_l_indisponibilite_des_identifiants_ne_casse_pas_le_listing(seams, caplog):
    """Fail-open loggé, comme les sections « partagé avec moi » et « perso cross-org »
    de cette même liste : `id` n'est consommé par rien encore, faire tomber le
    listing des clés de quelqu'un pour ça serait hors de proportion."""
    def _boom(keys):
        raise RuntimeError("base indisponible")
    seams.monkeypatch.setattr(ci.db, "instances_for_vault_rows", _boom)
    seams.vault[_member_key()] = [_row("zoho")]
    with caplog.at_level(logging.WARNING):
        out = _run()
    assert out["count"] == 1 and "id" not in out["instances"][0]
    assert any("stable identifiers" in r.message for r in caplog.records)


def test_l6_la_cle_privee_du_quadruplet_ne_part_jamais_sur_le_fil(seams):
    """`ConnectorInstance` est `extra="allow"` : tout ce qui reste dans le dict est
    sérialisé. Le quadruplet de coffre est un détail d'implémentation — et nommer
    l'entité qui détient une clé n'a rien à faire dans une réponse."""
    seams.vault[_member_key()] = [_row("zoho")]
    seams.vault[("org", str(ORG))] = [_row("hunter")]
    out = ci.ConnectorInstances.model_validate(_run())
    for inst in out.instances:
        assert not [k for k in inst.model_dump().keys() if k.startswith("_")]
