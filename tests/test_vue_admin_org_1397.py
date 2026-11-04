"""Ce qu'un admin d'org voit de son org, depuis un agent (signal 1397).

`oto_group op=get` (membres + comptes de l'équipe) et `op=list_invites` sur `oto_org` et
`oto_group` : elles branchent les handlers que sert déjà la console. `oto_org
op=list_loans` est retirée par la bascule des comptes (#1499) : les règles d'un compte se
lisent par `oto_instance op=rules`. Les autorisations restent celles du serveur. Toutes
les valeurs sont fictives.
"""
import pytest

from oto_mcp.capabilities import org_console
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx
from oto_mcp.capabilities.registry import CAPABILITIES

ORG = 7
CTX = ResolvedCtx(sub="admin", org_id=ORG)


def _cap(key):
    return next(c for c in CAPABILITIES if c.key == key)


@pytest.mark.parametrize("cle,inp,seam", [
    ("org.console", org_console.OrgInput(op="list_invites", org_id=ORG), "is_org_admin"),
    ("group.console", org_console.GroupInput(op="list_invites", group_id=3), "can_admin_group"),
    ("group.console", org_console.GroupInput(op="get", group_id=3), "can_read_group"),
])
def test_les_ops_neuves_sont_gardees_cote_serveur(monkeypatch, cle, inp, seam):
    """Les invitations de l'org = l'administrer ; celles d'une équipe = la
    mener ; lire une équipe = en être membre (un admin d'org y passe par l'escalade).
    Les mêmes règles que les capacités que la console appelle déjà."""
    from oto_mcp import roles
    from oto_mcp.capabilities import _authz
    monkeypatch.setattr(roles, seam, lambda *a, **k: False)
    # Le refus nomme les admins à joindre (lecture de base) : seule sa forme compte ici.
    for refus_nomme in ("_refus_org_admin", "_refus_chef_d_equipe"):
        monkeypatch.setattr(_authz, refus_nomme,
                            lambda *a, **k: AuthzDenied(403, "forbidden", "refus"))
    with pytest.raises(AuthzDenied) as refus:
        _cap(cle).authz(RawCtx(sub="intrus"), inp)
    assert refus.value.status == 403


def test_group_get_et_list_invites_branchent_les_handlers_de_la_console(monkeypatch):
    vus = []
    monkeypatch.setattr(org_console.groups, "_group_detail",
                        lambda ctx, inp: vus.append(("get", inp.group_id)) or {"ok": 1})
    monkeypatch.setattr(org_console.groups_invites, "_invite_list",
                        lambda ctx, inp: vus.append(("g_inv", inp.group_id)) or {"ok": 2})
    monkeypatch.setattr(org_console.orgs_invites, "_invite_list",
                        lambda ctx, inp: vus.append(("o_inv", inp.org_id)) or {"ok": 3})
    assert org_console._group(CTX, org_console.GroupInput(op="get", group_id=3)) == {"ok": 1}
    assert org_console._group(CTX, org_console.GroupInput(op="list_invites", group_id=3)) == {"ok": 2}
    assert org_console._org(CTX, org_console.OrgInput(op="list_invites", org_id=ORG)) == {"ok": 3}
    assert vus == [("get", 3), ("g_inv", 3), ("o_inv", ORG)]


def test_list_loans_est_retiree_sans_alias():
    assert "list_loans" not in org_console.OrgInput.model_fields["op"].annotation.__args__
    assert not any(c.key == "org.loans.list" for c in CAPABILITIES)
