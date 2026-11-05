"""L'org d'inscription SEULE cède la place d'org active ; PEUPLÉE, elle la garde.

Plus d'org perso (05/11/2026) : toute org née d'une inscription porte `auto_created`,
pour toujours — y compris l'org d'une entreprise que ses collègues ont rejointe. La
règle d'adhésion (`org_store.members.add_org_member_in`, celle de l'invitation sous un
espace suspendu) distingue donc, par `db.org_d_inscription.seule_d_inscription` :

- 1a : le collègue invité, dont l'org d'essai (née de son inscription, il y est seul)
  est suspendue, est PROMU sur l'org qui l'invite — même si celle-ci est née d'une
  inscription, peuplée ou non : on y invite, c'est une org d'équipe ;
- 1b : l'org d'une entreprise née d'une inscription mais PEUPLÉE ne cède pas sa place
  d'org active quand un de ses membres rejoint une autre org.

Sur une vraie base : le critère est du SQL, un double ne prouverait que le graphe
d'appels (`tests/test_add_org_member_promotion.py` tient l'aiguillage).
"""
from __future__ import annotations

import pytest


def _inscrit(sub: str) -> int:
    """Un compte qui s'inscrit sans invitation : `upsert_user` lui pose son org
    (`ensure_home_org`), née de l'inscription, où il est seul ; rend son id."""
    from oto_mcp import org_store
    from oto_mcp.db import upsert_user
    upsert_user(sub, email=f"{sub}@x.tld")
    home = org_store.get_active_org(sub)
    assert home is not None and org_store.is_auto_created(home)
    return home


@pytest.mark.parametrize("peuplee", [False, True], ids=["seule", "peuplee"])
def test_1a_l_essai_suspendu_cede_a_l_org_d_inscription_qui_invite(live, peuplee):
    from oto_mcp import org_store, org_suspension
    p = "p" if peuplee else "s"
    admin = f"u-1a-admin-{p}"
    org_qui_invite = _inscrit(admin)
    if peuplee:
        collegue = f"u-1a-collegue-{p}"
        _inscrit(collegue)
        org_store.add_org_member(org_qui_invite, collegue, "org_member", actor=admin)
    invite = f"u-1a-invite-{p}"
    essai = _inscrit(invite)
    org_store.suspend_org(essai, by="svc", reason="trial_ended")
    org_suspension.invalider()
    try:
        org_store.add_org_member(org_qui_invite, invite, "org_member", actor=admin)
        assert org_store.get_active_org(invite) == org_qui_invite
    finally:
        org_store.resume_org(essai)
        org_suspension.invalider()


def test_1b_l_org_d_entreprise_peuplee_garde_sa_place(live):
    from oto_mcp import org_store
    fondatrice = "u-1b-fondatrice"
    entreprise = _inscrit(fondatrice)
    salarie = "u-1b-salarie"
    _inscrit(salarie)
    org_store.add_org_member(entreprise, salarie, "org_member", actor=fondatrice)
    # Rejoindre l'org d'entreprise : son org d'inscription, où il était seul, a cédé.
    assert org_store.get_active_org(salarie) == entreprise
    assert org_store.is_auto_created(entreprise)

    ailleurs = org_store.create_org("Ailleurs", created_by="u-1b-tiers")
    org_store.add_org_member(ailleurs, "u-1b-tiers", "org_admin", actor=None)
    for membre in (salarie, fondatrice):
        org_store.add_org_member(ailleurs, membre, "org_member", actor="u-1b-tiers")
        assert org_store.get_active_org(membre) == entreprise, membre
