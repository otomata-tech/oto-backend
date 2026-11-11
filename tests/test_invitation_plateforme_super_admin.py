"""Invitation plateforme : porter une org est réservé au super admin.

Une invitation plateforme sans org (onboarding pur) reste ouverte à l'admin
plateforme. Dès qu'elle porte une org (`org_id`), son acceptation fait entrer
l'invité dans cette org avec le rôle choisi : c'est une écriture sur une org tierce,
que seul le super admin fait (`_authz.PLATFORM_INVITE`).

Figé ici, sur les deux faces (REST `platform.invite.create`, console MCP
`admin.invite` op=create) :
- un admin plateforme simple est refusé dès que `org_id` est fourni, quel que soit
  le rôle ;
- le super admin passe ;
- sans org, l'admin plateforme simple passe.

Et à l'acceptation (contre PostgreSQL) : une invitation plateforme qui porte une org
sans avoir été émise par un super admin n'est honorée par aucun chemin.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from oto_mcp import access
from oto_mcp.capabilities import registry
from oto_mcp.capabilities._types import AuthzDenied, RawCtx

RAW = RawCtx(sub="u-op")


def _cap(key: str):
    return next(c for c in registry.CAPABILITIES if c.key == key)


def _entree(key: str, **champs):
    cap = _cap(key)
    if key == "admin.invite":
        champs.setdefault("op", "create")
    return cap, cap.Input(**champs)


@pytest.fixture
def operateur(monkeypatch):
    """Un admin plateforme qui n'est pas super admin."""
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: True)
    monkeypatch.setattr(access, "is_super_admin", lambda sub: False)
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    monkeypatch.setattr(access, "get_user_role", lambda sub: "admin")


@pytest.fixture
def super_admin(monkeypatch):
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: True)
    monkeypatch.setattr(access, "is_super_admin", lambda sub: True)
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    monkeypatch.setattr(access, "get_user_role", lambda sub: "super_admin")


FACES = ["platform.invite.create", "admin.invite"]


@pytest.mark.parametrize("key", FACES)
@pytest.mark.parametrize("role", [None, "org_member", "org_admin"])
def test_operateur_refuse_des_qu_une_org_est_portee(operateur, key, role):
    champs = {"org_id": 42, "send_email": False}
    if role:
        champs["role"] = role
    cap, inp = _entree(key, **champs)
    with pytest.raises(AuthzDenied) as ei:
        cap.authz(RAW, inp)
    assert ei.value.status == 403 and ei.value.code == "forbidden"


@pytest.mark.parametrize("key", FACES)
@pytest.mark.parametrize("role", ["org_member", "org_admin"])
def test_super_admin_passe_avec_une_org(super_admin, key, role):
    cap, inp = _entree(key, org_id=42, role=role, send_email=False)
    assert cap.authz(RAW, inp).sub == "u-op"


@pytest.mark.parametrize("key", FACES)
def test_operateur_passe_sans_org(operateur, key):
    cap, inp = _entree(key, send_email=False)
    assert cap.authz(RAW, inp).sub == "u-op"


@pytest.mark.parametrize("op", ["list", "revoke"])
def test_console_lecture_et_revocation_restent_a_l_operateur(operateur, op):
    cap = _cap("admin.invite")
    assert cap.authz(RAW, cap.Input(op=op, invite_id=1)).sub == "u-op"


# --- Acceptation (PostgreSQL) -------------------------------------------------

def _org(nom: str) -> int:
    from oto_mcp import org_store
    oid = org_store.create_org(nom, created_by="u-pi-owner")
    org_store.add_org_member(oid, "u-pi-owner", "org_admin")
    return oid


def _accepter(sub: str, token: str) -> dict:
    from oto_mcp.capabilities.orgs import invites as cap
    return cap._invite_accept(SimpleNamespace(sub=sub), cap.InviteAcceptInput(token=token))


def test_invitation_avec_org_emise_sans_droit_ne_s_accepte_pas(live):
    from oto_mcp import org_store
    from oto_mcp.db import upsert_user
    sub = "u-pi-operateur"
    upsert_user(sub, email=f"{sub}@x.tld")
    org = _org("Org cible plateforme")
    _id, token = org_store.create_invitation(org, None, "org_admin", sub,
                                             source="platform_admin")
    with pytest.raises(AuthzDenied) as ei:
        _accepter(sub, token)
    assert ei.value.status == 403 and ei.value.code == "platform_invite_not_allowed"
    # Le store la refuse aussi, sans passer par la capacité : rien n'est écrit.
    assert org_store.accept_invitation(token, sub) is None
    assert org_store.get_org_role(org, sub) is None


def test_invitation_avec_org_emise_sans_droit_ignoree_par_l_adresse(live):
    from oto_mcp import org_store
    from oto_mcp.db import upsert_user
    sub = "u-pi-invite-adresse"
    email = f"{sub}@x.tld"
    upsert_user(sub, email=email)
    org = _org("Org cible adresse")
    org_store.create_invitation(org, email, "org_admin", "u-pi-autre-operateur",
                                source="platform_admin")
    assert org_store.claim_invitation_for_org(sub, email, org) is None
    assert org_store.reconcile_signup_with_invitation(sub, email) is None
    assert org_store.get_org_role(org, sub) is None


def test_invitation_avec_org_emise_par_le_super_admin_s_accepte(live, monkeypatch):
    from oto_mcp import org_store
    from oto_mcp.db import upsert_user
    monkeypatch.setenv("OTO_MCP_ADMIN_SUB", "u-pi-super")
    sub = "u-pi-invite-ok"
    upsert_user(sub, email=f"{sub}@x.tld")
    org = _org("Org cible super")
    _id, token = org_store.create_invitation(org, None, "org_admin", "u-pi-super",
                                             source="platform_admin")
    res = _accepter(sub, token)
    assert res["ok"] and res["org_id"] == org
    assert org_store.get_org_role(org, sub) == "org_admin"


def test_invitation_plateforme_sans_org_s_accepte(live):
    from oto_mcp import org_store
    from oto_mcp.db import upsert_user
    sub = "u-pi-onboarding"
    upsert_user(sub, email=f"{sub}@x.tld")
    _id, token = org_store.create_invitation(None, None, "org_member", "u-pi-operateur",
                                             source="platform_admin")
    assert _accepter(sub, token)["ok"]
