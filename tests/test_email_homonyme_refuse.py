"""Une adresse ne désigne pas un compte — et le silence coûtait plus qu'un zéro.

⚠️ Mesuré le 05/09/2026 sur la production : une même adresse personnelle porte DEUX
comptes, le nôtre (un sub nu) et celui d'un tenant tiers (un sub qualifié
`<tenant>:…`), qualifiés par émetteur (ADR 0052). Sur trente jours, ils
ont respectivement 91 et 98 appels.

Filtrer le monitoring par cette adresse rendait **91**, et taisait les 98 autres.
Pas un zéro — un **chiffre plausible**. Un zéro fait douter ; un nombre qui
ressemble à une réponse fait conclure, et personne ne va vérifier.

Et la même résolution sert `oto_admin_account op=suspend`, `set_role`, l'ajout de
membres : on y jouait à pile ou face entre deux homonymes, sur des gestes qui
engagent.
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities._types import AuthzDenied
from oto_mcp.capabilities.orgs import members as M


@pytest.fixture
def _annuaire(monkeypatch):
    """Ce que la base rend pour une adresse — la vraie forme du défaut."""
    par_email = {
        "seul@exemple.fr": [{"sub": "u-seul"}],
        "double@exemple.fr": [{"sub": "u-nu-1"},
                              {"sub": "acme:u-tiers-1"}],
        "inconnu@exemple.fr": [],
    }
    monkeypatch.setattr(M.db, "get_users_by_email",
                        lambda e: list(par_email.get(e, [])))
    return par_email


def test_une_adresse_a_un_seul_porteur_se_resout(_annuaire):
    assert M._resolve_target("seul@exemple.fr") == "u-seul"


def test_un_sub_passe_tel_quel_sans_toucher_a_l_annuaire(monkeypatch):
    """Le chemin sans ambiguïté possible ne doit pas coûter une lecture."""
    monkeypatch.setattr(M.db, "get_users_by_email",
                        lambda e: pytest.fail("l'annuaire ne doit pas être lu"))
    assert M._resolve_target("acme:u-tiers-1") == "acme:u-tiers-1"


def test_une_adresse_inconnue_est_refusee_en_le_disant(_annuaire):
    with pytest.raises(AuthzDenied) as e:
        M._resolve_target("inconnu@exemple.fr")
    assert e.value.code == "unknown_user"


# ── le défaut : deux porteurs ────────────────────────────────────────────────

def test_deux_porteurs_REFUSENT_au_lieu_d_en_choisir_un(_annuaire):
    with pytest.raises(AuthzDenied) as e:
        M._resolve_target("double@exemple.fr")
    assert e.value.code == "ambiguous_email"


def test_le_refus_NOMME_les_candidats_pour_etre_actionnable(_annuaire):
    """Un refus qui dit « ambigu » sans dire entre quoi oblige à aller chercher
    ailleurs — et l'appelant n'a souvent pas la surface pour le faire."""
    with pytest.raises(AuthzDenied) as e:
        M._resolve_target("double@exemple.fr")
    assert "u-nu-1" in e.value.message
    assert "acme:u-tiers-1" in e.value.message
    assert "sub" in e.value.message, "le refus dit AVEC QUOI reprendre"


def test_le_refus_ne_choisit_jamais_le_premier_meme_en_silence(_annuaire):
    """L'épreuve inverse : si quelqu'un « répare » en rendant le premier porteur,
    ce banc tombe. C'est exactement l'état d'avant — et il rendait un résultat
    d'apparence normale, ce qui est la raison pour laquelle il a duré."""
    try:
        rendu = M._resolve_target("double@exemple.fr")
    except AuthzDenied:
        return
    pytest.fail(f"a rendu `{rendu}` au lieu de refuser : le silence est revenu")


def test_la_resolution_lit_TOUS_les_porteurs_pas_le_premier():
    """La classe : `get_user_by_email` rend `fetchone()`, dans un ordre que rien
    ne fixe. Une résolution qui DÉCIDE ne doit pas s'en servir."""
    import inspect
    src = inspect.getsource(M._resolve_target)
    assert "get_users_by_email" in src
    assert "get_user_by_email(" not in src
