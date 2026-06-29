"""Détection texte/binaire des contenus de fichier (file_content.as_text).

Le tool décide inline-texte vs URL-signée selon le contenu réel. On teste le pur
`_as_text` (pas d'I/O Gmail/S3) : un contenu textuel décode → inline ; un binaire
(PDF, image, octet NUL) → None → part en URL signée.
"""
from __future__ import annotations

from oto_mcp.file_content import as_text as _as_text


def test_json_csv_md_are_text():
    assert _as_text(b'{"a": 1}', "application/json") == '{"a": 1}'
    assert _as_text(b"a,b\n1,2", "text/csv") == "a,b\n1,2"
    assert _as_text(b"# Titre", "text/markdown") == "# Titre"


def test_unknown_mime_but_clean_utf8_is_text():
    # mime générique, contenu UTF-8 propre sans NUL → traité comme texte.
    assert _as_text("héllo".encode("utf-8"), "application/octet-stream") == "héllo"


def test_pdf_magic_is_binary():
    # %PDF… contient des octets non-UTF8 plus loin ; un vrai PDF échoue le decode.
    pdf = b"%PDF-1.7\n\xff\xfe\x00\x01 binary blob"
    assert _as_text(pdf, "application/pdf") is None


def test_nul_byte_is_binary_even_if_decodable():
    # Décodable en UTF-8 mais contient un NUL → considéré binaire.
    assert _as_text(b"text\x00more", "application/octet-stream") is None


def test_png_magic_is_binary():
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    assert _as_text(png, "image/png") is None


def test_invalid_utf8_is_binary():
    assert _as_text(b"\xff\xfe\xfa\xfb", "application/octet-stream") is None
