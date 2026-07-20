"""Canonicalisation du public_identifier LinkedIn (#180).

Un slug LinkedIn accentué (`nicolas-chéhanne`) fait renvoyer à l'API Unipile un 403
« Insufficient permissions » trompeur — LinkedIn génère toujours des slugs ASCII.
`_canonical_li_identifier` retire les diacritiques avant l'appel (idempotent sur un
slug déjà ASCII ou un provider_id opaque).
"""
from __future__ import annotations

from oto_mcp.tools.unipile import _canonical_li_identifier as canon


def test_strips_accents_from_slug():
    assert canon("nicolas-chéhanne") == "nicolas-chehanne"
    assert canon("éàüô-test") == "eauo-test"


def test_ascii_slug_unchanged():
    assert canon("jean-dupont") == "jean-dupont"


def test_opaque_provider_id_unchanged():
    # provider id LinkedIn (base64-ish, sans accent) → intact
    assert canon("ACoAAB1234xyz_-") == "ACoAAB1234xyz_-"


def test_idempotent():
    once = canon("françois-mitterrand")
    assert once == canon(once) == "francois-mitterrand"
