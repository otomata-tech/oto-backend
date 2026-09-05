"""La doc REST se range par RESSOURCE, pas par portée d'autorisation (oto-backend#330).

Le tag d'une opération valait le premier segment de la clé de capacité — `me`, `org`,
`admin` — c'est-à-dire QUI a le droit, pas SUR QUOI. Pour qui découvre l'API, les
routes d'un même objet étaient éparpillées entre deux sections selon qu'on les atteint
pour soi ou pour son org : il fallait connaître notre modèle de droits pour trouver
« les routes des tableaux ».

⚠️ Les clés PLATES gardent leur portée pour section, et c'est voulu : leur second
segment est un verbe (`admin.instance_health`), en faire une section produirait une
section par action — pire que le classement qu'on remplace.
"""
from __future__ import annotations

from oto_mcp.openapi import _section


def test_la_meme_ressource_sous_deux_portees_donne_UNE_section():
    """Le cœur du lot : `me.datastore.*` et `org.datastore.*` se retrouvent."""
    assert _section("me.datastore.append_row") == "datastore"
    assert _section("org.datastore.rows") == "datastore"


def test_une_cle_PLATE_garde_sa_portee_pour_section():
    """Son second segment est un verbe, pas une ressource — 31 des 34 capacités
    `admin` sont dans ce cas, et une section par action serait pire que l'existant."""
    assert _section("admin.instance_health") == "admin"
    assert _section("me.profile") == "me"


def test_une_cle_PROFONDE_prend_quand_meme_sa_ressource():
    """Quatre segments existent (7 capacités) : la ressource reste le deuxième."""
    assert _section("me.datastore.rows.claim") == "datastore"


def test_aucune_cle_ne_produit_de_section_vide():
    """Une section vide ferait disparaître l'opération de la table des matières —
    elle serait servie et introuvable."""
    for cle in ("", ".", "me", "me.", "..x.."):
        assert _section(cle).strip(), f"section vide pour {cle!r}"


def test_le_DOCUMENT_servi_regroupe_bien_les_routes_d_un_objet():
    """⚠️ Le banc qui éprouve le CHEMIN, pas la fonction — la leçon du jour.

    Les autres appellent `_section` directement ; celui-ci lit le document réellement
    construit, celui qu'un intégrateur télécharge. C'est le seul qui verrait la
    fonction juste et le document faux.

    Mesuré avant/après (05/09/2026) : la doc passe de **11 sections à 42** pour les
    mêmes 206 opérations. C'est plus que « ranger par ressource » — ⚠️ **11 sections
    n'ont qu'une seule opération**, et c'est le prix assumé : elles correspondent à des
    ressources qui n'ont qu'une route, là où l'ancien classement les noyait dans un
    `me` de 18 opérations. Le gain visé est net de son côté : les 25 routes du
    datastore sont réunies au lieu d'être coupées entre `me` et `org` selon qu'on les
    atteint pour soi ou pour son org."""
    from oto_mcp.openapi import build

    doc = build()
    par_section: dict = {}
    for item in (doc.get("paths") or {}).values():
        for op in item.values():
            if isinstance(op, dict):
                for t in op.get("tags") or []:
                    par_section.setdefault(t, 0)
                    par_section[t] += 1

    assert par_section.get("datastore", 0) >= 20, (
        "les routes du datastore doivent être réunies sous une section, pas "
        f"réparties par portée : {par_section}")
    # ⚠️ Et la plus grosse section ne doit plus être une PORTÉE. C'est là qu'on lit
    # le renversement : avant, `me` dominait parce qu'il ramassait tout ce qu'une
    # personne fait pour elle-même, quel que soit l'objet.
    #
    # `me` et `org` ne disparaissent pas et ne doivent pas : les clés PLATES
    # (`org.update`, `me.profile`) n'ont pas de segment de ressource, leur portée est
    # ce qu'on a de plus juste. Ce qui compte est qu'elles ne dominent plus.
    plus_grosse = max(par_section.items(), key=lambda kv: kv[1])[0]
    assert plus_grosse == "datastore", (
        f"la plus grosse section est `{plus_grosse}` — si c'est une portée, le "
        f"premier segment a repris la main : {par_section}")


def test_toutes_les_capacites_servies_ont_une_section_non_vide():
    """Sur le registre RÉEL, pas sur des exemples choisis : c'est la seule façon de
    voir une clé dont la forme n'avait pas été prévue."""
    from oto_mcp.capabilities.registry import CAPABILITIES

    servies = [c for c in CAPABILITIES if getattr(c, "rest", None)]
    assert servies, "aucune capacité REST — le banc ne mesurerait rien"
    for cap in servies:
        assert _section(cap.key).strip(), cap.key
