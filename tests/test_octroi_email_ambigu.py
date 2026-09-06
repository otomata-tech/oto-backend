"""Accorder — et RÉVOQUER — l'accès à son compte connecteur sur une adresse ambiguë.

Une adresse ne désigne pas un compte : dix adresses en portent deux (mesuré le
05/09/2026), dont une paire sans aucun tenant. En choisir un en silence, c'est :

- à l'octroi, **ouvrir son compte connecteur au mauvais destinataire** ;
- à la révocation, **croire avoir retiré l'accès** alors qu'il reste au second.

Le second cas est le plus traître : le propriétaire fait un geste explicite de
retrait, reçoit un succès, et l'accès demeure.

`grantee` accepte DÉJÀ un sub — le refus a donc une sortie immédiate, et il la
nomme. Aucun changement de contrat n'était nécessaire ici, contrairement au
partage de tableau.
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities._types import AuthzDenied
from oto_mcp.capabilities.connectors import account_grants as AG


@pytest.fixture
def _annuaire(monkeypatch):
    par_email = {
        "seul@x.fr": [{"sub": "u-seul"}],
        "double@x.fr": [{"sub": "u-nu-1"}, {"sub": "acme:u-tiers-1"}],
        "inconnu@x.fr": [],
    }
    monkeypatch.setattr(AG.db, "get_users_by_email",
                        lambda e: list(par_email.get(e, [])))
    return par_email


def test_une_adresse_a_un_porteur_se_resout(_annuaire):
    assert AG._un_seul_porteur("seul@x.fr") == {"sub": "u-seul"}


def test_une_adresse_sans_porteur_rend_None_et_laisse_decider(_annuaire):
    """L'octroi lèvera 404 ; la révocation, elle, tolère et retombe sur la chaîne
    fournie — deux sémantiques légitimes, tranchées par l'appelant."""
    assert AG._un_seul_porteur("inconnu@x.fr") is None


def test_deux_porteurs_REFUSENT_et_le_refus_est_actionnable(_annuaire):
    with pytest.raises(AuthzDenied) as e:
        AG._un_seul_porteur("double@x.fr")
    assert e.value.code == "ambiguous_email"
    assert "u-nu-1" in e.value.message
    assert "acme:u-tiers-1" in e.value.message
    assert "grantee" in e.value.message, "le refus nomme la sortie, pas que le manque"


def test_l_octroi_refuse_avant_de_toucher_au_moindre_droit(_annuaire, monkeypatch):
    """Le refus tombe à la RÉSOLUTION : rien n'est écrit, même pas tenté."""
    monkeypatch.setattr(AG.db, "set_account_grant",
                        lambda *a, **k: pytest.fail("un droit a été accordé"))
    with pytest.raises(AuthzDenied) as e:
        AG._resolve_grantee(_ctx(), "double@x.fr")
    assert e.value.code == "ambiguous_email"


def test_la_REVOCATION_refuse_aussi_et_c_est_le_cas_traitre(_annuaire):
    """Révoquer « un des deux » rend un succès et laisse l'accès au second. Le
    propriétaire croit avoir fermé la porte."""
    import inspect
    src = inspect.getsource(AG._revoke)
    assert "_un_seul_porteur(" in src
    assert "get_user_by_email(" not in src


def test_un_sub_ne_consulte_pas_l_annuaire(monkeypatch):
    monkeypatch.setattr(AG.db, "get_users_by_email",
                        lambda e: pytest.fail("l'annuaire ne doit pas être lu"))
    monkeypatch.setattr(AG.db, "get_user", lambda s: {"sub": s})
    assert AG._resolve_grantee(_ctx(), "acme:u-tiers-1")["sub"] == "acme:u-tiers-1"


def test_aucune_resolution_par_adresse_ne_prend_le_premier_venu():
    """La classe : dans CE module, plus aucun chemin ne lit `get_user_by_email`,
    qui rend `fetchone()` dans un ordre que rien ne fixe."""
    import pathlib
    src = pathlib.Path(AG.__file__).read_text()
    corps = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "get_user_by_email(" not in corps


def _ctx():
    from oto_mcp.capabilities._types import ResolvedCtx
    return ResolvedCtx(sub="le-proprietaire", org_id=2)
