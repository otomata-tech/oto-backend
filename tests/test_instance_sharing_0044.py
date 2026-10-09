"""ADR 0044 — partage d'instance : share_side (étendre, prêt nominatif). Le cran
`share_down` BYO (restreindre sous le niveau) a été RETIRÉ (2026-07-08) : une
instance BYO est utilisable par tout le sous-arbre de son owner, restreindre =
poser l'instance au bon niveau (équipe). `share_down` ne subsiste que sur les
instances PLATFORM (liste des grantees — test_free_tier_platform_key). Exerce la
VRAIE logique (get_instance_sharing mocké avec des données), pas le fail-safe sans DB."""
import types

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import (access, credentials_store, group_store, roles, instance_refs, db, org_store,
                     providers)
from oto_mcp.capabilities.connectors import sharing as connectors_sharing
from oto_mcp.capabilities._types import AuthzDenied


# ── _sub_matches_scopes : vocabulaire commun aux deux axes ────────────────────
def test_sub_matches_scopes_user(monkeypatch):
    assert access._sub_matches_scopes("alice", ["user:alice"]) is True
    assert access._sub_matches_scopes("alice", ["user:bob"]) is False

def test_sub_matches_scopes_org_is_everyone():
    assert access._sub_matches_scopes("whoever", ["org"]) is True

def test_sub_matches_scopes_group_membership(monkeypatch):
    monkeypatch.setattr(group_store, "is_group_member",
                        lambda sub, gid: sub == "alice" and gid == 5)
    assert access._sub_matches_scopes("alice", ["group:5"]) is True
    assert access._sub_matches_scopes("bob", ["group:5"]) is False

def test_sub_matches_scopes_empty_is_false():
    assert access._sub_matches_scopes("alice", []) is False

def test_sub_matches_scopes_ignores_malformed(monkeypatch):
    monkeypatch.setattr(group_store, "is_group_member", lambda s, g: False)
    assert access._sub_matches_scopes("alice", ["group:notanint", "user:alice"]) is True


# ── guard : pin d'une instance d'ORG = ouvert à tout membre (plus d'allowlist) ─
def _mock_sharing(monkeypatch, down, side):
    monkeypatch.setattr(credentials_store, "get_instance_sharing",
                        lambda et, eid, conn, acct="": (down, side))

def test_guard_org_pin_open_to_any_member(monkeypatch):
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    ref = instance_refs.parse_ref(instance_refs.make_org_ref(35, "zoho"))
    # tout membre de l'org → OK, co-pose l'org de l'instance (un share_down
    # résiduel en base est SANS effet — le cran BYO est retiré)
    assert access.guard_instance_access("member", ref) == 35

def test_guard_org_pin_rejects_non_member(monkeypatch):
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: False)
    ref = instance_refs.parse_ref(instance_refs.make_org_ref(35, "zoho"))
    with pytest.raises(McpError, match="not a member of org"):
        access.guard_instance_access("intrus", ref)


# ── guard : pin d'une instance de GROUPE = lecteurs du groupe (admin inclus) ──
def test_guard_group_pin_reader_and_org_admin(monkeypatch):
    # `can_read_group` escalade pour l'org_admin (roles.py) : c'est le chemin par
    # lequel un admin d'org utilise l'instance d'une équipe de son org.
    monkeypatch.setattr(roles, "can_read_group",
                        lambda sub, gid: sub in ("finance_guy", "clemence_admin"))
    monkeypatch.setattr(group_store, "get_group", lambda gid: {"id": gid, "org_id": 35})
    ref = instance_refs.parse_ref(instance_refs.make_group_ref(2, "pennylane"))
    assert access.guard_instance_access("finance_guy", ref) == 35
    assert access.guard_instance_access("clemence_admin", ref) == 35
    with pytest.raises(McpError, match="not a member of group"):
        access.guard_instance_access("other_dept", ref)


# ── guard : share_side (prêt à un pair) ───────────────────────────────────────
def test_guard_member_share_side_allows_beneficiary(monkeypatch):
    # instance de "owner" dans l'org 8, prêtée à "bob" — prêteur vivant (#898 : la
    # garde lit l'état de pause du prêteur ; le cas en pause est sur base réelle,
    # tests/test_account_suspension_prets.py)
    _mock_sharing(monkeypatch, [], ["user:bob"])
    monkeypatch.setattr(db, "get_suspension", lambda sub: None)
    monkeypatch.setattr(access, "current_org", lambda sub: 99)  # org de l'APPELANT
    ref = instance_refs.parse_ref(instance_refs.make_member_ref(8, "owner", "zoho"))
    # bob emprunte : autorisé, co-pose SON org (99), pas celle de l'owner (8)
    assert access.guard_instance_access("bob", ref) == 99

def test_guard_member_share_side_rejects_non_beneficiary(monkeypatch):
    _mock_sharing(monkeypatch, [], ["user:bob"])
    ref = instance_refs.parse_ref(instance_refs.make_member_ref(8, "owner", "zoho"))
    with pytest.raises(McpError, match="another member"):
        access.guard_instance_access("carol", ref)

def test_guard_member_owner_still_works(monkeypatch):
    # le propriétaire garde le chemin owner (pas de lecture share_side nécessaire)
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    ref = instance_refs.parse_ref(instance_refs.make_member_ref(8, "owner", "zoho"))
    assert access.guard_instance_access("owner", ref) == 8


# ── oto_lend_instance : write path de share_side ──────────────────────────────
def _lend_wiring(monkeypatch, *, existing_side=None, write_ok=True, accounts=("",)):
    captured = {}
    monkeypatch.setattr(providers, "connector_for_provider", lambda c: object())
    monkeypatch.setattr(credentials_store, "list_accounts",
                        lambda et, eid, conn: [{"account": a, "meta": {}} for a in accounts])
    monkeypatch.setattr(access, "current_org", lambda sub: 35)
    monkeypatch.setattr(db, "get_user", lambda sub: {"sub": sub})
    monkeypatch.setattr(credentials_store, "get_instance_sharing",
                        lambda et, eid, conn, acct="": ([], list(existing_side or [])))
    def _set(et, eid, conn, acct="", *, share_down=None, share_side=None):
        captured["share_side"] = share_side
        captured["eid"] = eid
        captured["row"] = (conn, acct)
        return write_ok
    monkeypatch.setattr(credentials_store, "set_instance_sharing", _set)
    return captured

def _ctx(sub="alice"):
    return types.SimpleNamespace(sub=sub)

def test_lend_adds_beneficiary(monkeypatch):
    cap = _lend_wiring(monkeypatch)
    inp = connectors_sharing.LendInstanceInput(connector="zoho", to="bob")
    out = connectors_sharing._lend_instance(_ctx("alice"), inp)
    assert cap["share_side"] == ["user:bob"]
    assert cap["eid"] == "35:alice"          # ne prête QUE sa propre ligne
    assert out["lent_to"] == ["bob"] and out["revoked"] is False

def test_lend_revoke_removes(monkeypatch):
    cap = _lend_wiring(monkeypatch, existing_side=["user:bob", "user:carol"])
    inp = connectors_sharing.LendInstanceInput(connector="zoho", to="bob", revoke=True)
    out = connectors_sharing._lend_instance(_ctx("alice"), inp)
    assert cap["share_side"] == ["user:carol"]
    assert out["lent_to"] == ["carol"] and out["revoked"] is True

def test_lend_no_instance_raises(monkeypatch):
    _lend_wiring(monkeypatch, write_ok=False)  # aucune ligne à mettre à jour
    inp = connectors_sharing.LendInstanceInput(connector="zoho", to="bob")
    with pytest.raises(AuthzDenied, match="nothing to lend|No `"):
        connectors_sharing._lend_instance(_ctx("alice"), inp)

def test_lend_self_rejected(monkeypatch):
    _lend_wiring(monkeypatch)
    inp = connectors_sharing.LendInstanceInput(connector="zoho", to="alice")
    with pytest.raises(AuthzDenied, match="yourself"):
        connectors_sharing._lend_instance(_ctx("alice"), inp)

def test_lend_unknown_user_rejected(monkeypatch):
    _lend_wiring(monkeypatch)
    monkeypatch.setattr(db, "get_user", lambda sub: None)
    inp = connectors_sharing.LendInstanceInput(connector="zoho", to="ghost")
    with pytest.raises(AuthzDenied, match="Unknown user"):
        connectors_sharing._lend_instance(_ctx("alice"), inp)


# ── `to` = l'email d'un membre de l'org courante (mesuré en prod : un agent a passé
# l'adresse d'une collègue, l'outil l'a refusée comme « inconnue ») ─────────────────
def _membres(monkeypatch, annuaire):
    vus = []

    def _par_email(org, email):
        vus.append(org)
        sub = annuaire.get(email.strip().lower())
        return {"sub": sub, "org_role": "org_member"} if sub else None
    monkeypatch.setattr(org_store, "get_org_member_by_email", _par_email)
    return vus


def test_lend_par_email_d_un_membre_de_l_org(monkeypatch):
    cap = _lend_wiring(monkeypatch)
    vus = _membres(monkeypatch, {"bob@x.test": "bob"})
    inp = connectors_sharing.LendInstanceInput(connector="zoho", to=" Bob@X.test ")
    out = connectors_sharing._lend_instance(_ctx("alice"), inp)
    assert cap["share_side"] == ["user:bob"] and out["lent_to"] == ["bob"]
    assert vus == [35], "l'email se résout parmi les membres de l'org COURANTE seulement"


def test_lend_par_email_hors_org_dit_que_l_appartenance_manque(monkeypatch):
    cap = _lend_wiring(monkeypatch)
    _membres(monkeypatch, {})
    # Un compte oto qui porte cet email AILLEURS ne change rien au refus.
    monkeypatch.setattr(db, "get_user_by_email",
                        lambda e: pytest.fail("pas d'annuaire de la plateforme"), raising=False)
    inp = connectors_sharing.LendInstanceInput(connector="zoho", to="ext@y.test")
    with pytest.raises(AuthzDenied) as e:
        connectors_sharing._lend_instance(_ctx("alice"), inp)
    assert e.value.code == "not_an_org_member"
    assert "not the email of a member of your organization" in e.value.message
    assert "the email or the sub of a member of your organization" in e.value.message
    assert "share_side" not in cap


def test_lend_par_son_propre_email_refuse(monkeypatch):
    _lend_wiring(monkeypatch)
    _membres(monkeypatch, {"alice@x.test": "alice"})
    inp = connectors_sharing.LendInstanceInput(connector="zoho", to="alice@x.test")
    with pytest.raises(AuthzDenied, match="yourself"):
        connectors_sharing._lend_instance(_ctx("alice"), inp)


def test_le_refus_d_un_sub_inconnu_dit_ce_qui_est_accepte(monkeypatch):
    _lend_wiring(monkeypatch)
    monkeypatch.setattr(db, "get_user", lambda sub: None)
    inp = connectors_sharing.LendInstanceInput(connector="zoho", to="ghost")
    with pytest.raises(AuthzDenied, match="the email or the sub of a member"):
        connectors_sharing._lend_instance(_ctx("alice"), inp)


def test_le_texte_servi_dit_qu_un_email_est_accepte():
    from oto_mcp.capabilities.registry import CAPABILITIES
    instance = next(c for c in CAPABILITIES if c.mcp == "oto_instance")
    assert "the email or the sub of a member of your organization" in instance.description


# ── le compte prêté sur un connecteur multi-compte, et la carte d'un service ────────
# Mesuré en prod : `lend connector=microsoft` sans `account` répondait « No instance »
# à une personne qui en avait une ; `connector=outlook` aussi (la carte n'a pas de ligne).
def test_lend_sans_account_prend_le_seul_compte(monkeypatch):
    cap = _lend_wiring(monkeypatch, accounts=("jane@contoso.example",))
    out = connectors_sharing._lend_instance(
        _ctx("alice"), connectors_sharing.LendInstanceInput(connector="zoho", to="bob"))
    assert cap["row"] == ("zoho", "jane@contoso.example")
    assert out["account"] == "jane@contoso.example" and out["lent_to"] == ["bob"]


def test_lend_sans_account_sur_plusieurs_comptes_les_nomme(monkeypatch):
    cap = _lend_wiring(monkeypatch, accounts=("a@x.test", "b@x.test"))
    with pytest.raises(AuthzDenied) as e:
        connectors_sharing._lend_instance(
            _ctx("alice"), connectors_sharing.LendInstanceInput(connector="zoho", to="bob"))
    assert e.value.code == "account_required"
    assert "pass `account=`: one of `a@x.test`, `b@x.test`" in e.value.message
    assert "No `" not in e.value.message and "share_side" not in cap


def test_lend_d_un_compte_qui_n_est_pas_le_sien_les_nomme(monkeypatch):
    _lend_wiring(monkeypatch, accounts=("a@x.test",))
    with pytest.raises(AuthzDenied) as e:
        connectors_sharing._lend_instance(_ctx("alice"), connectors_sharing.LendInstanceInput(
            connector="zoho", to="bob", account="z@x.test"))
    assert e.value.code == "unknown_account" and "`a@x.test`" in e.value.message


def test_lend_sans_aucune_ligne_dit_no_instance(monkeypatch):
    cap = _lend_wiring(monkeypatch, accounts=())
    with pytest.raises(AuthzDenied, match="nothing to lend") as e:
        connectors_sharing._lend_instance(
            _ctx("alice"), connectors_sharing.LendInstanceInput(connector="zoho", to="bob"))
    assert e.value.code == "no_instance" and "share_side" not in cap


@pytest.mark.parametrize("carte", ["outlook", "microsoft"])
def test_lend_d_une_carte_service_prete_le_porteur_et_le_dit(monkeypatch, carte):
    cap = _lend_wiring(monkeypatch, accounts=("jane@contoso.example",))
    out = connectors_sharing._lend_instance(
        _ctx("alice"), connectors_sharing.LendInstanceInput(connector=carte, to="bob"))
    assert cap["row"] == ("microsoft", "jane@contoso.example"), "la ligne du PORTEUR"
    assert out["connector"] == "microsoft"
    assert "jane@contoso.example" in out["note"] and "every service it authorized" in out["note"]
    assert "outlook_calendar" in out["note"] and "sharepoint" in out["note"]
    assert ("has no instance of its own" in out["note"]) is (carte == "outlook")
