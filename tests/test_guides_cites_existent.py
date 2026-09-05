"""CLIQUET : un guide cité par une description SERVIE doit exister (oto#58).

Ce que ça attrape, et pourquoi ça vaut un test plutôt qu'une relecture : la
description d'un outil renvoyait vers un guide dédié, nommé, pour une opération de
chargement lourde — **et ce guide n'existait pas**. Ni en lecture par son identifiant,
ni dans la liste. Découvert par un agent le 04/09/2026 (signal 718), qui a payé la
recherche avant de constater le vide.

Personne n'avait rien cassé : le guide a été renommé ou retiré, et la description qui le
nomme n'a pas bougé avec lui. **Rien ne pouvait le signaler** — un renvoi mort ne lève
pas, il envoie chercher.

⚠️ La classe est là, pas dans ce cas : ce test vise **l'axe** (tout renvoi est vérifié),
pas la forme sous laquelle le défaut s'est présenté. Un second guide retiré demain
rougira ici avant d'être servi à quelqu'un.

## Ce qui est vérifié, et contre quoi

Les descriptions **telles que le serveur les sert** (registre FastMCP monté), plus les
corps des guides eux-mêmes — un guide qui renvoie vers un guide disparu coûte autant.
La référence est le dossier des SEEDS : un guide cité par une surface de plateforme est
du contenu de plateforme, donc il est semé. Un guide qui ne vivrait qu'en base ne serait
pas servi aux orgs qui n'y ont pas droit, et la description, elle, est servie à tout le
monde.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

GUIDES_DIR = pathlib.Path(__file__).resolve().parents[1] / "oto_mcp" / "guides"

#: « guide `slug` », « guides `slug` », « Guide dédié : `slug` ». Le mot `guide` doit
#: précéder : sans lui, tout identifiant entre accents graves serait candidat.
_CITATION = re.compile(
    r"guides?\s*(?:dédiés?|dedies?|nommés?)?\s*[:=]?\s*`([a-z][a-z0-9-]{2,})`",
    re.IGNORECASE)

#: Ce que le motif attrape et qui n'est PAS un slug de guide. Nommé, jamais toléré en
#: silence : une exception muette rendrait ce cliquet inutile le jour où il compte.
_PAS_DES_SLUGS = {
    # `delivery` d'un guide : « un guide `on-demand` porte titre/description ».
    "on-demand", "init",
}


def _slugs_semes() -> set[str]:
    return {p.stem for p in GUIDES_DIR.glob("*.md")}


def _cites(texte: str) -> set[str]:
    return {m.group(1).lower() for m in _CITATION.finditer(texte or "")} - _PAS_DES_SLUGS


def _descriptions_servies() -> dict[str, str]:
    from fastmcp import FastMCP

    from oto_mcp.tools import register_all
    m = FastMCP("t")
    register_all(m)
    return {t.name: (t.description or "") for t in asyncio.run(m._list_tools())}


def test_tout_guide_cite_par_un_outil_servi_existe():
    semes = _slugs_semes()
    manquants: dict[str, set[str]] = {}
    for nom, description in _descriptions_servies().items():
        absents = _cites(description) - semes
        if absents:
            manquants[nom] = absents
    assert not manquants, (
        "des outils renvoient vers des guides qui n'existent pas : "
        + "; ".join(f"{outil} → {sorted(slugs)}" for outil, slugs in sorted(manquants.items()))
        + f". Guides semés : {sorted(semes)}. Un renvoi mort ne lève pas, il envoie "
          "chercher — soit le guide est rétabli, soit son contenu est replié dans la "
          "description, soit le renvoi part.")


def test_tout_guide_cite_par_un_guide_existe():
    """Un guide qui renvoie vers un guide disparu coûte autant qu'une description."""
    semes = _slugs_semes()
    manquants: dict[str, set[str]] = {}
    for p in sorted(GUIDES_DIR.glob("*.md")):
        absents = _cites(p.read_text(encoding="utf-8")) - semes
        if absents:
            manquants[p.name] = absents
    assert not manquants, (
        "des guides renvoient vers des guides inexistants : "
        + "; ".join(f"{g} → {sorted(s)}" for g, s in sorted(manquants.items())))


def test_le_cliquet_verrait_un_renvoi_mort():
    """Une garde qui ne sait pas rougir ne garde rien : on lui montre le cas exact du
    04/09 (un slug plausible, absent des seeds) et on exige qu'elle le voie."""
    assert _cites("Guide dédié : `bulk-load-reseau`.") == {"bulk-load-reseau"}
    assert "bulk-load-reseau" not in _slugs_semes()
    assert _cites("guide `notice`") <= _slugs_semes()
