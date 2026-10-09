"""Lending a member instance to a TEAM (ADR 0044 share_side, `group:<id>`).

The loan reads as a team key: its members resolve it at the team tier, by account
name, with no pin. It stays its lender's: same org only, gone when the lender leaves
the org, pauses, or suspends the instance; the team can neither rename it nor make it
its default. All values are fictitious.
"""
import pytest

from oto_mcp import account_suspension, credentials_store, group_store, roles
from oto_mcp.access import cascade
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.connectors import sharing
from oto_mcp.connectors import identities as ci

ORG, GID, LENDER = 7, 3, "lender"


def _row(connector="zoho", account="filiale-a", org=ORG, sub=LENDER, meta=None):
    return {"entity_type": credentials_store.MEMBER, "entity_id": f"{org}:{sub}",
            "connector": connector, "account": account, "meta": meta or {}}


@pytest.fixture
def vault(monkeypatch):
    """A team of ORG, one lender still member and awake, and the shared rows."""
    state = {"shared": [_row()], "own": [], "members": {LENDER}, "paused": set()}
    monkeypatch.setattr(group_store, "get_group",
                        lambda gid: {"id": gid, "org_id": ORG} if gid == GID else None)
    monkeypatch.setattr(credentials_store, "list_shared_with",
                        lambda scopes: list(state["shared"])
                        if scopes == [f"group:{GID}"] else [])
    monkeypatch.setattr(credentials_store, "list_accounts",
                        lambda et, eid, con: [a for a in state["own"]
                                              if et == "group" and eid == str(GID)])
    monkeypatch.setattr(credentials_store, "list_credentials",
                        lambda et, eid: [{"connector": "zoho", **a} for a in state["own"]]
                        if et == "group" else [])
    monkeypatch.setattr(credentials_store, "has_credential",
                        lambda et, eid, con: any(state["own"]) and et == "group")
    monkeypatch.setattr(credentials_store, "get_credential",
                        lambda et, eid, con, account="": (
                            f"key:{et}:{eid}:{account}"
                            if (et == credentials_store.MEMBER
                                or any(a["account"] == account for a in state["own"]))
                            else None))
    monkeypatch.setattr(roles, "is_org_member",
                        lambda sub, org: sub in state["members"] and org == ORG)
    monkeypatch.setattr(account_suspension, "refus_preteur",
                        lambda sub, label: "paused" if sub in state["paused"] else None)
    return state


# ── The team tier sees the loan ──────────────────────────────────────────────

def test_a_lent_instance_reads_as_a_team_key(vault):
    assert group_store.has_group_secret(GID, "zoho")
    assert group_store.get_group_secret(GID, "zoho", "filiale-a") == \
        f"key:{credentials_store.MEMBER}:{ORG}:{LENDER}:filiale-a"
    assert group_store.get_group_secret(GID, "zoho", "absente") is None
    assert not group_store.has_group_secret(GID, "pennylane")


@pytest.mark.parametrize("change", [
    lambda s: s["shared"].__setitem__(0, _row(org=99)),            # another org
    lambda s: s["members"].discard(LENDER),                         # lender left
    lambda s: s["paused"].add(LENDER),                              # lender paused
    lambda s: s["shared"].__setitem__(0, _row(meta={"suspended": True})),
])
def test_a_lapsed_loan_serves_nothing(vault, change):
    change(vault)
    assert group_store.lent_instances(GID, "zoho") == []
    assert not group_store.has_group_secret(GID, "zoho")


def test_team_accounts_merge_own_then_lent_and_never_inherit_a_default(vault):
    vault["own"] = [{"account": "siege", "meta": {"is_default": True}, "set_at": None}]
    vault["shared"] = [_row(account="siege"), _row(account="filiale-a",
                                                   meta={"is_default": True})]
    accts = group_store.list_group_accounts(GID, "zoho")
    assert [a["account"] for a in accts] == ["siege", "filiale-a"]   # own masks the loan
    lent = accts[1]
    assert lent["meta"] == {"lent_by": LENDER}                       # no is_default


def test_a_single_lent_account_is_picked_automatically(vault):
    assert cascade._shared_auto_account("group", str(GID), "zoho", "for your team",
                                        scope="group") == "filiale-a"


def test_team_secrets_listing_names_the_lender(vault):
    out = group_store.list_group_secrets(GID)
    assert {"provider": "zoho", "account": "filiale-a", "lent_by": LENDER} in out


# ── Lending to a team ────────────────────────────────────────────────────────

@pytest.fixture
def lend(monkeypatch):
    written = {}
    monkeypatch.setattr(sharing.access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(group_store, "get_group",
                        lambda gid: {"id": gid, "org_id": ORG if gid == GID else 99})
    monkeypatch.setattr(credentials_store, "list_accounts",
                        lambda et, eid, con: [{"account": "filiale-a"}])
    monkeypatch.setattr(credentials_store, "get_instance_sharing",
                        lambda *a, **k: (None, ["user:peer"]))
    monkeypatch.setattr(credentials_store, "set_instance_sharing",
                        lambda *a, share_side=None, **k: written.update(side=share_side) or True)
    return written


CTX = ResolvedCtx(sub=LENDER, org_id=ORG)


def test_lend_to_a_team_writes_its_scope(lend):
    out = sharing._lend_instance(CTX, sharing.LendInstanceInput(
        connector="zoho", to_group=GID, account="filiale-a"))
    assert lend["side"] == ["user:peer", f"group:{GID}"]
    assert out["lent_to"] == ["peer"] and out["lent_to_groups"] == [GID]


def test_revoke_a_team_loan(lend, monkeypatch):
    monkeypatch.setattr(credentials_store, "get_instance_sharing",
                        lambda *a, **k: (None, [f"group:{GID}"]))
    out = sharing._lend_instance(CTX, sharing.LendInstanceInput(
        connector="zoho", to_group=GID, revoke=True))
    assert lend["side"] == [] and out["lent_to_groups"] == []


def test_a_loan_never_crosses_orgs(lend):
    with pytest.raises(AuthzDenied) as e:
        sharing._lend_instance(CTX, sharing.LendInstanceInput(connector="zoho", to_group=42))
    assert e.value.code == "unknown_group"


@pytest.mark.parametrize("kw", [{}, {"to": "peer", "to_group": GID}])
def test_exactly_one_borrower(lend, kw):
    with pytest.raises(AuthzDenied) as e:
        sharing._lend_instance(CTX, sharing.LendInstanceInput(connector="zoho", **kw))
    assert e.value.code == "one_borrower"


# ── oto_identity at the team tier ────────────────────────────────────────────

@pytest.fixture
def team_ctx(monkeypatch, vault):
    from oto_mcp import access
    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(access, "current_group", lambda sub: GID)
    return vault


def test_identities_list_a_lent_account_as_granted(team_ctx):
    [ident] = ci._keyed_list("member-of-team", "zoho", "group")
    assert ident["id"] == "filiale-a"
    assert ident["granted"] is True and ident["owner"] == {"sub": LENDER}
    assert ident["is_default"] is False


@pytest.mark.parametrize("call", [
    lambda: ci._keyed_select("m", "zoho", "filiale-a", "group"),
    lambda: ci.rename_identity("m", "zoho", "filiale-a", "autre", "group"),
])
def test_the_team_cannot_rename_or_default_a_lent_account(team_ctx, call):
    with pytest.raises(ValueError, match="lent to the team"):
        call()
