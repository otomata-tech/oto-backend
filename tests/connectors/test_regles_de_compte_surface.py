"""The surface of an account's RULES and of its OWNER (#1499): `connectors.instances.rules.*`,
`connectors.instances.share_events`, `connectors.instances.transfer`, and the accounts of
ANOTHER org in the listing (D1).

Bench: the base is replaced (the account, its rules, the journal, the roles), the level
is the REAL one (`access.niveau`): what is judged here is who may write what.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from oto_mcp import access, account_suspension, db, group_store, org_store, roles
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.connectors import instances as ci
from oto_mcp.capabilities.connectors import sharing, transfert
from oto_mcp.db import regles_de_comptes, transfert_de_compte

ORG, AILLEURS, EQUIPE, EQUIPE_AILLEURS = 8, 9, 40, 90
ADMIN, CHEF, TITULAIRE, MEMBRE, DEHORS = "admin", "chef", "titulaire", "membre", "dehors"
MEMBRES = {ADMIN, CHEF, TITULAIRE, MEMBRE}
EMAILS = {f"{s}@exemple.test": s for s in MEMBRES}
# Oto accounts outside the org: one designable, one a third-party tenant's (ADR 0052).
COMPTES_OTO = {"dehors@ailleurs.test": [{"sub": DEHORS}],
               "tiers@ailleurs.test": [{"sub": "acme:tiers"}]}


def _compte(owner_type="member", owner_id=f"{ORG}:{TITULAIRE}", connector="zoho",
            regles=(), org_id=ORG):
    return access.Compte(instance_id=12, owner_type=owner_type, owner_id=owner_id,
                         connector=connector, account="ventes", org_id=org_id,
                         regles=tuple(regles))


@pytest.fixture
def banc(monkeypatch):
    env = SimpleNamespace(compte=_compte(), poses=[], retraits=[], transferts=[],
                          journal=[])
    monkeypatch.setattr(sharing, "compte_par_id", lambda iid: env.compte)
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: (sub, org) == (ADMIN, ORG))
    monkeypatch.setattr(roles, "can_admin_group",
                        lambda sub, gid: (sub, gid) == (CHEF, EQUIPE))
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: org == ORG and sub in MEMBRES)
    monkeypatch.setattr(group_store, "is_group_member",
                        lambda sub, gid: gid == EQUIPE and sub in (CHEF, MEMBRE))
    monkeypatch.setattr(group_store, "get_group", lambda gid: {
        EQUIPE: {"id": EQUIPE, "org_id": ORG, "name": "Ventes"},
        EQUIPE_AILLEURS: {"id": EQUIPE_AILLEURS, "org_id": AILLEURS, "name": "Autre"},
    }.get(int(gid)))
    monkeypatch.setattr(org_store, "get_org_member_by_email", lambda org, email: (
        {"sub": EMAILS[email]} if org == ORG and email in EMAILS else None))
    monkeypatch.setattr(db, "get_users_by_email", lambda email: COMPTES_OTO.get(email, []))
    monkeypatch.setattr(account_suspension, "etat", lambda sub: None)
    monkeypatch.setattr(org_store, "get_org_role", lambda org, sub: (
        "member" if org == ORG and sub in MEMBRES else None))
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access, "qui_gere", lambda compte, org=None: "its owner")
    monkeypatch.setattr(access, "libelle_de_personne", lambda sub: sub)
    monkeypatch.setattr(regles_de_comptes, "regles_du_compte", lambda iid: [])
    monkeypatch.setattr(regles_de_comptes, "poser_regle",
                        lambda org, iid, **kw: env.poses.append(kw))
    monkeypatch.setattr(regles_de_comptes, "retirer_regle",
                        lambda iid, **kw: env.retraits.append(kw) or True)
    monkeypatch.setattr(regles_de_comptes, "journal_du_compte",
                        lambda iid, limit: env.journal[:limit])

    def _transferer(iid, *, vers_type, vers_id, acteur):
        env.transferts.append((vers_type, vers_id, acteur))
        return {"instance_id": iid, "owner_before": "member:x", "owner_after":
                f"{vers_type}:{vers_id}", "rules_removed": 0, "default_dropped": False}
    monkeypatch.setattr(transfert_de_compte, "transferer", _transferer)
    return env


def _ctx(sub):
    return ResolvedCtx(sub=sub, org_id=ORG)


def _refus(fn, *args, **kw):
    with pytest.raises(AuthzDenied) as e:
        fn(*args, **kw)
    return e.value


def _poser(sub, **kw):
    return sharing._set_rule(_ctx(sub), sharing.SetRuleInput(instance_id=12, **kw))


# ── Who sets a rule, and how high ────────────────────────────────────────────────

def test_seul_qui_gere_pose_une_regle_et_le_refus_nomme_qui_gere(banc):
    e = _refus(_poser, MEMBRE, to=CHEF, level="read")
    assert (e.status, e.code) == (403, "insufficient_level") and "its owner" in str(e)
    assert banc.poses == []
    _poser(TITULAIRE, to=MEMBRE, level="write")
    assert banc.poses == [{"subject_user": MEMBRE, "level": "write", "services": None,
                           "acteur": TITULAIRE}]


def test_manage_n_est_donne_que_par_l_admin_d_org(banc):
    e = _refus(_poser, TITULAIRE, to=MEMBRE, level="manage")
    assert e.code == "level_not_grantable" and banc.poses == []
    banc.compte = _compte("org", str(ORG))
    _poser(ADMIN, to=MEMBRE, level="manage")
    assert banc.poses[-1]["level"] == "manage"


def test_le_chef_d_equipe_donne_au_plus_write(banc):
    banc.compte = _compte("group", str(EQUIPE))
    e = _refus(_poser, CHEF, to_org=True, level="sensitive")
    assert e.code == "level_not_grantable"
    _poser(CHEF, to_org=True, level="write")
    assert banc.poses == [{"subject_org": True, "level": "write", "services": None,
                           "acteur": CHEF}]


def test_une_regle_ne_nomme_pas_le_titulaire(banc):
    e = _refus(_poser, TITULAIRE, to="titulaire@exemple.test", level="read")
    assert (e.status, e.code) == (400, "invalid_arguments")
    assert e.details == {"reason": "owner_subject"} and banc.poses == []


def test_l_admin_ne_gere_pas_le_compte_personnel_d_un_membre(banc):
    assert _refus(_poser, ADMIN, to=MEMBRE, level="read").code == "insufficient_level"


def test_un_seul_sujet(banc):
    banc.compte = _compte("org", str(ORG))
    assert _refus(_poser, ADMIN, to=MEMBRE, to_org=True, level="read").code == "one_subject"
    assert _refus(_poser, ADMIN, level="read").code == "one_subject"


def test_les_services_ne_valent_que_pour_google_ou_microsoft(banc):
    banc.compte = _compte("org", str(ORG))
    e = _refus(_poser, ADMIN, to=MEMBRE, level="read", services=["gmail"])
    assert e.code == "services_not_applicable"


def test_retirer_une_regle_absente_est_un_404(banc, monkeypatch):
    banc.compte = _compte("org", str(ORG))
    monkeypatch.setattr(regles_de_comptes, "retirer_regle", lambda iid, **kw: False)
    e = _refus(sharing._remove_rule, _ctx(ADMIN),
               sharing.RuleSubjectInput(instance_id=12, to=MEMBRE))
    assert (e.status, e.code) == (404, "no_rule")


# ── D1: a person outside the org, by the email of her oto account ────────────────

def test_une_regle_hors_org_se_pose_par_l_email_d_un_compte_oto(banc):
    _poser(TITULAIRE, to="dehors@ailleurs.test", level="read")
    assert banc.poses == [{"subject_user": DEHORS, "level": "read", "services": None,
                           "acteur": TITULAIRE}]


@pytest.mark.parametrize("to", ["inconnu@ailleurs.test", "tiers@ailleurs.test", DEHORS])
def test_le_refus_d_un_sujet_introuvable_ne_dit_pas_si_le_compte_existe(banc, to):
    """Unknown address, an address only a third-party tenant's account carries, a sub
    outside the org: the SAME code and the SAME message."""
    e = _refus(_poser, TITULAIRE, to=to, level="read")
    reference = _refus(_poser, TITULAIRE, to="inconnu@ailleurs.test", level="read")
    assert (e.status, e.code, str(e)) == (404, "unknown_subject", str(reference))
    assert to not in str(e) and banc.poses == []


def test_une_equipe_d_une_autre_org_est_refusee(banc):
    banc.compte = _compte("org", str(ORG))
    e = _refus(_poser, ADMIN, to_group=EQUIPE_AILLEURS, level="read")
    assert (e.status, e.code) == (404, "unknown_group") and banc.poses == []


def test_la_regle_d_une_personne_hors_org_se_retire_par_son_sub(banc):
    banc.compte = _compte(regles=[access.Regle(user=DEHORS, group=None, org=False,
                                               level="read")])
    sharing._remove_rule(_ctx(TITULAIRE), sharing.RuleSubjectInput(instance_id=12, to=DEHORS))
    assert banc.retraits == [{"subject_user": DEHORS, "acteur": TITULAIRE}]


def test_le_journal_dit_si_le_sujet_est_hors_org(banc):
    banc.journal = [
        {"action": "set", "subject_kind": "user", "subject_id": DEHORS, "level": "read",
         "previous_level": None, "services": None, "owner_before": None, "hors_org": True,
         "actor_sub": TITULAIRE, "at": "2026-11-04T10:00:00+00:00"},
        {"action": "removed", "subject_kind": "org", "subject_id": None, "level": None,
         "previous_level": "read", "services": None, "owner_before": None,
         "hors_org": False, "actor_sub": ADMIN, "at": "2026-11-03T10:00:00+00:00"}]
    out = sharing.ShareEvents.model_validate(sharing._share_events(
        _ctx(TITULAIRE), sharing.ShareEventsInput(instance_id=12)))
    assert [e.outside_org for e in out.events] == [True, False]


def test_la_liste_montre_le_compte_prete_par_une_autre_org_et_rien_d_autre(monkeypatch):
    """A rule of org 9 opens one of its accounts to `dehors` (calling from org 8): listed
    with the lending org's name and the level held; no owner, no author, no other account
    of that org. Her own key in org 9 is marked `elsewhere_org`."""
    monkeypatch.setattr(account_suspension, "etat", lambda sub: None)
    monkeypatch.setattr(org_store, "get_org_role", lambda org, sub: "member")
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    prete = access.Compte(instance_id=31, owner_type="member", owner_id=f"{AILLEURS}:boss",
                          connector="zoho", account="ventes", org_id=AILLEURS,
                          org_nom="Autre Org",
                          regles=(access.Regle(user=DEHORS, group=None, org=False,
                                               level="write"),))
    sienne = access.Compte(instance_id=32, owner_type="member",
                           owner_id=f"{AILLEURS}:{DEHORS}", connector="zoho", account="",
                           org_id=AILLEURS, org_nom="Autre Org")
    vus = []
    monkeypatch.setattr(access, "comptes_hors_org", lambda con, org, sub, account=None: (
        vus.append((con, org, sub)), [prete, sienne])[1])
    out = ci._comptes_d_ailleurs(DEHORS, ORG, "zoho")
    assert vus == [("zoho", ORG, DEHORS)]
    assert out[0] == {"id": "inst:31", "ref": "inst:31", "connector": "zoho",
                      "level": "member", "owner": {"type": "user"}, "name": out[0]["name"],
                      "account": "ventes", "via": "credential", "your_level": "write",
                      "lent_by_org": "Autre Org"}
    assert "boss" not in repr(out[0])
    assert out[1]["elsewhere_org"] == "Autre Org" and out[1]["your_level"] == "manage"
    assert "lent_by_org" not in out[1]
    for inst in out:
        ci.ConnectorInstance.model_validate(inst)


# ── The transfer of the owner ────────────────────────────────────────────────────

def _transferer(sub, **kw):
    return transfert._transfer(_ctx(sub), transfert.TransferInput(instance_id=12, **kw))


def test_le_titulaire_donne_son_compte_a_l_equipe_qu_il_dirige(banc, monkeypatch):
    monkeypatch.setattr(roles, "can_admin_group",
                        lambda sub, gid: (sub, gid) == (TITULAIRE, EQUIPE))
    monkeypatch.setattr(group_store, "is_group_member",
                        lambda sub, gid: (sub, gid) == (TITULAIRE, EQUIPE))
    out = _transferer(TITULAIRE, to_group=EQUIPE)
    assert banc.transferts == [("group", str(EQUIPE), TITULAIRE)]
    assert out["id"] == "inst:12"


def test_un_membre_qui_ne_dirige_pas_l_equipe_ne_la_lui_donne_pas(banc):
    e = _refus(_transferer, TITULAIRE, to_group=EQUIPE)
    assert (e.status, e.code) == (403, "insufficient_level") and banc.transferts == []


def test_le_chef_ne_donne_pas_le_compte_d_equipe_a_une_personne(banc):
    banc.compte = _compte("group", str(EQUIPE))
    e = _refus(_transferer, CHEF, to=MEMBRE)
    assert (e.status, e.code) == (403, "owner_change_forbidden") and banc.transferts == []


def test_l_admin_donne_le_compte_a_une_personne_de_l_org(banc):
    banc.compte = _compte("org", str(ORG))
    _transferer(ADMIN, to="membre@exemple.test")
    assert banc.transferts == [("member", f"{ORG}:{MEMBRE}", ADMIN)]


def test_le_nouveau_titulaire_est_dans_l_org_du_compte(banc):
    banc.compte = _compte("org", str(ORG))
    e = _refus(_transferer, ADMIN, to=DEHORS)
    assert (e.status, e.code) == (404, "not_an_org_member")
    e = _refus(_transferer, ADMIN, to_group=EQUIPE_AILLEURS)
    assert (e.status, e.code) == (404, "unknown_group") and banc.transferts == []


def test_le_refus_de_la_base_garde_son_code(banc, monkeypatch):
    def _refuse(*a, **k):
        raise transfert_de_compte.TransfertRefuse("account_name_taken", "Name taken.")
    monkeypatch.setattr(transfert_de_compte, "transferer", _refuse)
    banc.compte = _compte("org", str(ORG))
    e = _refus(_transferer, ADMIN, to_org=True)
    assert (e.status, e.code) == (409, "account_name_taken")
