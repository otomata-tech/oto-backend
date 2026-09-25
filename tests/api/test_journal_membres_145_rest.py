"""Journal des entrées et sorties de membres d'une org (otomata-tech/oto#145) — contre PostgreSQL.

Ce que ce fichier prouve :
1. chaque geste sur l'appartenance écrit SA ligne — ajout, changement de rôle, retrait
   par un admin, départ volontaire, espace personnel créé d'office, invitation acceptée
   ou honorée au signup — avec l'org, le membre, l'ancien et le nouveau rôle et
   l'acteur (`None` = le système) ; un re-ajout au même rôle n'écrit rien ;
2. la ligne est écrite DANS la transaction du geste : un journal qui refuse d'écrire
   fait échouer le geste, qui n'a pas lieu ;
3. `GET /api/orgs/{id}/members/events` : réservé aux admins de CETTE org (et à
   l'opérateur plateforme), paginé ; un simple membre, un tiers et surtout un
   EX-membre sont refusés, sans que la réponse ne nomme l'org ; le 404 déclaré est
   rejoué sur la route servie ;
4. la même lecture par la console MCP (`oto_admin_org_member op=events`).

Porteur identifié par un vérifieur factice dont le bearer EST le sub. Base jetable
par module (`live`).
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

ADMIN, MEMBRE, PARTANT, RETIRE, TIERS, OPERATEUR, INVITE, INSCRIT, PERSO = (
    "jm-admin", "jm-membre", "jm-partant", "jm-retire", "jm-tiers", "jm-operateur",
    "jm-invite", "jm-inscrit", "jm-perso")
NOM_ORG = "zzJournal Org secrète"


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@journal.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


def _h(sub: str) -> dict:
    return {"Authorization": f"Bearer {sub}"}


def _journal(org_id: int) -> list[dict]:
    from oto_mcp import org_store
    return list(reversed(org_store.list_member_events(org_id, limit=200)))


def _geste(e: dict) -> tuple:
    return (e["sub"], e["action"], e["old_role"], e["new_role"], e["actor_sub"])


@pytest.fixture(scope="module")
def monde(live):
    from oto_mcp import db, org_store
    from oto_mcp.capabilities.orgs import members as om
    for sub in (ADMIN, MEMBRE, PARTANT, RETIRE, TIERS, OPERATEUR, INVITE, INSCRIT, PERSO):
        db.upsert_user(sub, email=f"{sub}@journal.invalid", name=sub.upper())
    db.set_user_role(OPERATEUR, "admin")
    o = org_store.create_org(NOM_ORG, created_by=ADMIN)
    org_store.add_org_member(o, ADMIN, "org_admin", actor=ADMIN)
    autre = org_store.create_org("Org du tiers", created_by=TIERS)
    org_store.add_org_member(autre, TIERS, "org_admin", actor=TIERS)

    class Ctx:
        def __init__(self, sub):
            self.sub = sub

    # Les gestes par les VRAIES capacités (celles que servent REST et MCP).
    om._add_member(Ctx(ADMIN), om.AddMemberInput(org_id=o, target=MEMBRE))
    om._add_member(Ctx(ADMIN), om.AddMemberInput(org_id=o, target=PARTANT))
    om._add_member(Ctx(ADMIN), om.AddMemberInput(org_id=o, target=RETIRE))
    om._set_member_role(Ctx(ADMIN), om.SetMemberRoleInput(org_id=o, sub=MEMBRE,
                                                           role="org_admin"))
    # Re-ajout au MÊME rôle : aucun changement, aucune ligne.
    om._add_member(Ctx(ADMIN), om.AddMemberInput(org_id=o, target=MEMBRE,
                                                 role="org_admin"))
    om._set_member_role(Ctx(MEMBRE), om.SetMemberRoleInput(org_id=o, sub=MEMBRE,
                                                            role="org_member"))
    om._remove_member(Ctx(ADMIN), om.RemoveMemberInput(org_id=o, target=RETIRE))
    om._leave_org(Ctx(PARTANT), om.LeaveOrgInput(org_id=o))
    # Invitation acceptée par clic (la personne agit), puis honorée au signup (le système).
    _, jeton = org_store.create_invitation(o, f"{INVITE}@journal.invalid", "org_member",
                                           invited_by=ADMIN)
    assert org_store.accept_invitation(jeton, INVITE)
    org_store.create_invitation(o, f"{INSCRIT}@journal.invalid", "org_member",
                                invited_by=ADMIN)
    assert org_store.reconcile_signup_with_invitation(INSCRIT, f"{INSCRIT}@journal.invalid")
    perso = org_store.ensure_personal_org(PERSO, f"{PERSO}@journal.invalid", "Perso")
    yield {"o": o, "autre": autre, "perso": perso}


@pytest.fixture(scope="module")
def client(monde):
    from oto_mcp.api import routes as api_routes
    return TestClient(Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)))


# ── 1. chaque geste écrit sa ligne ───────────────────────────────────────────

def test_chaque_geste_ecrit_sa_ligne(monde):
    assert [_geste(e) for e in _journal(monde["o"])] == [
        (ADMIN, "added", None, "org_admin", ADMIN),
        (MEMBRE, "added", None, "org_member", ADMIN),
        (PARTANT, "added", None, "org_member", ADMIN),
        (RETIRE, "added", None, "org_member", ADMIN),
        (MEMBRE, "role_changed", "org_member", "org_admin", ADMIN),
        # (le re-ajout au même rôle n'a rien écrit)
        (MEMBRE, "role_changed", "org_admin", "org_member", MEMBRE),
        (RETIRE, "removed", "org_member", None, ADMIN),
        (PARTANT, "removed", "org_member", None, PARTANT),
        (INVITE, "added", None, "org_member", INVITE),
        (INSCRIT, "added", None, "org_member", None),
    ]


def test_l_espace_personnel_est_un_geste_du_systeme(monde):
    assert [_geste(e) for e in _journal(monde["perso"])] == [
        (PERSO, "added", None, "org_admin", None)]


def test_le_journal_est_dans_la_transaction_du_geste(monde):
    """Un journal qui refuse d'écrire fait échouer le geste, qui n'a pas lieu :
    ni membre sans ligne, ni ligne sans membre."""
    import psycopg
    from oto_mcp import db, org_store
    from oto_mcp.db import _connect
    o = monde["o"]
    db.upsert_user("jm-refuse")   # son espace perso naît AVANT la contrainte de refus
    with _connect() as conn:
        conn.execute("ALTER TABLE org_member_events ADD CONSTRAINT jm_refus "
                     "CHECK (sub <> 'jm-refuse') NOT VALID")
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            org_store.add_org_member(o, "jm-refuse", "org_member", actor=ADMIN)
        assert org_store.get_org_role(o, "jm-refuse") is None
        with pytest.raises(psycopg.errors.CheckViolation):
            org_store.add_org_member(o, "jm-refuse", "org_member", actor=ADMIN)
    finally:
        with _connect() as conn:
            conn.execute("ALTER TABLE org_member_events DROP CONSTRAINT jm_refus")
    # Et le retrait : MEMBRE reste membre si sa ligne ne s'écrit pas.
    with _connect() as conn:
        conn.execute("ALTER TABLE org_member_events ADD CONSTRAINT jm_refus2 "
                     "CHECK (action <> 'removed') NOT VALID")
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            org_store.remove_org_member(o, MEMBRE, actor=ADMIN)
        assert org_store.get_org_role(o, MEMBRE) == "org_member"
    finally:
        with _connect() as conn:
            conn.execute("ALTER TABLE org_member_events DROP CONSTRAINT jm_refus2")


# ── 3. la lecture REST ───────────────────────────────────────────────────────

def test_l_admin_lit_le_journal_pagine(client, monde):
    o = monde["o"]
    r = client.get(f"/api/orgs/{o}/members/events", headers=_h(ADMIN),
                   params={"limit": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"org_id", "events", "next_before_id"}
    page1 = body["events"]
    assert [e["sub"] for e in page1] == [INSCRIT, INVITE, PARTANT]   # plus récent d'abord
    assert page1[2]["action"] == "removed" and page1[2]["actor_sub"] == PARTANT
    assert page1[0]["actor_sub"] is None and page1[0]["actor_email"] is None
    assert page1[1]["email"] == f"{INVITE}@journal.invalid"
    assert body["next_before_id"] == page1[-1]["id"]
    tout, curseur = list(page1), body["next_before_id"]
    while curseur is not None:
        r = client.get(f"/api/orgs/{o}/members/events", headers=_h(ADMIN),
                       params={"limit": 3, "before_id": curseur})
        assert r.status_code == 200, r.text
        tout += r.json()["events"]
        curseur = r.json()["next_before_id"]
    assert [e["id"] for e in tout] == [e["id"] for e in reversed(_journal(o))]


def test_l_operateur_plateforme_lit_le_journal(client, monde):
    r = client.get(f"/api/orgs/{monde['o']}/members/events", headers=_h(OPERATEUR))
    assert r.status_code == 200, r.text
    assert len(r.json()["events"]) == len(_journal(monde["o"]))


@pytest.mark.parametrize("qui", [MEMBRE, TIERS, RETIRE, PARTANT],
                         ids=["simple-membre", "tiers", "ex-membre-retire",
                              "ex-membre-parti"])
def test_refuse_a_qui_n_administre_pas_l_org(client, monde, qui):
    r = client.get(f"/api/orgs/{monde['o']}/members/events", headers=_h(qui))
    assert r.status_code == 403, r.text
    # Rien sur l'org quittée : ni son nom, ni un membre, ni un geste.
    for trace in (NOM_ORG, ADMIN, "removed", "added"):
        assert trace not in r.text


def test_un_ex_membre_n_apprend_rien_de_l_org_quittee(client, monde):
    """Nulle part dans SES lectures : l'org quittée n'apparaît plus dans ses orgs."""
    r = client.get("/api/me/orgs", headers=_h(RETIRE))
    assert r.status_code == 200, r.text
    assert NOM_ORG not in r.text


def test_org_inconnue_404_declare(client):
    """Le refus déclaré (`unknown_org`), rejoué sur la route servie : seul l'opérateur
    plateforme franchit l'autz sur une org qui n'existe pas."""
    r = client.get("/api/orgs/999999/members/events", headers=_h(OPERATEUR))
    assert r.status_code == 404, r.text
    assert r.json()["error"] == "unknown_org"


# ── 4. la console MCP ────────────────────────────────────────────────────────

def test_console_mcp_op_events(monde):
    from oto_mcp.capabilities import admin_console as ac
    from oto_mcp.capabilities._types import AuthzDenied, RawCtx
    from oto_mcp.capabilities.registry import by_key
    cap = by_key("admin.org_member")
    inp = ac.OrgMemberAdminInput(op="events", org_id=monde["o"], limit=2)
    with pytest.raises(AuthzDenied):
        cap.authz(RawCtx(sub=RETIRE), inp)
    out = cap.handler(cap.authz(RawCtx(sub=ADMIN), inp), inp)
    assert [e["sub"] for e in out["events"]] == [INSCRIT, INVITE]
    assert out["next_before_id"] == out["events"][-1]["id"]
