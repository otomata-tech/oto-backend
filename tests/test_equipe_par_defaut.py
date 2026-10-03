"""Un membre d'équipe n'est jamais « sans équipe ».

`org_group_members.is_active` restait FALSE pour tout le monde (rien ne le posait à
l'ajout, et basculer d'org l'effaçait) ; et le front pose `X-Oto-Org` sur toute page
/org/:id, que `current_group` traitait comme « niveau org ». Résultat : `/api/me`
rendait `active_group: null`, et l'onglet Team (clés d'équipe) restait caché.
`current_group` rend désormais l'équipe du sub dans l'org résolue ; seul
`X-Oto-Group: 0` garde le niveau org.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp import access, db, group_store, org_store, session_org, tenancy


@pytest.fixture
def opt_in(monkeypatch):
    monkeypatch.setenv(access.scope.ENV_EQUIPE_PAR_DEFAUT, f"autre, {tenancy.primary_slug()}")


@pytest.fixture
def monde(live):
    u = uuid.uuid4().hex[:8]
    sub, autre = f"m_{u}", f"o_{u}"
    for s in (sub, autre):
        db.upsert_user(s, email=f"{s}@example.test")
    org = org_store.create_org(f"org_{u}", created_by=sub)
    org_b = org_store.create_org(f"orgb_{u}", created_by=sub)
    org_store.add_org_member(org, sub, "org_admin")  # maison = org (la première)
    org_store.add_org_member(org_b, sub)
    org_store.add_org_member(org, autre)
    g1 = group_store.create_group(org, f"g1_{u}")
    g2 = group_store.create_group(org, f"g2_{u}")
    gb = group_store.create_group(org_b, f"gb_{u}")
    group_store.add_group_member(g1, sub)
    group_store.add_group_member(g2, sub)
    group_store.add_group_member(gb, sub)
    return {"sub": sub, "autre": autre, "org": org, "org_b": org_b,
            "g1": g1, "g2": g2, "gb": gb}


def _pose(fn, val):
    tok = fn(val)
    return lambda: getattr(session_org, fn.__name__.replace("set_", "reset_"))(tok)


def test_sans_equipe_active_on_rend_la_premiere_rejointe(monde, opt_in):
    assert org_store.get_active_org(monde["sub"]) == monde["org"]
    assert group_store.get_active_group(monde["sub"]) is None
    assert access.current_group(monde["sub"]) == monde["g1"]


def test_l_equipe_active_prime(monde, opt_in):
    group_store.set_active_group(monde["sub"], monde["g2"])
    assert access.current_group(monde["sub"]) == monde["g2"]


def test_consultation_d_org_rend_l_equipe_de_cette_org(monde, opt_in):
    """Le front pose `X-Oto-Org` sur toute page /org/:id."""
    for org, attendu in ((monde["org"], monde["g1"]), (monde["org_b"], monde["gb"])):
        undo = _pose(session_org.set_view_org, org)
        try:
            assert access.current_group(monde["sub"]) == attendu
        finally:
            undo()


def test_jeton_org_rend_l_equipe_de_cette_org_jamais_une_autre(monde, opt_in):
    group_store.set_active_group(monde["sub"], monde["g2"])  # maison ⊂ org
    undo = _pose(session_org.set_call_org, monde["org_b"])
    try:
        assert access.current_group(monde["sub"]) == monde["gb"]
    finally:
        undo()


def test_niveau_org_explicite_reste_possible(monde, opt_in):
    undo = _pose(session_org.set_view_group, 0)
    try:
        assert access.current_group(monde["sub"]) is None
    finally:
        undo()


def test_hors_de_toute_equipe_reste_none(monde, opt_in):
    assert access.current_group(monde["autre"]) is None


def test_hors_tenant_opt_in_rien_ne_change(monde, monkeypatch):
    monkeypatch.delenv(access.scope.ENV_EQUIPE_PAR_DEFAUT, raising=False)
    assert access.current_group(monde["sub"]) is None
    undo = _pose(session_org.set_view_org, monde["org"])
    try:
        assert access.current_group(monde["sub"]) is None
    finally:
        undo()
    monkeypatch.setenv(access.scope.ENV_EQUIPE_PAR_DEFAUT, "un-autre-tenant")
    assert access.current_group(monde["sub"]) is None
