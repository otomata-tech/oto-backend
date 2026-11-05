"""Auto-retrait d'une org (`me.leave_org`, self-service SUB_ONLY).

Gardes du handler `_leave_org` : org inconnue → 404, non-membre → 404, sa dernière
org → 409 (plus d'org perso depuis le 05/11/2026), dernier org_admin → 409, sinon retrait. On monkeypatche `org_store` (pas de PG).
"""
from types import SimpleNamespace

import pytest

from oto_mcp.capabilities.orgs import members as om
from oto_mcp.capabilities._types import AuthzDenied


def _ctx(sub="u1"):
    return SimpleNamespace(sub=sub)


def _patch(monkeypatch, *, org=True, derniere=False, role="org_member",
           admins=2, removed=True):
    monkeypatch.setattr(om.org_store, "get_org", lambda oid: {"id": oid} if org else None)
    monkeypatch.setattr(om.org_store, "get_org_role", lambda oid, sub: role)
    monkeypatch.setattr(
        om.org_store, "list_org_members",
        lambda oid: [{"org_role": "org_admin"} for _ in range(admins)]
        + [{"org_role": "org_member"}],
    )
    calls = {}
    def _remove(oid, sub, *, actor):
        calls["removed"] = (oid, sub)
        calls["actor"] = actor
        return removed
    monkeypatch.setattr(om.org_store, "remove_org_member", _remove)
    # Après le retrait, `_rendre_un_espace` lit les orgs restantes : le compte en garde
    # une, aucun espace n'est à rendre (le cas « dernière org » est couvert par
    # `test_invite_sans_org_perso.py`). Sans cette doublure, la lecture part en base.
    # `derniere` : l'org 7 est la seule de l'appelant (garde `last_org`).
    monkeypatch.setattr(om.org_store, "list_orgs_for_user",
                        lambda sub: [{"org_id": 7}] + ([] if derniere else [{"org_id": 99}]))
    monkeypatch.setattr(om.org_store, "ensure_home_org", _jamais)
    return calls


def _jamais(*a, **k):
    raise AssertionError("aucun espace à rendre : le compte garde une org")


def test_leave_ok(monkeypatch):
    calls = _patch(monkeypatch, role="org_member")
    out = om._leave_org(_ctx("u1"), om.LeaveOrgInput(org_id=7))
    assert out == {"ok": True, "org_id": 7, "left": True}
    assert calls["removed"] == (7, "u1")
    # Le journal (oto#145) nomme la personne comme actrice de son propre départ.
    assert calls["actor"] == "u1"


def test_leave_admin_not_last_ok(monkeypatch):
    _patch(monkeypatch, role="org_admin", admins=2)
    out = om._leave_org(_ctx(), om.LeaveOrgInput(org_id=7))
    assert out["left"] is True


def test_leave_unknown_org(monkeypatch):
    _patch(monkeypatch, org=False)
    with pytest.raises(AuthzDenied) as e:
        om._leave_org(_ctx(), om.LeaveOrgInput(org_id=7))
    assert e.value.code == "unknown_org"


def test_leave_last_org_refused(monkeypatch):
    # Plus d'org perso (05/11/2026) : on ne quitte pas sa DERNIÈRE org, quelle qu'elle soit.
    calls = _patch(monkeypatch, derniere=True)
    with pytest.raises(AuthzDenied) as e:
        om._leave_org(_ctx(), om.LeaveOrgInput(org_id=7))
    assert e.value.code == "last_org"
    assert "removed" not in calls


def test_leave_the_org_created_at_signup_ok(monkeypatch):
    # L'org posée par l'inscription n'a plus de règle à part : avec une autre org, elle se quitte.
    _patch(monkeypatch, derniere=False)
    assert om._leave_org(_ctx(), om.LeaveOrgInput(org_id=7))["left"] is True


def test_leave_not_member(monkeypatch):
    _patch(monkeypatch, role=None)
    with pytest.raises(AuthzDenied) as e:
        om._leave_org(_ctx(), om.LeaveOrgInput(org_id=7))
    assert e.value.code == "not_a_member"


def test_leave_last_admin_refused(monkeypatch):
    _patch(monkeypatch, role="org_admin", admins=1)
    with pytest.raises(AuthzDenied) as e:
        om._leave_org(_ctx(), om.LeaveOrgInput(org_id=7))
    assert e.value.code == "last_org_admin"


# ── sous le jeton d'un agent hébergé (`verrou_org.py`) ───────────────────────

@pytest.fixture
def verrou():
    """Un jeton de délégation pour `u1`, verrouillé sur l'org 7 (celle de son travail)."""
    from oto_mcp import verrou_org
    t = verrou_org.poser(verrou_org.Verrou(sub="u1", org_id=7, job_id=1))
    yield
    verrou_org.lever(t)


def test_leave_refused_under_a_delegation_token(monkeypatch, verrou):
    """Quitter une org promeut une autre maison : le jeton d'un travail ne déplace pas
    le défaut de la personne — même l'org de son propre travail, et rien n'est retiré."""
    calls = _patch(monkeypatch, role="org_member")
    for org_id in (7, 8):
        with pytest.raises(AuthzDenied) as e:
            om._leave_org(_ctx("u1"), om.LeaveOrgInput(org_id=org_id))
        assert e.value.code == "org_out_of_job"
    assert "removed" not in calls


def test_leave_ok_for_someone_else_than_the_token_bearer(monkeypatch, verrou):
    """Le verrou ne vise que le porteur : un autre compte quitte normalement."""
    calls = _patch(monkeypatch, role="org_member")
    assert om._leave_org(_ctx("u2"), om.LeaveOrgInput(org_id=7))["left"] is True
    assert calls["removed"] == (7, "u2")
