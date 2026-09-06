"""Suspendre, changer un rôle, ajouter à un groupe : sur une adresse ambiguë.

C'est le coin de la classe où l'erreur ne se compte nulle part. Un partage raté
se voit dans la liste des partages ; un rôle posé sur le mauvais homonyme
ressemble à un rôle légitime, et aucun inventaire ne dit qu'on visait l'autre.
Une suspension frappe un compte qui n'a rien demandé pendant que celui qu'on
voulait arrêter continue.

Dix adresses portent deux comptes (mesuré le 05/09/2026), dont une paire sans
aucun tenant : ce n'est pas un effet de la qualification par émetteur (ADR 0052),
c'est une propriété de la résolution par adresse.

⚠️ Ces surfaces prennent `target` — un email OU un sub, indifféremment. La sortie
du refus existe donc déjà, sans changer aucun contrat : passer le `sub`.
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities import _identite
from oto_mcp.capabilities import users_admin as UA
from oto_mcp.capabilities.groups import members as GM
from oto_mcp.capabilities._types import AuthzDenied

DEUX = [{"sub": "u-nu-1"}, {"sub": "acme:u-tiers-1"}]


@pytest.fixture
def _annuaire(monkeypatch):
    def porteurs(email):
        return list(DEUX) if email == "double@x.fr" else (
            [{"sub": "u-seul", "email": email}] if email == "seule@x.fr" else [])
    monkeypatch.setattr(_identite.db, "get_users_by_email", porteurs)


# ── suspendre / changer un rôle ─────────────────────────────────────────────

def test_un_role_ne_se_pose_pas_sur_une_adresse_ambigue(_annuaire):
    with pytest.raises(AuthzDenied) as e:
        UA._resolve_target("double@x.fr")
    assert e.value.code == "ambiguous_email"
    assert "u-nu-1" in e.value.message and "acme:u-tiers-1" in e.value.message
    assert "`sub`" in e.value.message, "le refus nomme la sortie"


def test_le_sub_reste_la_sortie(_annuaire):
    """`target` accepte déjà un sub : refuser l'ambiguïté n'enferme personne."""
    assert UA._resolve_target("acme:u-tiers-1") == "acme:u-tiers-1"


def test_une_adresse_unique_passe_comme_avant(_annuaire):
    assert UA._resolve_target("seule@x.fr") == "u-seul"


def test_une_adresse_inconnue_garde_SON_code(_annuaire):
    """404 ici, 400 chez les groupes. Uniformiser serait un changement de
    contrat déguisé en refactorisation : le seam ne décide pas de l'absence."""
    with pytest.raises(AuthzDenied) as e:
        UA._resolve_target("personne@x.fr")
    assert (e.value.status, e.value.code) == (404, "unknown_user")


# ── ajouter / retirer un membre de groupe ───────────────────────────────────

def test_un_groupe_ne_s_ouvre_pas_sur_une_adresse_ambigue(_annuaire):
    with pytest.raises(AuthzDenied) as e:
        GM._resolve_target("double@x.fr")
    assert e.value.code == "ambiguous_email"


def test_le_groupe_garde_SON_code_pour_l_inconnu(_annuaire):
    with pytest.raises(AuthzDenied) as e:
        GM._resolve_target("personne@x.fr")
    assert (e.value.status, e.value.code) == (400, "unknown_user")


def test_le_groupe_laisse_passer_une_adresse_unique(_annuaire):
    assert GM._resolve_target("seule@x.fr") == "u-seul"


# ── le domicile ─────────────────────────────────────────────────────────────

def test_les_deux_surfaces_partagent_le_MEME_refus(_annuaire):
    """Sinon la garde se recopie, et la septième copie sera celle qui manque."""
    a = pytest.raises(AuthzDenied)
    with a as ea:
        UA._resolve_target("double@x.fr")
    with pytest.raises(AuthzDenied) as eb:
        GM._resolve_target("double@x.fr")
    assert ea.value.message == eb.value.message
