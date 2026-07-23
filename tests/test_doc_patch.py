"""Moteur d'édition partielle par section (oto/#6 top5 #3)."""
import pytest

from oto_mcp import doc_patch as P

BODY = """# Fiche

intro générale.

## Panorama des marchés

ancien contenu du panorama.
plusieurs lignes.

### Sous-marché A

détail A.

## Contacts

- alice
"""


def test_replace_keeps_heading_and_swaps_body():
    out = P.patch_section(BODY, "Panorama des marchés", "NOUVEAU panorama.", mode="replace")
    assert "## Panorama des marchés" in out
    assert "NOUVEAU panorama." in out
    assert "ancien contenu du panorama." not in out
    # la sous-section fait partie de la section ciblée → remplacée aussi
    assert "détail A." not in out
    # les AUTRES sections intactes
    assert "intro générale." in out and "- alice" in out


def test_replace_stops_at_same_level_heading():
    # « Contacts » (## ) n'est pas touché en remplaçant « Panorama » (## ).
    out = P.patch_section(BODY, "Panorama des marchés", "x", mode="replace")
    assert "## Contacts" in out and "- alice" in out


def test_append_adds_after_existing_section_body():
    out = P.patch_section(BODY, "Contacts", "- bob", mode="append")
    assert "- alice" in out and "- bob" in out
    assert out.index("- alice") < out.index("- bob")


def test_prepend_inserts_right_after_heading():
    out = P.patch_section(BODY, "Contacts", "- zoé", mode="prepend")
    assert out.index("- zoé") < out.index("- alice")


def test_heading_match_is_case_and_hash_insensitive():
    out = P.patch_section(BODY, "## panorama DES marchés", "ok", mode="replace")
    assert "ok" in out and "ancien contenu" not in out


def test_missing_section_raises_with_available():
    with pytest.raises(P.SectionNotFound) as ei:
        P.patch_section(BODY, "Inexistante", "x")
    assert "Panorama des marchés" in ei.value.available
    assert "Contacts" in ei.value.available


def test_headings_lists_all():
    assert P.headings(BODY) == ["Fiche", "Panorama des marchés", "Sous-marché A", "Contacts"]


def test_invalid_mode():
    with pytest.raises(ValueError):
        P.patch_section(BODY, "Contacts", "x", mode="delete")
