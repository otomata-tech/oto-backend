"""Une org créée par la console d'administration naît AVEC son responsable (#297).

`org.admin.create` appelait `org_store.create_org(...)` et rien d'autre — l'org
naissait donc sans aucun membre, donc sans personne pour l'administrer. C'est
précisément l'état que la garde de #280 refuse (`group_unadministrable`) : la garde
n'était pas du code mort, mais l'état qu'elle protège ne devrait pas pouvoir naître.
Mesuré en prod le 11/08 : deux orgs de juin sont encore sans aucun membre.

Arbitrage : `admin` (email | sub | `"me"`) NOMME le responsable, parce que le cas
d'usage réel de cette console est de provisionner une org pour quelqu'un d'autre ;
omis, c'est l'appelant. L'invariant tenu par ce fichier est donc « toute création
écrit exactement un org_admin », quel que soit le chemin — pas « le créateur est
membre ».

On monkeypatche les stores (pas de PG) et on appelle les deux faces : le handler de
capacité (REST `POST /api/admin/orgs`) et la console MCP `oto_admin_org(op=create)`.
"""
from types import SimpleNamespace

import pytest

import oto_mcp.capabilities.admin_console as ac
from oto_mcp.capabilities.orgs import admin as oa
from oto_mcp.capabilities._types import AuthzDenied

NEW_ORG = 512
CALLER = "operator-sub"


def _ctx(sub=CALLER):
    return SimpleNamespace(sub=sub, org_id=None)


def _patch(monkeypatch, *, known_emails=None):
    """Journalise les créations et les adhésions écrites. `known_emails` = les users
    déjà connus (un email inconnu ne se résout pas — il ne s'est jamais connecté)."""
    known = known_emails or {}
    created, members = [], []
    monkeypatch.setattr(oa.org_store, "create_org",
                        lambda name, created_by, **kw: created.append((name, created_by)) or NEW_ORG)
    monkeypatch.setattr(oa.org_store, "add_org_member",
                        lambda oid, sub, role: members.append((oid, sub, role)))
    # ⚠️ La résolution lit TOUS les porteurs d'une adresse depuis le 05/09 : une
    # adresse peut en avoir plusieurs, et en choisir un en silence était le défaut.
    monkeypatch.setattr("oto_mcp.capabilities.orgs.members.db.get_users_by_email",
                        lambda e: ([{"sub": known[e]}] if e in known else []))
    return created, members


# Les deux faces, appelées avec la MÊME intention métier : « crée l'org `name`,
# responsable `admin` ».
def _via_capability(name, admin=None):
    return oa._create_org(_ctx(), oa.CreateOrgInput(name=name, admin=admin))


def _via_console(name, admin=None):
    return ac._org(_ctx(), ac.OrgAdminInput(op="create", name=name, admin=admin))


BOTH = pytest.mark.parametrize("create", [_via_capability, _via_console],
                               ids=["POST /api/admin/orgs", "oto_admin_org(op=create)"])


# ── L'invariant : jamais d'org sans responsable ──────────────────────────────

@BOTH
def test_creating_without_an_admin_makes_the_caller_the_chief(monkeypatch, create):
    """Le chemin paresseux (le seul que poste le dashboard aujourd'hui) doit être le
    chemin juste : à défaut de nom, l'opérateur garde la main — jamais personne."""
    created, members = _patch(monkeypatch)
    out = create("Acme Corp")
    assert created == [("Acme Corp", CALLER)]
    assert members == [(NEW_ORG, CALLER, "org_admin")]
    assert out["org_id"] == NEW_ORG and out["admin_sub"] == CALLER


@BOTH
def test_creating_for_someone_else_keeps_the_operator_out(monkeypatch, create):
    """LE cas d'usage de cette console : l'org est provisionnée POUR un client. Le
    client est l'unique responsable, l'opérateur plateforme n'est pas membre."""
    created, members = _patch(monkeypatch, known_emails={"chief@client.tld": "client-sub"})
    out = create("Client Corp", "chief@client.tld")
    assert members == [(NEW_ORG, "client-sub", "org_admin")]
    assert CALLER not in [m[1] for m in members]
    assert out["admin_sub"] == "client-sub"


@BOTH
def test_me_is_the_explicit_way_to_keep_it(monkeypatch, create):
    """« Je crée pour moi » reste dicible, et dit la même chose que l'omission."""
    _, members = _patch(monkeypatch)
    assert create("Mon périmètre", "me")["admin_sub"] == CALLER
    assert members == [(NEW_ORG, CALLER, "org_admin")]


@BOTH
def test_a_sub_can_be_named_directly(monkeypatch, create):
    """`admin` accepte un sub brut (même contrat que `org.member.add`)."""
    _, members = _patch(monkeypatch)
    create("Acme", "client-sub")
    assert members == [(NEW_ORG, "client-sub", "org_admin")]


# ── Les refus ne laissent jamais d'org orpheline derrière eux ────────────────

@BOTH
def test_an_unknown_admin_creates_nothing(monkeypatch, create):
    """La cible est résolue AVANT la création : sinon un email fautif produit
    exactement l'org sans responsable qu'on cherche à ne plus produire."""
    created, members = _patch(monkeypatch)
    with pytest.raises(AuthzDenied) as e:
        create("Acme", "ghost@client.tld")
    assert e.value.code == "unknown_user"
    assert created == [] and members == []


@BOTH
def test_an_empty_name_creates_nothing(monkeypatch, create):
    created, members = _patch(monkeypatch)
    with pytest.raises(AuthzDenied):
        create("   ")
    assert created == [] and members == []
