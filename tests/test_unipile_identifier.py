"""Canonicalisation du public_identifier LinkedIn (#180, #1353, #1447).

`_canonical_li_identifier` ramène une URL à son slug et décode un slug encodé en
pourcentage ; il GARDE les accents (un slug peut porter une lettre non ASCII).
La forme sans accents (#180) n'est plus qu'un repli, tenté par `_read_li_slug` quand
l'amont refuse le slug tel quel — testé dans `tests/test_unipile_slug_accents.py`.
"""
from __future__ import annotations

from oto_mcp.tools.unipile import _canonical_li_identifier as canon


def test_keeps_the_accents_of_a_slug():
    assert canon("chloé-dupré") == "chloé-dupré"
    assert canon("chloe\u0301-dupre\u0301") == "chloé-dupré"  # NFD → NFC


def test_ascii_slug_unchanged():
    assert canon("jean-dupont") == "jean-dupont"


def test_opaque_provider_id_unchanged():
    # provider id LinkedIn (base64-ish, sans accent) → intact
    assert canon("ACoAAB1234xyz_-") == "ACoAAB1234xyz_-"


def test_idempotent():
    once = canon("fran%C3%A7ois-mitterrand")
    assert once == canon(once) == "françois-mitterrand"


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


def test_une_url_encodee_et_accentuee_rend_le_slug_decode():
    assert canon("https://www.linkedin.com/in/chlo%C3%A9-dupr%C3%A9/") == "chloé-dupré"


def test_un_slug_nu_encode_en_pourcentage_rend_le_slug_decode():
    """#1447 : le slug nu encodé partait tel quel → INVALID_ARGUMENT de l'amont."""
    assert canon("chlo%c3%a9-dupr%c3%a9-123456") == "chloé-dupré-123456"
    assert canon(canon("chlo%C3%A9-dupr%C3%A9")) == "chloé-dupré"


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
