"""Combinateurs d'autz de la couche capacité (`capabilities/_authz.py`).

C'est la politique d'autz **à point unique** (ADR 0009 §7) : chaque règle décide
allow/deny et injecte `org_id`/`group_id` depuis l'état serveur (jamais d'un param
client → verrou IDOR par construction). Avant ce fichier, seul `ORG_ADMIN` était
exercé directement ; les 10 autres règles n'étaient touchées que via les
monkeypatches d'autres tests — donc leur logique de décision n'était pas vérifiée.

On stubbe les seams sous-jacents (`access`, `roles`, `group_store`, `ownership`)
et on assert la DÉCISION de chaque règle (allow → ResolvedCtx attendu ; deny →
AuthzDenied avec le bon code/status). Les seams eux-mêmes sont testés ailleurs ;
ici on verrouille le câblage de la politique.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from oto_mcp import access, group_store, roles
from oto_mcp.capabilities import _authz
from oto_mcp.capabilities._types import AuthzDenied, RawCtx


@pytest.fixture(autouse=True)
def base_identity(monkeypatch):
    """Défauts neutres : user connu, org active 42, rôle member, aucun privilège.
    Chaque test relève juste ce qu'il exerce."""
    monkeypatch.setattr(access, "current_org", lambda sub: 42)
    monkeypatch.setattr(access, "get_user_role", lambda sub: "member")
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: False)
    monkeypatch.setattr(access, "is_super_admin", lambda sub: False)
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: False)
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: False)
    monkeypatch.setattr(roles, "can_read_group", lambda sub, gid: False)
    monkeypatch.setattr(roles, "can_admin_group", lambda sub, gid: False)


RAW = RawCtx(sub="u1")
ANON = RawCtx(sub=None)


def _denied(fn, raw, inp=None):
    with pytest.raises(AuthzDenied) as ei:
        fn(raw, inp)
    return ei.value


# --- SUB_ONLY --------------------------------------------------------------

def test_sub_only_allows_any_authenticated():
    ctx = _authz.SUB_ONLY(RAW)
    assert ctx.sub == "u1" and ctx.org_id == 42 and ctx.role == "member"


def test_sub_only_denies_anonymous():
    err = _denied(_authz.SUB_ONLY, ANON)
    assert err.status == 401 and err.code == "auth_required"


# --- ORG_MEMBER ------------------------------------------------------------

def test_org_member_allows_with_active_org():
    assert _authz.ORG_MEMBER(RAW).org_id == 42


def test_org_member_denies_without_active_org(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    err = _denied(_authz.ORG_MEMBER, RAW)
    assert err.status == 400 and err.code == "no_active_org"


def test_org_member_denies_anonymous():
    assert _denied(_authz.ORG_MEMBER, ANON).code == "auth_required"


# --- ORG_ADMIN (escalade super via roles.is_org_admin) ---------------------

def test_org_admin_allows_when_admin(monkeypatch):
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: True)
    assert _authz.ORG_ADMIN(RAW).org_id == 42


def test_org_admin_denies_plain_member():
    err = _denied(_authz.ORG_ADMIN, RAW)
    assert err.status == 403 and err.code == "forbidden"


def test_org_admin_denies_without_active_org(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    assert _denied(_authz.ORG_ADMIN, RAW).code == "no_active_org"


# --- PLATFORM_ADMIN / SUPER_ADMIN ------------------------------------------

def test_platform_admin_allows_operator(monkeypatch):
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: True)
    assert _authz.PLATFORM_ADMIN(RAW).sub == "u1"


def test_platform_admin_denies_non_operator():
    assert _denied(_authz.PLATFORM_ADMIN, RAW).status == 403


def test_super_admin_allows_super(monkeypatch):
    monkeypatch.setattr(access, "is_super_admin", lambda sub: True)
    assert _authz.SUPER_ADMIN(RAW).sub == "u1"


def test_super_admin_denies_platform_operator(monkeypatch):
    """Un admin plateforme (non super) ne passe PAS SUPER_ADMIN — la frontière
    admin/super_admin doit tenir."""
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: True)
    assert _denied(_authz.SUPER_ADMIN, RAW).status == 403


# --- ADMIN_BY_OP (dispatch op-aware) ---------------------------------------

def test_admin_by_op_dispatches_per_verb(monkeypatch):
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: True)
    # read → PLATFORM_ADMIN (passe pour un opérateur), write → SUPER_ADMIN (refusé).
    rule = _authz.ADMIN_BY_OP({"read": _authz.PLATFORM_ADMIN, "write": _authz.SUPER_ADMIN})
    assert rule(RAW, SimpleNamespace(op="read")).sub == "u1"
    assert _denied(rule, RAW, SimpleNamespace(op="write")).status == 403


def test_admin_by_op_unknown_op_is_refused():
    """Un op hors map = refus net, jamais fail-open."""
    rule = _authz.ADMIN_BY_OP({"read": _authz.SUB_ONLY})
    err = _denied(rule, RAW, SimpleNamespace(op="delete"))
    assert err.status == 400 and err.code == "unsupported_op"


# --- RESOURCE_GOVERN -------------------------------------------------------

def test_resource_govern_list_open_to_authenticated():
    """Les ops `list` (sans resource_id) passent pour tout authentifié — le
    handler filtre ensuite aux ressources gouvernables."""
    rule = _authz.RESOURCE_GOVERN()
    ctx = rule(RAW, SimpleNamespace(op="list", resource_type=None, resource_id=None))
    assert ctx.sub == "u1"


def test_resource_govern_allows_owner(monkeypatch):
    monkeypatch.setattr("oto_mcp.ownership.can_govern", lambda sub, t, i: True)
    rule = _authz.RESOURCE_GOVERN()
    ctx = rule(RAW, SimpleNamespace(op="transfer", resource_type="project", resource_id="7"))
    assert ctx.sub == "u1"


def test_resource_govern_denies_non_owner(monkeypatch):
    monkeypatch.setattr("oto_mcp.ownership.can_govern", lambda sub, t, i: False)
    rule = _authz.RESOURCE_GOVERN()
    err = _denied(rule, RAW, SimpleNamespace(op="transfer", resource_type="project", resource_id="7"))
    assert err.status == 403 and err.code == "forbidden"


def test_resource_govern_requires_resource_ref():
    rule = _authz.RESOURCE_GOVERN()
    err = _denied(rule, RAW, SimpleNamespace(op="get", resource_type=None, resource_id=None))
    assert err.status == 400 and err.code == "missing_resource"


# --- ORG_MEMBER_OF / ORG_ADMIN_OF (org ciblée par id de path) --------------

def test_org_member_of_reads_id_from_input(monkeypatch):
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: org == 99)
    ctx = _authz.ORG_MEMBER_OF("org_id")(RAW, SimpleNamespace(org_id=99))
    assert ctx.org_id == 99  # injecté depuis l'input, pas l'org active (42)


def test_org_member_of_denies_non_member():
    err = _denied(_authz.ORG_MEMBER_OF("org_id"), RAW, SimpleNamespace(org_id=99))
    assert err.status == 403


def test_org_member_of_missing_field(monkeypatch):
    err = _denied(_authz.ORG_MEMBER_OF("org_id"), RAW, SimpleNamespace(org_id=None))
    assert err.status == 400 and err.code == "missing_org"


def test_org_admin_of_allows_admin(monkeypatch):
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: org == 99)
    assert _authz.ORG_ADMIN_OF("org_id")(RAW, SimpleNamespace(org_id=99)).org_id == 99


def test_org_admin_of_denies_member_of_target(monkeypatch):
    """Membre de l'org cible mais pas admin → refusé (frontière lecture/écriture)."""
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    assert _denied(_authz.ORG_ADMIN_OF("org_id"), RAW, SimpleNamespace(org_id=99)).status == 403


# --- ORG_ADMIN_OPT (self-service par défaut, épinglable par `org`) ----------
# Régression otomata-private#69 : une écriture org-admin doit pouvoir cibler une
# org EXPLICITE (robuste au reset de session) sans changer de privilège.

def test_org_admin_opt_falls_back_to_active_when_absent(monkeypatch):
    """Sans `org` fourni → org active (42), garde admin sur elle (parité ORG_ADMIN)."""
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: org == 42)
    ctx = _authz.ORG_ADMIN_OPT("org")(RAW, SimpleNamespace(org=None))
    assert ctx.org_id == 42


def test_org_admin_opt_pins_explicit_org(monkeypatch):
    """`org=99` fourni → écrit sur 99, PAS l'org active (42) — robuste au reset."""
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: org == 99)
    ctx = _authz.ORG_ADMIN_OPT("org")(RAW, SimpleNamespace(org=99))
    assert ctx.org_id == 99


def test_org_admin_opt_denies_explicit_org_not_admin(monkeypatch):
    """Org explicite dont on n'est pas admin → refusé (aucune escalade cross-org)."""
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: org == 42)  # admin de l'active seulement
    err = _denied(_authz.ORG_ADMIN_OPT("org"), RAW, SimpleNamespace(org=99))
    assert err.status == 403 and err.code == "forbidden"


def test_org_admin_opt_denies_plain_member_on_active():
    err = _denied(_authz.ORG_ADMIN_OPT("org"), RAW, SimpleNamespace(org=None))
    assert err.status == 403 and err.code == "forbidden"


def test_org_admin_opt_denies_without_active_org(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    assert _denied(_authz.ORG_ADMIN_OPT("org"), RAW, SimpleNamespace(org=None)).code == "no_active_org"


# --- GROUP_MEMBER_OF / GROUP_ADMIN_OF --------------------------------------

def test_group_member_of_injects_parent_org(monkeypatch):
    monkeypatch.setattr(group_store, "get_group", lambda gid: {"org_id": 7})
    monkeypatch.setattr(roles, "can_read_group", lambda sub, gid: True)
    ctx = _authz.GROUP_MEMBER_OF("group_id")(RAW, SimpleNamespace(group_id=3))
    assert ctx.group_id == 3 and ctx.org_id == 7  # org parent dérivée du groupe


def test_group_member_of_unknown_group(monkeypatch):
    monkeypatch.setattr(group_store, "get_group", lambda gid: None)
    err = _denied(_authz.GROUP_MEMBER_OF("group_id"), RAW, SimpleNamespace(group_id=3))
    assert err.status == 404 and err.code == "unknown_group"


def test_group_member_of_denies_outsider(monkeypatch):
    monkeypatch.setattr(group_store, "get_group", lambda gid: {"org_id": 7})
    err = _denied(_authz.GROUP_MEMBER_OF("group_id"), RAW, SimpleNamespace(group_id=3))
    assert err.status == 403


def test_group_admin_of_allows_chief(monkeypatch):
    monkeypatch.setattr(group_store, "get_group", lambda gid: {"org_id": 7})
    monkeypatch.setattr(roles, "can_admin_group", lambda sub, gid: True)
    assert _authz.GROUP_ADMIN_OF("group_id")(RAW, SimpleNamespace(group_id=3)).group_id == 3


def test_group_admin_of_denies_plain_member(monkeypatch):
    monkeypatch.setattr(group_store, "get_group", lambda gid: {"org_id": 7})
    monkeypatch.setattr(roles, "can_read_group", lambda sub, gid: True)  # membre, pas chef
    assert _denied(_authz.GROUP_ADMIN_OF("group_id"), RAW, SimpleNamespace(group_id=3)).status == 403
