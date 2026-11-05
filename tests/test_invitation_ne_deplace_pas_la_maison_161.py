"""oto#161 — accepter une invitation ne DÉPLACE pas une maison établie.

L'org **maison** (`org_members.is_active`, ADR 0023) décide de tout ce qu'un appel
vise quand il ne précise rien : les **clés membre** en tête (rangées sous
`{maison}:{sub}`, et **jamais migrées** quand la maison change). `_accept_invitation_row`
appelait `members.set_active_org(sub, org_id)` juste après l'ajout — sans condition.
Mesuré en production le 10/09/2026 : 7 personnes déplacées d'une org réelle vers une
autre, 8 dont les clés membre sont devenues injoignables par défaut (« non configuré »
sans que rien ne dise que la clé existe ailleurs). Le même corps est atteint **sans
aucun clic** par `reconcile_signup_with_invitation`, sur simple correspondance d'email.

La règle de la maison existe déjà, écrite UNE fois dans `add_org_member` (ADR
0030/0033) : aucune maison → la nouvelle ; maison = l'espace perso silencieux →
promotion ; **maison réelle établie → on n'y touche pas**. Le correctif ne la recopie
pas, il la laisse décider — d'où le banc en **deux moitiés indissociables** : un
correctif qui protégerait la maison établie mais n'en poserait plus à qui n'en a pas
serait PIRE que le défaut (l'invité retomberait sur un espace perso vide).

Chaque moitié est jouée sur les **deux entrées** (lien mail, réconciliation de
signup — le code court partageable, troisième entrée d'origine, a été RETIRÉ le
15/09/2026, oto-backend#560). Elles convergent vers le même corps, mais c'est la
leçon de #280 : un test qui n'exerce qu'un chemin passe au vert en laissant le
trou ouvert.

Palier ÉQUIPE inclus : `group_store.set_active_group` écrit lui aussi
`org_members.is_active` (invariant ADR 0012, groupe actif ⊂ org active) — appelé nu, il
ré-ouvrait le trou par la bande.

Vrai PostgreSQL, base neuve pour ce module : ce banc n'a de valeur que sur le vrai
`add_org_member` (verrou advisory, index partiel `org_members_one_active`, colonne
`orgs.personal_of`). Le mesurer sur des doublures ne prouverait qu'un graphe d'appels.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def live(pg_module_dsn):
    pytest.importorskip("psycopg")
    url_avant = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_module_dsn
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant


# ── Le monde de chaque cas ───────────────────────────────────────────────────

def _org(nom: str, sub: str, role: str = "org_admin") -> int:
    """Une org RÉELLE (pas une perso) dont `sub` est membre."""
    from oto_mcp import org_store
    oid = org_store.create_org(nom, created_by=sub)
    org_store.add_org_member(oid, sub, role)
    return oid


def _invitation(org_id: int, email: str, *, group_id=None, group_role=None):
    """Rend le token d'une invitation nominative fraîche."""
    from oto_mcp import org_store
    _id, token = org_store.create_invitation(
        org_id, email, "org_member", "u-161-emetteur",
        group_id=group_id, group_role=group_role)
    return token


# Les deux entrées d'acceptation, avec la MÊME intention : « <sub> accepte ».
def _via_token(sub, token, email):
    from oto_mcp import org_store
    return org_store.accept_invitation(token, sub)


def _via_signup(sub, token, email):
    """Le chemin SANS CLIC : la réconciliation par email au premier insert du user."""
    from oto_mcp import org_store
    return org_store.reconcile_signup_with_invitation(sub, email)


ENTREES = pytest.mark.parametrize(
    "accepte", [_via_token, _via_signup],
    ids=["lien mail", "signup sans clic"])


# ── Moitié 1 : une maison ÉTABLIE ne bouge pas ───────────────────────────────

@ENTREES
def test_une_invitation_acceptee_ne_deplace_pas_une_maison_reelle(live, accepte):
    """LE cas d'oto#161 : la personne a déjà une org réelle pour maison. Elle accepte
    une invitation pour une AUTRE org réelle → elle la rejoint, mais sa maison reste
    la sienne (sans quoi ses clés membre, rangées sous l'ancienne, deviennent
    injoignables sans que rien ne le dise)."""
    from oto_mcp import org_store
    sub = f"u-161-maison-{accepte.__name__}"
    email = f"{sub}@x.tld"
    maison = _org(f"Maison 161 {sub}", sub)
    assert org_store.get_active_org(sub) == maison
    invitante = _org(f"Invitante 161 {sub}", "u-161-emetteur")

    res = accepte(sub, _invitation(invitante, email), email)

    assert res and res["org_id"] == invitante
    # L'adhésion a bien eu lieu — le correctif ne coupe pas l'invitation.
    assert org_store.get_org_role(invitante, sub) == "org_member"
    # ... et la maison n'a pas bougé.
    assert org_store.get_active_org(sub) == maison, (
        "accepter une invitation a DÉPLACÉ la maison d'une org réelle établie "
        f"({maison}) vers l'org invitante ({invitante}) — oto#161")


def test_lecho_servi_annonce_la_maison_REELLE_pas_lorg_invitante(live):
    """Le contrat rendu par `org.invite.accept` porte `active_org` : lu APRÈS
    l'écriture, jamais recopié de l'invitation. Un écho qui annonce l'org rejointe
    comme maison est un accusé de réception faux — le front s'y fie pour router."""
    from oto_mcp import org_store
    from oto_mcp.capabilities.orgs import invites as cap
    sub = "u-161-echo"
    email = f"{sub}@x.tld"
    maison = _org("Maison 161 echo", sub)
    invitante = _org("Invitante 161 echo", "u-161-emetteur")
    token = _invitation(invitante, email)

    out = cap._invite_accept(SimpleNamespace(sub=sub), cap.InviteAcceptInput(token=token))

    assert out["org_id"] == invitante          # ce qu'il a rejoint
    assert out["active_org"] == maison, (      # où ses appels tombent VRAIMENT
        "l'écho annonce comme maison une org qui ne l'est pas — oto#161")


# ── Moitié 2 : à qui n'a PAS de maison, on en pose bien une ──────────────────

@ENTREES
def test_une_invitation_acceptee_pose_la_maison_a_qui_nen_a_pas(live, accepte):
    """L'autre moitié, indissociable : sans maison, l'org rejointe DOIT le devenir.
    C'est le cas majoritaire en prod (16 comptes nés dans leur org contre 7 déplacés) :
    un correctif qui casserait celui-ci serait pire que le défaut."""
    from oto_mcp import org_store
    sub = f"u-161-sans-maison-{accepte.__name__}"
    email = f"{sub}@x.tld"
    invitante = _org(f"Invitante 161 nue {sub}", "u-161-emetteur")
    assert org_store.get_active_org(sub) is None

    res = accepte(sub, _invitation(invitante, email), email)

    assert res and res["org_id"] == invitante
    assert org_store.get_active_org(sub) == invitante, (
        "l'invité SANS maison n'en a pas reçu — il retomberait sur un espace perso "
        "vide au lieu de son org")


@ENTREES
def test_lespace_perso_silencieux_cede_toujours_la_place(live, accepte):
    """Troisième cas de la règle d'`add_org_member`, conservé tel quel : la maison est
    l'espace perso créé d'office (ADR 0030/0033) → l'org réelle le PROMEUT. Sans lui,
    un invité atterrit sur sa perso vide (16 comptes en prod dans ce cas)."""
    from oto_mcp import org_store
    from oto_mcp.db import upsert_user
    sub = f"u-161-perso-{accepte.__name__}"
    email = f"{sub}@x.tld"
    upsert_user(sub, email=email)   # naissance du compte, exactement comme au signup
    perso = org_store.home_org(sub)
    assert perso and org_store.get_active_org(sub) == perso
    invitante = _org(f"Invitante 161 perso {sub}", "u-161-emetteur")

    accepte(sub, _invitation(invitante, email), email)

    assert org_store.get_active_org(sub) == invitante, (
        "la promotion perso → org réelle a été perdue")


# ── Palier ÉQUIPE : le même trou, par la bande ───────────────────────────────

def test_une_invitation_dequipe_ne_deplace_pas_la_maison_non_plus(live):
    """`set_active_group` pose l'org du groupe en maison (invariant ADR 0012). Une
    invitation d'ÉQUIPE acceptée par quelqu'un qui a déjà une maison réelle ne doit
    donc ni déplacer la maison, ni poser un groupe actif hors de celle-ci."""
    from oto_mcp import group_store, org_store
    sub = "u-161-equipe-maison"
    email = f"{sub}@x.tld"
    maison = _org("Maison 161 equipe", sub)
    invitante = _org("Invitante 161 equipe", "u-161-emetteur")
    gid = group_store.create_group(invitante, "Equipe 161", created_by="u-161-emetteur")
    token = _invitation(invitante, email, group_id=gid, group_role="group_member")

    res = org_store.accept_invitation(token, sub)

    assert res["group_id"] == gid
    assert group_store.get_group_role(gid, sub) == "group_member"  # l'équipe est rejointe
    assert org_store.get_active_org(sub) == maison, (
        "l'invitation d'ÉQUIPE a déplacé la maison par `set_active_group` — oto#161")
    assert group_store.get_active_group(sub) != gid, (
        "groupe actif posé hors de la maison : l'invariant ADR 0012 (groupe actif ⊂ "
        "org active) serait rompu")


def test_une_invitation_dequipe_pose_maison_et_equipe_a_qui_na_rien(live):
    """L'autre moitié au palier équipe : sans maison, l'invitation d'équipe pose bien
    l'org parente en maison ET l'équipe en groupe actif."""
    from oto_mcp import group_store, org_store
    sub = "u-161-equipe-nue"
    email = f"{sub}@x.tld"
    invitante = _org("Invitante 161 equipe nue", "u-161-emetteur")
    gid = group_store.create_group(invitante, "Equipe 161 nue", created_by="u-161-emetteur")
    assert org_store.get_active_org(sub) is None
    token = _invitation(invitante, email, group_id=gid, group_role="group_member")

    org_store.accept_invitation(token, sub)

    assert org_store.get_active_org(sub) == invitante
    assert group_store.get_active_group(sub) == gid
