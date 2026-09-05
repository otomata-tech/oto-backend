"""Anti-lockout de l'écriture de rôle : les DEUX routes, le même refus (#273).

`org_store.add_org_member` est un upsert — « ajouter » un membre déjà présent écrase
son rôle. `POST /api/orgs/{id}/members` (`org.member.add`) et
`POST /api/orgs/{id}/members/{sub}` (`org.member.set_role`) écrivent donc la même
ligne, mais seule la seconde refusait de rétrograder le dernier org_admin : on
verrouillait une org hors de son administration par la première.

**Chaque cas est joué sur les deux chemins.** Un test qui n'exerce que `set_role`
passe au vert en laissant le trou ouvert — c'est précisément ce qui l'avait rendu
invisible. On monkeypatche `org_store` (pas de PG).
"""
from types import SimpleNamespace

import pytest

from oto_mcp.capabilities.orgs import members as om
from oto_mcp.capabilities._types import AuthzDenied


def _ctx(sub="caller"):
    return SimpleNamespace(sub=sub)


def _patch(monkeypatch, *, role="org_admin", admins=1, org=True):
    """`role` = rôle actuel de la cible (None = non-membre), `admins` = org_admins de l'org."""
    monkeypatch.setattr(om.org_store, "get_org", lambda oid: {"id": oid} if org else None)
    monkeypatch.setattr(om.org_store, "get_org_role", lambda oid, sub: role)
    monkeypatch.setattr(
        om.org_store, "list_org_members",
        lambda oid: [{"org_role": "org_admin"} for _ in range(admins)]
        + [{"org_role": "org_member"}],
    )
    written = []
    monkeypatch.setattr(om.org_store, "add_org_member",
                        lambda oid, sub, r: written.append((oid, sub, r)))
    return written


# Les deux chemins d'écriture de rôle, appelés avec la MÊME intention métier :
# « donner à `sub` le rôle `role` dans l'org `org_id` ».
def _via_add(org_id, sub, role):
    return om._add_member(_ctx(), om.AddMemberInput(org_id=org_id, target=sub, role=role))


def _via_set_role(org_id, sub, role):
    return om._set_member_role(_ctx(), om.SetMemberRoleInput(org_id=org_id, sub=sub, role=role))


BOTH = pytest.mark.parametrize("write_role", [_via_add, _via_set_role],
                               ids=["POST /members", "POST /members/{sub}"])


@BOTH
def test_demoting_the_last_org_admin_is_refused(monkeypatch, write_role):
    """Le cœur de #273 : la route d'ajout acceptait ce que la route de rôle refusait."""
    written = _patch(monkeypatch, role="org_admin", admins=1)
    with pytest.raises(AuthzDenied) as e:
        write_role(7, "boss", "org_member")
    assert e.value.status == 409
    assert e.value.code == "last_org_admin"
    assert written == []                      # rien n'a été écrit : l'org garde son admin


@BOTH
def test_demoting_an_admin_among_several_is_allowed(monkeypatch, write_role):
    """La garde ne mord que sur le DERNIER : sinon on ne pourrait plus rétrograder du tout."""
    written = _patch(monkeypatch, role="org_admin", admins=2)
    out = write_role(7, "boss", "org_member")
    assert out == {"ok": True, "org_id": 7, "sub": "boss", "role": "org_member"}
    assert written == [(7, "boss", "org_member")]


@BOTH
def test_rewriting_the_last_admin_as_admin_is_allowed(monkeypatch, write_role):
    """Ré-écrire org_admin sur le dernier org_admin n'enlève rien — c'est un no-op, pas
    un verrouillage. Refuser ici casserait l'idempotence de la ré-invitation."""
    written = _patch(monkeypatch, role="org_admin", admins=1)
    out = write_role(7, "boss", "org_admin")
    assert out["role"] == "org_admin"
    assert written == [(7, "boss", "org_admin")]


@BOTH
def test_writing_a_plain_member_is_untouched(monkeypatch, write_role):
    """La garde regarde le rôle ACTUEL de la cible : un simple membre n'est jamais
    concerné, même dans une org à un seul admin."""
    written = _patch(monkeypatch, role="org_member", admins=1)
    out = write_role(7, "someone", "org_member")
    assert out["ok"] is True
    assert written == [(7, "someone", "org_member")]


# ── Les deux divergences LÉGITIMES entre les deux routes ─────────────────────

def test_add_accepts_a_non_member(monkeypatch):
    """`add` sur un non-membre est son objet même (pas de 404) — et la garde ne s'y
    déclenche pas : on ne rétrograde personne en ajoutant quelqu'un."""
    written = _patch(monkeypatch, role=None, admins=1)
    out = om._add_member(_ctx(), om.AddMemberInput(org_id=7, target="newbie", role="org_member"))
    assert out["sub"] == "newbie"
    assert written == [(7, "newbie", "org_member")]


def test_set_role_refuses_a_non_member(monkeypatch):
    """`set_role` exige l'appartenance : 404, jamais une adhésion implicite."""
    written = _patch(monkeypatch, role=None, admins=1)
    with pytest.raises(AuthzDenied) as e:
        om._set_member_role(_ctx(), om.SetMemberRoleInput(org_id=7, sub="ghost", role="org_admin"))
    assert e.value.code == "not_a_member"
    assert written == []


def test_add_resolves_an_email_before_guarding(monkeypatch):
    """`add` accepte un email : la garde doit voir le sub RÉSOLU, pas la chaîne saisie —
    sinon elle interroge le rôle d'une cible qui n'existe pas et laisse tout passer."""
    written = _patch(monkeypatch, role="org_admin", admins=1)
    monkeypatch.setattr(om.db, "get_users_by_email", lambda e: [{"sub": "boss"}])
    with pytest.raises(AuthzDenied) as e:
        om._add_member(_ctx(), om.AddMemberInput(org_id=7, target="boss@x.tld", role="org_member"))
    assert e.value.code == "last_org_admin"
    assert written == []
