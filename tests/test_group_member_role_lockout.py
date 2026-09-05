"""Anti-lockout de l'écriture de rôle d'ÉQUIPE : les deux routes, le même verdict (#280).

`group_store.add_group_member` est un upsert — « ajouter » un membre déjà présent écrase
son rôle. `POST /api/groups/{id}/members` (`group.member.add`) et
`POST /api/groups/{id}/members/{sub}` (`group.member.set_role`) écrivent donc la même
ligne, mais seule la seconde portait une garde : on rétrogradait le dernier chef par la
première. Même forme qu'#273 au palier org, un palier plus bas.

**La garde a changé de critère en même temps**, et c'est le cœur de ce fichier : la
garantie n'est pas « l'équipe garde un chef explicite » mais **« une équipe a toujours
quelqu'un qui peut l'administrer, le responsable d'organisation compris »**. Un `org_admin`
administre toutes les équipes de son org (`roles.can_admin_group`) — donc rétrograder le
dernier chef est AUTORISÉ (`test_org_admin_can_demote_the_last_chief`), et seul l'état
« plus personne » est refusé (`test_…_without_an_org_admin_is_refused`).

**Chaque cas est joué sur les deux chemins.** Un test qui n'exerce que `set_role` passe au
vert en laissant le trou ouvert — c'est précisément ce qui l'avait rendu invisible côté org.
On monkeypatche les stores (pas de PG).
"""
from types import SimpleNamespace

import pytest

from oto_mcp.capabilities.groups import members as gm
from oto_mcp.capabilities._types import AuthzDenied

ORG_ID = 3
GROUP_ID = 42


def _ctx(sub="caller"):
    # `org_id` = org parente, injectée par GROUP_ADMIN_OF (jamais un param client).
    return SimpleNamespace(sub=sub, org_id=ORG_ID, group_id=GROUP_ID)


def _patch(monkeypatch, *, role="group_admin", chiefs=1, org_admins=1):
    """`role` = rôle actuel de la cible dans l'équipe (None = non-membre),
    `chiefs` = chefs explicites de l'équipe, `org_admins` = responsables de l'org parente.
    """
    monkeypatch.setattr(gm.group_store, "get_group_role", lambda gid, sub: role)
    monkeypatch.setattr(gm.group_store, "count_group_admins", lambda gid: chiefs)
    # L'invariant « déjà membre de l'org parente » est satisfait par défaut.
    monkeypatch.setattr(gm.org_store, "get_org_role", lambda oid, sub: "org_member")
    monkeypatch.setattr(
        gm.org_store, "list_org_members",
        lambda oid: [{"org_role": "org_admin"} for _ in range(org_admins)]
        + [{"org_role": "org_member"}],
    )
    written = []
    monkeypatch.setattr(gm.group_store, "add_group_member",
                        lambda gid, sub, r: written.append((gid, sub, r)))
    monkeypatch.setattr(gm.group_store, "remove_group_member", lambda gid, sub: True)
    return written


# Les deux chemins d'écriture de rôle, appelés avec la MÊME intention métier :
# « donner à `sub` le rôle `role` dans l'équipe `group_id` ».
def _via_add(group_id, sub, role):
    return gm._add_member(_ctx(), gm.AddGroupMemberInput(group_id=group_id, target=sub,
                                                         role=role))


def _via_set_role(group_id, sub, role):
    return gm._set_member_role(_ctx(), gm.SetGroupMemberRoleInput(group_id=group_id,
                                                                  sub=sub, role=role))


BOTH = pytest.mark.parametrize("write_role", [_via_add, _via_set_role],
                               ids=["POST /members", "POST /members/{sub}"])


# ── La garantie révisée : l'org_admin administre l'équipe ─────────────────────

@BOTH
def test_org_admin_can_demote_the_last_chief(monkeypatch, write_role):
    """LE cas qui prouve le changement de CRITÈRE, pas un simple déplacement de code :
    l'équipe perd son dernier chef explicite et l'écriture PASSE, parce que le
    responsable de l'org l'administre. L'ancienne garde `last_group_admin` refusait ici."""
    written = _patch(monkeypatch, role="group_admin", chiefs=1, org_admins=1)
    out = write_role(GROUP_ID, "chief", "group_member")
    assert out == {"ok": True, "group_id": GROUP_ID, "sub": "chief", "role": "group_member"}
    assert written == [(GROUP_ID, "chief", "group_member")]


@BOTH
def test_demoting_the_last_chief_without_an_org_admin_is_refused(monkeypatch, write_role):
    """L'état vraiment mort : zéro chef explicite après l'écriture ET aucun responsable
    dans l'org (atteignable — `oto_admin_org op=create` crée une org sans membre)."""
    written = _patch(monkeypatch, role="group_admin", chiefs=1, org_admins=0)
    with pytest.raises(AuthzDenied) as e:
        write_role(GROUP_ID, "chief", "group_member")
    assert e.value.status == 409
    assert e.value.code == "group_unadministrable"
    assert written == []                       # rien n'a été écrit : l'équipe reste gérable


@BOTH
def test_another_chief_remains_without_an_org_admin(monkeypatch, write_role):
    """Sans responsable d'org, un second chef explicite suffit : la garde ne mord que sur
    le dernier."""
    written = _patch(monkeypatch, role="group_admin", chiefs=2, org_admins=0)
    out = write_role(GROUP_ID, "chief", "group_member")
    assert out["role"] == "group_member"
    assert written == [(GROUP_ID, "chief", "group_member")]


@BOTH
def test_rewriting_the_last_chief_as_chief_is_allowed(monkeypatch, write_role):
    """Ré-écrire `group_admin` sur le dernier chef n'enlève rien — c'est un no-op, pas un
    verrouillage. Refuser ici casserait l'idempotence de la ré-invitation."""
    written = _patch(monkeypatch, role="group_admin", chiefs=1, org_admins=0)
    out = write_role(GROUP_ID, "chief", "group_admin")
    assert out["role"] == "group_admin"
    assert written == [(GROUP_ID, "chief", "group_admin")]


@BOTH
def test_writing_a_plain_member_is_untouched(monkeypatch, write_role):
    """La garde regarde le rôle ACTUEL de la cible : un simple membre n'est jamais
    concerné, même dans une équipe sans chef et une org sans responsable."""
    written = _patch(monkeypatch, role="group_member", chiefs=0, org_admins=0)
    out = write_role(GROUP_ID, "someone", "group_member")
    assert out["ok"] is True
    assert written == [(GROUP_ID, "someone", "group_member")]


# ── Les divergences LÉGITIMES entre les deux routes ──────────────────────────

def test_add_accepts_a_non_member(monkeypatch):
    """`add` sur un non-membre de l'équipe est son objet même (pas de 404) — et la garde
    ne s'y déclenche pas : on ne rétrograde personne en ajoutant quelqu'un."""
    written = _patch(monkeypatch, role=None, chiefs=1, org_admins=0)
    out = gm._add_member(_ctx(), gm.AddGroupMemberInput(group_id=GROUP_ID, target="newbie",
                                                        role="group_member"))
    assert out["sub"] == "newbie"
    assert written == [(GROUP_ID, "newbie", "group_member")]


def test_set_role_refuses_a_non_member(monkeypatch):
    """`set_role` exige l'appartenance : 404, jamais une adhésion implicite."""
    written = _patch(monkeypatch, role=None, chiefs=1, org_admins=1)
    with pytest.raises(AuthzDenied) as e:
        gm._set_member_role(_ctx(), gm.SetGroupMemberRoleInput(group_id=GROUP_ID,
                                                               sub="ghost",
                                                               role="group_admin"))
    assert e.value.code == "not_a_member"
    assert written == []


def test_add_refuses_someone_outside_the_parent_org(monkeypatch):
    """L'invariant groupe⊂org reste porté par `add` (la 404 de `set_role` le subsume)."""
    written = _patch(monkeypatch, role=None, chiefs=1, org_admins=1)
    monkeypatch.setattr(gm.org_store, "get_org_role", lambda oid, sub: None)
    with pytest.raises(AuthzDenied) as e:
        gm._add_member(_ctx(), gm.AddGroupMemberInput(group_id=GROUP_ID, target="outsider",
                                                      role="group_member"))
    assert e.value.code == "not_org_member"
    assert written == []


def test_add_resolves_an_email_before_guarding(monkeypatch):
    """`add` accepte un email : la garde doit voir le sub RÉSOLU, pas la chaîne saisie —
    sinon elle interroge le rôle d'une cible qui n'existe pas et laisse tout passer."""
    written = _patch(monkeypatch, role="group_admin", chiefs=1, org_admins=0)
    # L'ancrage a bougé : la résolution vit dans `_identite` (domicile unique de
    # la garde d'homonymie). Ce que ce test garde est INCHANGÉ — la garde doit
    # voir le sub résolu — seule la porte d'entrée de l'annuaire a changé.
    monkeypatch.setattr(gm._identite.db, "get_users_by_email", lambda e: [{"sub": "chief"}])
    with pytest.raises(AuthzDenied) as e:
        gm._add_member(_ctx(), gm.AddGroupMemberInput(group_id=GROUP_ID,
                                                      target="chief@x.tld",
                                                      role="group_member"))
    assert e.value.code == "group_unadministrable"
    assert written == []


# ── Le retrait porte la MÊME garde (sinon elle est contournable en deux appels) ──

def _via_remove(group_id, sub):
    return gm._remove_member(_ctx(), gm.RemoveGroupMemberInput(group_id=group_id,
                                                               target=sub))


def test_removing_the_last_chief_is_allowed_when_the_org_has_an_admin(monkeypatch):
    _patch(monkeypatch, role="group_admin", chiefs=1, org_admins=1)
    out = _via_remove(GROUP_ID, "chief")
    assert out == {"ok": True, "group_id": GROUP_ID, "sub": "chief", "removed": True}


def test_removing_the_last_chief_without_an_org_admin_is_refused(monkeypatch):
    """Même critère que la rétrogradation : sans quoi le retrait serait le chemin strict
    d'une garde que la rétrogradation autorise — donc contournable (rétrograder, retirer)."""
    _patch(monkeypatch, role="group_admin", chiefs=1, org_admins=0)
    removed = []
    monkeypatch.setattr(gm.group_store, "remove_group_member",
                        lambda gid, sub: removed.append(sub) or True)
    with pytest.raises(AuthzDenied) as e:
        _via_remove(GROUP_ID, "chief")
    assert e.value.code == "group_unadministrable"
    assert removed == []
