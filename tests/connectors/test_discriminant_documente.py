"""Cliquet : ce que le contrat DIT du discriminant est ce que le serveur FAIT.

`AuthDescriptor.field_discriminator`, `CredentialField.when` et `.choices` pilotent le
formulaire de pose de clé du dashboard produit. Jusqu'au 2026-09-01 leurs règles
n'existaient qu'en commentaires Python — invisibles au document servi, puisqu'un `#` ne
devient pas une `description`. Le front a donc implémenté « la seule lecture cohérente
possible » et a eu raison par déduction, pas parce qu'on le lui avait dit.

La règle qui se déduisait mal, et qui est la plus coûteuse à rater :

    discriminant DÉCLARÉ mais valeur PAS ENCORE CHOISIE ⟹ tous les champs restent
    pertinents.

Un front qui filtre dès l'ouverture du formulaire cache des champs que la pose exigera,
et l'utilisateur ne voit jamais ce qu'on lui reproche de ne pas avoir rempli.

⚠️ **Ce fichier vérifie les deux moitiés ensemble, et c'est là son intérêt** : que la
description existe, et que le comportement qu'elle promet soit celui du code. Prise
seule, la première moitié laisserait le contrat mentir tant que la phrase reste
présente ; prise seule, la seconde laisserait le client redevenir aveugle.

Éprouvé rouge le 2026-09-01 avant d'être posé : description retirée ⟹ le premier test
nomme le champ ; `fields_for` rendu filtrant sur discriminant vide ⟹ le second.
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities.connectors.catalog_card import AuthDescriptor, CredentialField
from oto_mcp.providers import REGISTRY
from oto_mcp.providers.api import CONNECTOR as HTTP


@pytest.mark.parametrize("modele, champ", [
    (AuthDescriptor, "field_discriminator"),
    (CredentialField, "when"),
    (CredentialField, "choices"),
])
def test_le_champ_porte_une_description_servie(modele, champ):
    """Un commentaire `#` ne voyage pas : seul `Field(description=…)` atteint le
    document. Ces trois champs-là ne peuvent pas se passer d'explication — leur valeur
    vide veut dire « tout », et c'est le contraire de ce qu'on lit spontanément."""
    schema = modele.model_json_schema()["properties"][champ]
    description = (schema.get("description") or "").strip()
    assert description, f"{modele.__name__}.{champ} est servi sans description"


def test_le_discriminant_sans_valeur_ne_masque_rien():
    """La règle que la description promet, sur le seul connecteur qui déclare un
    discriminant. Sans elle, un formulaire s'ouvrirait amputé des champs que la pose
    exige — et rien n'échouerait avant l'écriture."""
    assert HTTP.field_discriminator, "banc caduc : http ne discrimine plus"
    assert HTTP.fields_for({}) == HTTP.secret_fields
    assert HTTP.fields_for({HTTP.field_discriminator: ""}) == HTTP.secret_fields
    assert HTTP.fields_for({HTTP.field_discriminator: "   "}) == HTTP.secret_fields


def test_un_when_vide_veut_dire_toujours():
    """L'autre moitié de la même règle, sur les ~90 connecteurs sans discriminant :
    aucun de leurs champs ne porte de `when`, et tous restent pertinents quoi qu'on
    passe. Lire « vide » comme « jamais » les ferait tous disparaître."""
    sans_discriminant = [c for c in REGISTRY.values()
                         if not c.field_discriminator and c.secret_fields]
    assert sans_discriminant, "banc caduc : plus aucun connecteur sans discriminant"
    for connecteur in sans_discriminant:
        assert all(not f.when for f in connecteur.secret_fields), connecteur.name
        assert connecteur.fields_for({"auth_mode": "peu_importe"}) == \
            connecteur.secret_fields, connecteur.name
