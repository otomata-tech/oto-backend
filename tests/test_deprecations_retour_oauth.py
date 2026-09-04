"""Le préavis du retour OAuth unifié (oto-backend#670) — un SECOND couple daté,
INDÉPENDANT de celui du renommage doctrine→guide (`ANNONCE`/`RETRAIT`) : ce lot n'a
pas encore été tagué en production, donc sa date d'annonce ne peut pas être posée
ici (voir `deprecations.ANNONCE_RETOUR_OAUTH` et `docs/alias-deprecies.md`).
"""
from __future__ import annotations

import datetime

from oto_mcp import deprecations as d


def test_le_couple_retour_oauth_reutilise_preavis_mois_sans_reprendre_annonce():
    """Même durée contractuelle (Art 8.2) que le renommage doctrine→guide — mais
    PAS la même date d'annonce : ce lot n'a rien à voir avec le tag `v1.159.0`."""
    assert d.PREAVIS_MOIS >= 2
    assert d.ANNONCE_RETOUR_OAUTH is None or d.ANNONCE_RETOUR_OAUTH != d.ANNONCE


def test_annonce_non_posee_double_toujours():
    """Tant que `ANNONCE_RETOUR_OAUTH` reste `None` (ce lot n'est pas encore tagué
    en production), le doublage ne s'arrête JAMAIS tout seul : il n'y a pas de date
    fantôme à comparer, donc pas de fenêtre qui se ferme avant qu'Alexis/le
    superviseur ne l'ait ouverte au tag."""
    assert d.ANNONCE_RETOUR_OAUTH is None
    assert d.RETRAIT_RETOUR_OAUTH is None
    assert d.dans_le_preavis_retour_oauth() is True
    assert d.dans_le_preavis_retour_oauth(datetime.date(2099, 1, 1)) is True


def test_une_fois_lannonce_posee_le_meme_preavis_sapplique(monkeypatch):
    """Le jour où `ANNONCE_RETOUR_OAUTH` sera posée (au tag qui déploie ce lot), le
    mécanisme redevient celui de `RETRAIT` : deux mois CALENDAIRES, la même
    constante `PREAVIS_MOIS` que l'Art 8.2 impose déjà au renommage doctrine→guide."""
    annonce = datetime.date(2026, 9, 4)
    retrait = d._plus_de_mois(annonce, d.PREAVIS_MOIS)
    monkeypatch.setattr(d, "ANNONCE_RETOUR_OAUTH", annonce)
    monkeypatch.setattr(d, "RETRAIT_RETOUR_OAUTH", retrait)

    assert d.dans_le_preavis_retour_oauth(annonce) is True
    assert d.dans_le_preavis_retour_oauth(retrait - datetime.timedelta(days=1)) is True
    assert d.dans_le_preavis_retour_oauth(retrait) is False
    assert d.dans_le_preavis_retour_oauth(retrait + datetime.timedelta(days=1)) is False
