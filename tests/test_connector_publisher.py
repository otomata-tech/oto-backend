"""Tout connecteur nomme son éditeur — et aucun défaut ne parle à sa place.

Tranché par Alexis le 2026-09-02. `publisher_name` retombait sur « Otomata » quand le
module de déclaration ne portait pas de constante `PUBLISHER` : **le défaut parlait donc
à la place d'une déclaration absente**, et il mentait. Le MCP *officiel* de Folk a été
servi sous notre nom du 02/08 au 02/09 ; six canaux de messagerie envoyaient des messages
par une passerelle tierce, sous notre nom aussi.

    Une absence se voit et se corrige ; une attribution fausse se croit.

Le repli est retiré (`Connector.publisher_name` rend `""`). Ce fichier est le cliquet qui
empêche le trou de revenir : un connecteur qui n'a pas d'éditeur déclaré rougit ici, avant
d'atteindre une fiche.

⚠️ **Il juge TOUT le registre, sans filtre de famille — et un test le prouve.** C'est
exactement le filtre `kind == "tools"` de `test_connector_logos.py` qui a laissé `folkmcp`
sans éditeur pendant un mois : le contrôle existait, il ne regardait simplement pas la
famille où le défaut vivait. Du point de vue de la fiche, un mount est un connecteur comme
un autre.
"""
from __future__ import annotations

import dataclasses

import pytest

from oto_mcp import providers
from oto_mcp.providers._model import _c


def _sans_editeur() -> set[str]:
    """Les connecteurs dont la fiche n'afficherait aucun éditeur — TOUT le registre.

    On lit la valeur **SERVIE** (`publisher_name`), pas la constante du module : un
    éditeur se déclare par DEUX chemins — la constante `PUBLISHER` du module, et le
    champ `publisher` de l'entrée (utilisé quand l'éditeur est une propriété partagée
    par plusieurs entrées d'une même factory, cf. `unipile.channel`). Un cliquet qui ne
    regarderait que le premier serait aveugle aux six canaux hébergés.
    """
    return {n for n, c in providers.REGISTRY.items() if not c.publisher_name}


def test_tout_connecteur_declare_son_editeur():
    """DIRECTION 1 — l'oubli. Sans repli, une omission ne s'affiche plus « Otomata » :
    elle s'affiche vide. C'est déjà mieux qu'un mensonge, mais une fiche muette reste
    une fiche à corriger — alors on la refuse à la CI, pas en production."""
    muets = sorted(_sans_editeur())
    assert not muets, (
        f"{muets} : connecteur sans éditeur déclaré. Ajoute `PUBLISHER = \"…\"` dans "
        "`oto_mcp/providers/<nom>.py`. La question qui donne la valeur est « à qui "
        "l'appel arrive-t-il ? » — une passerelle tierce se nomme (reddit → "
        "`redditapis.com`), un service qu'on écrit aussi (planity → `Otomata`). Ce "
        "n'est PAS « la marque est-elle la bonne ? » : cette question-là n'attrape "
        "aucune des trois fautes du 2026-09-02. Cf. `docs/connector-vault.md` "
        "§« Ce que la fiche DIT ».")


def test_les_editeurs_declares_visent_un_connecteur_reel():
    """DIRECTION 2 — l'entrée morte. Un éditeur keyé sur un nom qui n'existe plus ne
    sert rien et fait croire à une couverture qu'on n'a pas. Même paire de directions
    que `test_connector_logos.py` : l'oubli, puis la déclaration périmée."""
    fantomes = sorted(set(providers._PUBLISHER_BY_CONNECTOR) - set(providers.REGISTRY))
    assert not fantomes, f"{fantomes} : éditeur déclaré sans connecteur correspondant."


_FAMILLES = sorted({c.kind for c in providers.REGISTRY.values()})


def test_le_registre_porte_bien_plusieurs_familles():
    """Sans ça, le paramétrage ci-dessous serait vrai par vacuité."""
    assert len(_FAMILLES) > 1, _FAMILLES


@pytest.mark.parametrize("famille", _FAMILLES)
def test_le_controle_voit_chaque_famille(famille, monkeypatch):
    """LE PIÈGE DÉJÀ PAYÉ, figé — et prouvé par MUTATION, pas par relecture.

    `test_connector_logos.py` a filtré `kind == "tools"` du 2026-08-02 au 2026-09-02 :
    le contrôle tournait vert en ne jugeant AUCUN connecteur fédéré, et c'est là que le
    trou vivait (`folkmcp`). Un test qui se contenterait de recalculer la population
    serait aveugle au même défaut : il faut exercer `_sans_editeur` LUI-MÊME.

    Alors on rend muet, dans chaque famille du registre, un connecteur réel — et on
    exige que le contrôle le voie. Un filtre glissé dans `_sans_editeur` fait rougir ce
    test pour la famille qu'il exclut. Paramétré sur les familles PRÉSENTES : une
    famille ajoutée demain est couverte sans que personne y pense."""
    cible = next(n for n, c in providers.REGISTRY.items() if c.kind == famille)
    monkeypatch.setitem(providers.REGISTRY, cible,
                        dataclasses.replace(providers.REGISTRY[cible], publisher=""))
    monkeypatch.delitem(providers._PUBLISHER_BY_CONNECTOR, cible, raising=False)
    assert providers.REGISTRY[cible].publisher_name == "", "la mutation n'a pas pris"
    assert cible in _sans_editeur(), (
        f"le contrôle ne voit pas la famille {famille!r} (essayé sur {cible!r}) — "
        "un filtre s'est glissé dans `_sans_editeur`.")


def test_sans_declaration_le_champ_servi_est_vide_jamais_otomata():
    """LA RÈGLE elle-même, exercée sur une entrée qui n'a NI champ NI constante.

    C'est le comportement qui protège ce que le cliquet ci-dessus ne voit pas : une
    entrée construite hors du registre (un futur registre dynamique, un test, un
    connecteur d'org). Le défaut ne doit rien inventer."""
    orphelin = _c("connecteur-sans-domicile", ["fantome"])
    assert orphelin.publisher_name == ""
    assert orphelin.publisher_name != "Otomata"


def test_les_cas_connus_du_2026_09_02():
    """CAS À RÉPONSE CONNUE — les fiches qui ont réellement menti, et les trois maison.

    Rejoué sur les valeurs servies : c'est ce que l'utilisateur reçoit, pas ce que le
    source déclare. Les deux fautes vont dans des sens OPPOSÉS et une seule relecture
    les confond — `folkmcp` se créditait à nous, `planity` créditait un tiers de ce
    qu'on écrit et opère nous-mêmes."""
    attendus = {
        # Corrigées à la main le 2026-09-02 (PR #834), figées ici.
        # ⚠️ `folkmcp` et `atlassian` étaient ICI jusqu'au 2026-09-09 : partis avec
        # la fédération MCP (ADR 0069). Le cas d'école qu'ils portaient — un DÉFAUT
        # qui parle à la place d'une déclaration absente, et sert le produit d'un
        # tiers sous notre nom — reste décrit dans `docs/connector-vault.md` ; la
        # garde, elle, n'a pas de filtre de famille et couvre tout le registre.
        "planity": "Otomata",           # le connecteur est le NÔTRE (natif depuis #913)
        # Les six canaux hébergés : le message part chez Unipile, qui détient la
        # session du compte opéré. Déclaré une seule fois, chez le porteur de la clé.
        "linkedin_unipile": "Unipile",
        "whatsapp": "Unipile",
        "telegram": "Unipile",
        "instagram": "Unipile",
        # Légitimement nôtres : on les a écrits ET c'est nous qui recevons l'appel.
        # Ils DÉCLARENT « Otomata » au lieu d'y retomber — la valeur est la même, la
        # différence est qu'elle est désormais un choix relisible.
        "browser": "Otomata",
        "web": "Otomata",
        "http": "Otomata",
    }
    servis = {n: providers.REGISTRY[n].publisher_name for n in attendus}
    assert servis == attendus


def test_reddit_nomme_la_passerelle_pas_la_marque():
    """Le cas qui a fixé la définition : l'API de Reddit est fermée en self-serve, le
    connecteur tape un revendeur. La fiche disait « Reddit » — on créditait une marque
    d'un service qu'elle ne rend pas, et l'intermédiaire disparaissait."""
    assert "redditapis.com" in providers.REGISTRY["reddit"].publisher_name
