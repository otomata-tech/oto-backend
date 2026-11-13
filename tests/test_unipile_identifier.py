"""Canonicalisation du public_identifier LinkedIn (#180).

Un slug LinkedIn accentué (`chloé-dupré`) fait renvoyer à l'API Unipile un 403
« Insufficient permissions » trompeur — LinkedIn génère toujours des slugs ASCII.
`_canonical_li_identifier` retire les diacritiques avant l'appel (idempotent sur un
slug déjà ASCII ou un provider_id opaque).
"""
from __future__ import annotations

from oto_mcp.tools.unipile import _canonical_li_identifier as canon


def test_strips_accents_from_slug():
    assert canon("chloé-dupré") == "chloe-dupre"
    assert canon("éàüô-test") == "eauo-test"


def test_ascii_slug_unchanged():
    assert canon("jean-dupont") == "jean-dupont"


def test_opaque_provider_id_unchanged():
    # provider id LinkedIn (base64-ish, sans accent) → intact
    assert canon("ACoAAB1234xyz_-") == "ACoAAB1234xyz_-"


def test_idempotent():
    once = canon("françois-mitterrand")
    assert once == canon(once) == "francois-mitterrand"


import pytest  # noqa: E402

from oto_mcp.mcp_errors import McpError  # noqa: E402


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/in/jean-dupont",
    "https://www.linkedin.com/in/jean-dupont/",
    "https://fr.linkedin.com/in/jean-dupont/?originalSubdomain=fr",
    "linkedin.com/in/jean-dupont",
    "https://www.linkedin.com/in/jean-dupont/details/experience/",
])
def test_une_url_de_profil_est_ramenee_au_slug(url):
    """#1353 : l'URL complète partait telle quelle → faux 403 « can't be accessed »."""
    assert canon(url) == "jean-dupont"


def test_une_url_encodee_et_accentuee_rend_le_slug_ascii():
    assert canon("https://www.linkedin.com/in/chlo%C3%A9-dupr%C3%A9/") == "chloe-dupre"


def test_un_slug_nu_encode_en_pourcentage_rend_le_slug_ascii():
    """#1447 : le slug nu encodé partait tel quel → INVALID_ARGUMENT de l'amont."""
    assert canon("chlo%C3%A9-dupr%C3%A9-123456") == "chloe-dupre-123456"
    assert canon(canon("chlo%C3%A9-dupr%C3%A9")) == "chloe-dupre"


def test_une_url_d_entreprise_est_ramenee_au_slug():
    assert canon("https://www.linkedin.com/company/acme-corp/about/", "company") == "acme-corp"


@pytest.mark.parametrize("url,genre", [
    ("https://www.linkedin.com/sales/lead/ACwAA123,NAME_SEARCH,x", "in"),
    ("https://www.linkedin.com/company/acme-corp/", "in"),
    ("https://www.linkedin.com/in/jean-dupont/", "company"),
    ("https://www.linkedin.com/feed/update/urn:li:activity:1/", "in"),
    ("https://exemple.fr/in/jean-dupont", "in"),
])
def test_une_autre_url_est_refusee_nommement(url, genre):
    with pytest.raises(McpError) as e:
        canon(url, genre)
    assert f"linkedin.com/{genre}/<slug>" in str(e.value)
