"""#55 — capacité `connectors.account_grants.{list,grant,revoke}` : le propriétaire
(et lui seul, owner := ctx.sub par construction) accorde/révoque l'opération de son
compte à un membre nommé d'une org commune. Deny-by-default, audité."""
import pytest

from oto_mcp.capabilities import connectors_account_grants as cap
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


_OWNER = ResolvedCtx(sub="owner", org_id=3, role="member")


def _wire(monkeypatch, *, users=None, share=True, connected="OWNER_ACC"):
    users = users or {"grantee": {"sub": "grantee", "email": "g@x.io"}}
    monkeypatch.setattr(cap.db, "get_user", lambda sub: users.get(sub))
    monkeypatch.setattr(cap.db, "get_user_by_email",
                        lambda email: next((u for u in users.values()
                                            if u.get("email") == email), None))
    monkeypatch.setattr(cap.db, "users_share_org", lambda a, b: share)
    monkeypatch.setattr(cap.db, "get_unipile_account_id", lambda sub, prov: connected)


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


def test_grant_rejects_grantee_without_shared_org(monkeypatch):
    _wire(monkeypatch, share=False)
    with pytest.raises(AuthzDenied) as e:
        cap._grant(_OWNER, cap.AccountGrantInput(channel="linkedin", grantee="grantee"))
    assert e.value.code == "not_in_shared_org" and e.value.status == 400


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
    monkeypatch.setattr(cap.db, "get_user_by_email",
                        lambda email: {"sub": "grantee", "email": email})
    seen = {}
    monkeypatch.setattr(cap.db, "clear_account_grant",
                        lambda owner, prov, grantee: seen.update(g=grantee) or True)
    monkeypatch.setattr(cap.db, "clear_operated_pointers_to", lambda *a: None)
    res = cap._revoke(_OWNER, cap.AccountGrantInput(channel="linkedin", grantee="g@x.io"))
    assert res["revoked"] is True and seen["g"] == "grantee"


def test_list_shapes(monkeypatch):
    monkeypatch.setattr(cap.db, "list_account_grants_by_owner",
                        lambda sub: [{"provider": "LINKEDIN", "grantee_sub": "grantee"}])
    monkeypatch.setattr(cap.db, "list_account_grants_to",
                        lambda sub: [{"provider": "LINKEDIN", "owner_sub": "boss"}])
    res = cap._list(_OWNER, cap.AccountGrantsListInput())
    assert res["granted_by_me"][0]["grantee_sub"] == "grantee"
    assert res["granted_to_me"][0]["owner_sub"] == "boss"
