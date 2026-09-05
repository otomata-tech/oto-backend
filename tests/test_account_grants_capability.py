"""#55 — capacité `connectors.account_grants.{list,grant,revoke}` : le propriétaire
(et lui seul, owner := ctx.sub par construction) accorde/révoque l'opération de son
compte à N'IMPORTE QUEL user oto, **y compris hors de ses orgs** (cross-org assumé),
OU à un groupe entier (`grantee="group:<id>"`, fan-out dynamique sur ses membres
actuels). Deny-by-default, audité."""
import pytest

from oto_mcp.capabilities.connectors import account_grants as cap
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


_OWNER = ResolvedCtx(sub="owner", org_id=3, role="member")


def _wire(monkeypatch, *, users=None, connected="OWNER_ACC"):
    users = users or {"grantee": {"sub": "grantee", "email": "g@x.io"}}
    monkeypatch.setattr(cap.db, "get_user", lambda sub: users.get(sub))
    # ⚠️ Depuis le 05/09 la résolution lit TOUS les porteurs d'une adresse : une
    # adresse peut en désigner deux, et en choisir un en silence était le défaut.
    monkeypatch.setattr(cap.db, "get_users_by_email",
                        lambda email: [u for u in users.values()
                                       if u.get("email") == email])
    monkeypatch.setattr(cap.db, "get_unipile_account_id", lambda sub, org, prov: connected)


def test_grant_ok_by_sub(monkeypatch):
    _wire(monkeypatch)
    saved = {}
    monkeypatch.setattr(cap.db, "set_account_grant",
                        lambda owner, prov, aid, grantee, granted_by:
                        saved.update(owner=owner, prov=prov, aid=aid,
                                     grantee=grantee, by=granted_by))
    res = cap._grant(_OWNER, cap.AccountGrantInput(channel="linkedin", grantee="grantee"))
    assert res["ok"] and res["account_id"] == "OWNER_ACC"
    # owner = ctx.sub, JAMAIS un param client (verrou anti-IDOR par construction).
    assert saved == {"owner": "owner", "prov": "LINKEDIN", "aid": "OWNER_ACC",
                     "grantee": "grantee", "by": "owner"}


def test_grant_resolves_grantee_by_email(monkeypatch):
    _wire(monkeypatch)
    saved = {}
    monkeypatch.setattr(cap.db, "set_account_grant",
                        lambda owner, prov, aid, grantee, granted_by:
                        saved.update(grantee=grantee))
    res = cap._grant(_OWNER, cap.AccountGrantInput(channel="linkedin", grantee="g@x.io"))
    assert res["grantee_sub"] == "grantee" and saved["grantee"] == "grantee"


def test_grant_rejects_unknown_user(monkeypatch):
    _wire(monkeypatch, users={})
    with pytest.raises(AuthzDenied) as e:
        cap._grant(_OWNER, cap.AccountGrantInput(channel="linkedin", grantee="ghost"))
    assert e.value.code == "unknown_user" and e.value.status == 404


def test_grant_rejects_self(monkeypatch):
    _wire(monkeypatch, users={"owner": {"sub": "owner", "email": "o@x.io"}})
    with pytest.raises(AuthzDenied) as e:
        cap._grant(_OWNER, cap.AccountGrantInput(channel="linkedin", grantee="owner"))
    assert e.value.code == "self_grant"


def test_grant_allows_cross_org_grantee(monkeypatch):
    # Cross-org assumé : un grantee sans AUCUNE org commune est accepté (on partage
    # son propre compte à qui on veut — freelance externe).
    _wire(monkeypatch, users={"ext": {"sub": "ext", "email": "freelance@ext.io"}})
    saved = {}
    monkeypatch.setattr(cap.db, "set_account_grant",
                        lambda owner, prov, aid, grantee, granted_by:
                        saved.update(grantee=grantee))
    res = cap._grant(_OWNER, cap.AccountGrantInput(channel="linkedin",
                                                   grantee="freelance@ext.io"))
    assert res["ok"] and saved["grantee"] == "ext"


def test_grant_rejects_unconnected_channel(monkeypatch):
    _wire(monkeypatch, connected=None)
    with pytest.raises(AuthzDenied) as e:
        cap._grant(_OWNER, cap.AccountGrantInput(channel="whatsapp", grantee="grantee"))
    assert e.value.code == "channel_not_connected" and e.value.status == 404


def test_revoke_idempotent_and_clears_pointer(monkeypatch):
    calls = {}
    monkeypatch.setattr(cap.db, "clear_account_grant",
                        lambda owner, prov, grantee:
                        calls.update(grant=(owner, prov, grantee)) or False)
    monkeypatch.setattr(cap.db, "clear_operated_pointers_to",
                        lambda owner, prov, grantee:
                        calls.update(pointer=(owner, prov, grantee)))
    res = cap._revoke(_OWNER, cap.AccountGrantInput(channel="linkedin", grantee="grantee"))
    assert res["ok"] and res["revoked"] is False  # idempotent : déjà absent
    assert calls["grant"] == ("owner", "LINKEDIN", "grantee")
    assert calls["pointer"] == ("owner", "LINKEDIN", "grantee")


def test_revoke_resolves_email(monkeypatch):
    monkeypatch.setattr(cap.db, "get_users_by_email",
                        lambda email: [{"sub": "grantee", "email": email}])
    seen = {}
    monkeypatch.setattr(cap.db, "clear_account_grant",
                        lambda owner, prov, grantee: seen.update(g=grantee) or True)
    monkeypatch.setattr(cap.db, "clear_operated_pointers_to", lambda *a: None)
    res = cap._revoke(_OWNER, cap.AccountGrantInput(channel="linkedin", grantee="g@x.io"))
    assert res["revoked"] is True and seen["g"] == "grantee"


def test_list_shapes(monkeypatch):
    monkeypatch.setattr(cap.db, "list_account_grants_by_owner",
                        lambda sub: [{"provider": "LINKEDIN", "grantee_sub": "grantee"}])
    monkeypatch.setattr(cap.db, "list_account_group_grants_by_owner", lambda sub: [])
    monkeypatch.setattr(cap.db, "list_account_grants_to",
                        lambda sub: [{"provider": "LINKEDIN", "owner_sub": "boss"}])
    res = cap._list(_OWNER, cap.AccountGrantsListInput())
    assert res["granted_by_me"][0]["grantee_sub"] == "grantee"
    assert res["granted_to_me"][0]["owner_sub"] == "boss"


def test_list_merges_nominative_and_group_grants_by_owner(monkeypatch):
    monkeypatch.setattr(cap.db, "list_account_grants_by_owner",
                        lambda sub: [{"provider": "LINKEDIN", "grantee_sub": "grantee"}])
    monkeypatch.setattr(cap.db, "list_account_group_grants_by_owner",
                        lambda sub: [{"provider": "LINKEDIN", "grantee_group_id": 42}])
    monkeypatch.setattr(cap.db, "list_account_grants_to", lambda sub: [])
    res = cap._list(_OWNER, cap.AccountGrantsListInput())
    assert len(res["granted_by_me"]) == 2
    assert res["granted_by_me"][1]["grantee_group_id"] == 42


# --- Cible groupe (extension #55 / ADR 0051) ---------------------------------

def _wire_group(monkeypatch, *, connected="OWNER_ACC", group=None):
    monkeypatch.setattr(cap.db, "get_unipile_account_id", lambda sub, org, prov: connected)
    monkeypatch.setattr(cap.group_store, "get_group",
                        lambda gid: group if group is not None
                        else {"id": gid, "name": "Croissance", "org_id": 3})


def test_parse_group_target():
    assert cap._parse_group_target("group:42") == 42
    assert cap._parse_group_target("grantee") is None
    assert cap._parse_group_target("g@x.io") is None


def test_parse_group_target_rejects_malformed_id():
    with pytest.raises(AuthzDenied) as e:
        cap._parse_group_target("group:abc")
    assert e.value.code == "invalid_group_target" and e.value.status == 400


def test_grant_to_group(monkeypatch):
    _wire_group(monkeypatch)
    saved = {}
    monkeypatch.setattr(cap.db, "set_account_group_grant",
                        lambda owner, prov, aid, gid, granted_by:
                        saved.update(owner=owner, prov=prov, aid=aid, gid=gid,
                                     by=granted_by))
    res = cap._grant(_OWNER, cap.AccountGrantInput(channel="linkedin", grantee="group:42"))
    assert res["ok"] and res["grantee_group_id"] == 42
    assert res["grantee_group_name"] == "Croissance"
    assert saved == {"owner": "owner", "prov": "LINKEDIN", "aid": "OWNER_ACC",
                     "gid": 42, "by": "owner"}


def test_grant_to_unknown_group_rejected(monkeypatch):
    _wire_group(monkeypatch, group=False)
    with pytest.raises(AuthzDenied) as e:
        cap._grant(_OWNER, cap.AccountGrantInput(channel="linkedin", grantee="group:999"))
    assert e.value.code == "unknown_group" and e.value.status == 404


def test_grant_to_group_rejects_unconnected_channel(monkeypatch):
    _wire_group(monkeypatch, connected=None)
    with pytest.raises(AuthzDenied) as e:
        cap._grant(_OWNER, cap.AccountGrantInput(channel="whatsapp", grantee="group:42"))
    assert e.value.code == "channel_not_connected"


def test_revoke_group_clears_grant_and_member_pointers(monkeypatch):
    calls = {}
    monkeypatch.setattr(cap.db, "clear_account_group_grant",
                        lambda owner, prov, gid: calls.update(grant=(owner, prov, gid)) or True)
    monkeypatch.setattr(cap.db, "clear_operated_pointers_to_group",
                        lambda owner, prov, gid: calls.update(pointer=(owner, prov, gid)))
    res = cap._revoke(_OWNER, cap.AccountGrantInput(channel="linkedin", grantee="group:42"))
    assert res["ok"] and res["revoked"] is True and res["grantee_group_id"] == 42
    assert calls["grant"] == ("owner", "LINKEDIN", 42)
    assert calls["pointer"] == ("owner", "LINKEDIN", 42)
