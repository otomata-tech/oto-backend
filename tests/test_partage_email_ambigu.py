"""Partager sur une adresse ambiguë : le seul endroit de la classe qui donne accès
à des DONNÉES.

Dix adresses portent deux comptes (mesuré le 05/09/2026), dont une paire sans
aucun tenant. Partager sur l'une d'elles donnait l'accès à l'un des deux, choisi
par l'ordre que rend la base — et rien ne le disait au propriétaire.

Ailleurs dans cette classe, une erreur fausse un compteur ou vise le mauvais
compte pour une préférence. Ici, elle ouvre un tableau, un projet ou une doctrine
à quelqu'un qu'on ne visait pas.

⚠️ Le refus DOIT avoir une issue, sinon c'est une panne : les dix adresses
deviendraient impartageables. D'où `sub` — sur la surface v2, la seule dont le
contrat d'entrée peut évoluer (celui de l'héritée est un cliquet).
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities import resources as R
from oto_mcp.capabilities._types import AuthzDenied
from oto_mcp.capabilities.datastore import sharing as DS


@pytest.fixture
def _annuaire(monkeypatch):
    porteurs = {
        "seule@x.fr": [{"sub": "u-seul", "email": "seule@x.fr"}],
        "double@x.fr": [{"sub": "8ugqeq6cv40f"}, {"sub": "tulina:f3s740z39vfq"}],
    }
    for mod in (R, DS):
        monkeypatch.setattr(mod.db, "get_users_by_email",
                            lambda e: list(porteurs.get(e, [])))
        monkeypatch.setattr(mod.db, "get_user",
                            lambda s: {"sub": s, "email": "x@y.z"} if s.startswith(("u-", "8", "tulina")) else None)
    return porteurs


# ── le tableau ───────────────────────────────────────────────────────────────

def test_un_tableau_ne_se_partage_pas_sur_une_adresse_ambigue(_annuaire):
    with pytest.raises(AuthzDenied) as e:
        DS._destinataire("double@x.fr", "")
    assert e.value.code == "ambiguous_email"
    assert "8ugqeq6cv40f" in e.value.message and "tulina:f3s740z39vfq" in e.value.message
    assert "`sub`" in e.value.message, "le refus nomme la sortie"


def test_le_sub_est_la_sortie(_annuaire):
    assert DS._destinataire("", "tulina:f3s740z39vfq")["sub"] == "tulina:f3s740z39vfq"


def test_les_deux_ensemble_sont_refuses(_annuaire):
    """Ils peuvent désigner des comptes différents, et rien ne dirait lequel a
    servi — un partage réussi vers une cible qu'on ne sait pas nommer."""
    with pytest.raises(AuthzDenied) as e:
        DS._destinataire("seule@x.fr", "u-seul")
    assert e.value.code == "email_and_sub"


def test_une_adresse_unique_passe_comme_avant(_annuaire):
    assert DS._destinataire("seule@x.fr", "")["sub"] == "u-seul"


# ── la ressource générique : projets et doctrines ───────────────────────────

def test_un_projet_non_plus(_annuaire):
    with pytest.raises(AuthzDenied) as e:
        R._resolve_recipient("double@x.fr")
    assert e.value.code == "ambiguous_email"


def test_le_refus_nomme_la_sortie_SUR_LA_MEME_SURFACE(_annuaire):
    """⚠️ Aller-retour assumé : `sub` a d'abord été posé sur `oto_resource_v2`,
    parce que le schéma servi de l'héritée est gelé — et le refus renvoyait donc
    vers un AUTRE outil. C'était contraire à la décision d'Alexis du 04/09 (ADR
    0068, « pas de v2 ») : on corrige l'outil que les gens utilisent.

    Le cliquet n'interdit pas d'ajouter — il protège d'une rupture qui FAIT
    ÉCHOUER un appel (un champ rendu obligatoire). Un champ optionnel ne casse
    aucun appelant, et l'empreinte se regrave dans le même commit.

    Un refus qui renvoie vers un autre outil est une issue plus étroite qu'un
    refus qu'on peut lever là où on est."""
    with pytest.raises(AuthzDenied) as e:
        R._resolve_recipient("double@x.fr")
    assert "`sub`" in e.value.message
    assert "v2" not in e.value.message, "la sortie est sur la surface qu'on utilise"
    assert "sub" in R.ResourceInput.model_fields


def test_la_surface_v2_porte_le_champ_et_le_DECLARE():
    """Un champ qu'aucun texte servi n'annonce n'existe pas pour un agent."""
    from oto_mcp.capabilities.resources_v2 import ResourceInputV2
    champ = ResourceInputV2.model_fields["sub"]
    assert champ.description and "plusieurs" in champ.description


# ── ce que le lot ne fait PAS ────────────────────────────────────────────────

def test_rien_de_ce_qui_est_deja_partage_n_est_touche():
    """On ferme le chemin, on ne révoque pas : les partages en place restent, y
    compris ceux posés sur une adresse ambiguë. Les défaire serait une décision
    d'Alexis, pas un effet de bord d'un correctif."""
    import inspect
    for source in (inspect.getsource(DS), inspect.getsource(R._resolve_recipient)):
        assert "revoke" not in source or "ownership.revoke" in source
