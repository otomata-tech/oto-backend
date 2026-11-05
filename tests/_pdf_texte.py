"""Un PDF minimal qui PORTE du texte, fabriqué ici — pypdf sait écrire des pages
blanches, pas du texte. Une ligne par entrée de `lignes`, police standard, sans
dépendance : de quoi éprouver l'extraction sans fichier du disque."""
from __future__ import annotations

import io


def pdf_sans_texte() -> bytes:
    """Un vrai PDF qui ne porte aucun texte — ce que rend un scan sans OCR."""
    import pypdf
    w = pypdf.PdfWriter()
    w.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def pdf_texte(lignes: list[str]) -> bytes:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    flux = "BT /F1 11 Tf 14 TL 50 800 Td " + " ".join(
        f"({esc(l)}) Tj T*" for l in lignes) + " ET"
    corps = flux.encode("latin-1")
    objets = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>",
        b"<< /Length %d >>\nstream\n" % len(corps) + corps + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    positions = []
    for i, o in enumerate(objets, start=1):
        positions.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objets) + 1)
    for p in positions:
        out += b"%010d 00000 n \n" % p
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objets) + 1, xref)
    return bytes(out)


def jpeg_minimal() -> bytes:
    """Un JPEG valide de 1×1 pixel gris, écrit octet par octet (baseline, une table de
    quantification à 1, deux tables de Huffman d'un seul code) — synthétique, sans
    dépendance, rien de réel dedans."""
    def segment(marqueur: int, corps: bytes) -> bytes:
        return bytes([0xFF, marqueur]) + (len(corps) + 2).to_bytes(2, "big") + corps
    dqt = segment(0xDB, b"\x00" + b"\x01" * 64)
    sof = segment(0xC0, b"\x08\x00\x01\x00\x01\x01\x01\x11\x00")
    un_code = b"\x01" + b"\x00" * 15 + b"\x00"   # 1 code de longueur 1 → symbole 0
    dht = segment(0xC4, b"\x00" + un_code) + segment(0xC4, b"\x10" + un_code)
    sos = segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")
    # DC : catégorie 0 (bit 0) ; AC : fin de bloc (bit 0) ; bourrage à 1.
    return b"\xff\xd8" + dqt + sof + dht + sos + b"\x3f" + b"\xff\xd9"


def pdf_scanne(pages: list[tuple[str, bytes]]) -> bytes:
    """Un PDF « scanné » : une image par page, sans aucun texte. Chaque entrée est
    `(filtre, flux)` — `"/DCTDecode"` et un JPEG pour un scan JPEG, `"/CCITTFaxDecode"`
    (flux factice) pour un scan noir et blanc de fax."""
    n = len(pages)
    objets: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>",
                           b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
                               b" ".join(b"%d 0 R" % (3 + 3 * i) for i in range(n)), n)]
    for i, (filtre, flux) in enumerate(pages):
        page, image, contenu = 3 + 3 * i, 4 + 3 * i, 5 + 3 * i
        dessin = b"q 595 0 0 842 0 0 cm /Im0 Do Q"
        objets += [
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>" % (
                image, contenu),
            b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace "
            b"/DeviceGray /BitsPerComponent 8 /Filter %s /Length %d >>\nstream\n" % (
                filtre.encode(), len(flux)) + flux + b"\nendstream",
            b"<< /Length %d >>\nstream\n" % len(dessin) + dessin + b"\nendstream",
        ]
    out = bytearray(b"%PDF-1.4\n")
    positions = []
    for i, o in enumerate(objets, start=1):
        positions.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objets) + 1)
    for p in positions:
        out += b"%010d 00000 n \n" % p
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objets) + 1, xref)
    return bytes(out)
