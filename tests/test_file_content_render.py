"""Service unique `file_content.render_for_agent` — décision inline vs URL signée
partagée par gmail/drive/slack (DRY, ex-duplication)."""
import pytest

from oto_mcp import file_content, media_store


def test_render_inline_small_text():
    out = file_content.render_for_agent(b"# hello", "a.md", "text/markdown", sub="s", prefix="slack-files")
    assert out == {"filename": "a.md", "mimeType": "text/markdown", "size": 7,
                   "encoding": "text", "content": "# hello"}


def test_render_binary_uploads_and_signs(monkeypatch):
    calls = {}

    def fake_upload(prefix, sub, data, mime, filename):
        calls.update(prefix=prefix, sub=sub, filename=filename, size=len(data))
        return "https://signed/x"

    monkeypatch.setattr(media_store, "upload_private", fake_upload)
    monkeypatch.setattr(media_store, "presign_expiry", lambda: 3600)
    out = file_content.render_for_agent(b"\x89PNG\x00", "img.png", "image/png", sub="u1", prefix="slack-files")
    assert out["encoding"] == "url"
    assert out["url"] == "https://signed/x"
    assert out["expires_in"] == 3600
    assert calls == {"prefix": "slack-files", "sub": "u1", "filename": "img.png", "size": 5}


def test_render_large_text_goes_to_url(monkeypatch):
    monkeypatch.setattr(media_store, "upload_private", lambda *a, **k: "https://signed/y")
    monkeypatch.setattr(media_store, "presign_expiry", lambda: 3600)
    big = b"x" * (file_content.INLINE_TEXT_CAP + 1)
    out = file_content.render_for_agent(big, "big.md", "text/markdown", sub="s", prefix="p")
    assert out["encoding"] == "url"        # texte mais trop gros → URL, pas inline


def test_render_pdf_returns_its_text_inline_and_joins_the_original(monkeypatch):
    # Un agent dont le bac à sable ne joint pas notre stockage lisait un PDF servi
    # en URL seule : rien. Le texte vient maintenant inline, l'original à côté.
    from _pdf_texte import pdf_texte
    monkeypatch.setattr(media_store, "upload_private", lambda *a, **k: "https://signed/pdf")
    monkeypatch.setattr(media_store, "presign_expiry", lambda: 3600)
    out = file_content.render_for_agent(pdf_texte(["Facture 42", "Total 1 200,00"]),
                                        "f.pdf", "application/pdf", sub="s", prefix="p")
    assert out["encoding"] == "text" and out["format"] == "pdf-text"
    assert "Facture 42" in out["content"] and out["pages"] == 1
    assert out["truncated"] is False
    assert out["raw_url"] == "https://signed/pdf" and out["raw_expires_in"] == 3600


def test_render_pdf_without_text_falls_back_to_url_and_says_why(monkeypatch):
    monkeypatch.setattr(media_store, "upload_private", lambda *a, **k: "https://signed/scan")
    monkeypatch.setattr(media_store, "presign_expiry", lambda: 3600)
    from _pdf_texte import pdf_sans_texte
    out = file_content.render_for_agent(pdf_sans_texte(), "scan.pdf",
                                        "application/pdf", sub="s", prefix="p")
    assert out["encoding"] == "url" and out["url"] == "https://signed/scan"
    assert out["text_unavailable"].startswith("empty")


def test_render_unreadable_pdf_never_leaks_its_bytes_as_text(monkeypatch):
    # Des octets de PDF illisible restent décodables en UTF-8 : sans garde, ils
    # tombaient dans la règle du « petit texte » et sortaient inline.
    monkeypatch.setattr(media_store, "upload_private", lambda *a, **k: "https://signed/bad")
    monkeypatch.setattr(media_store, "presign_expiry", lambda: 3600)
    out = file_content.render_for_agent(b"%PDF-1.4\n" + b"x" * 50, "casse.pdf",
                                        "application/pdf", sub="s", prefix="p")
    assert out["encoding"] == "url" and "content" not in out
    assert out["text_unavailable"].startswith("failed")


def test_render_pdf_text_survives_a_storage_outage(monkeypatch):
    from _pdf_texte import pdf_texte

    def boom(*a, **k):
        raise media_store.MediaError(500, "no_s3", "down")

    monkeypatch.setattr(media_store, "upload_private", boom)
    out = file_content.render_for_agent(pdf_texte(["Contrat de prestation"]), "c.pdf",
                                        "application/pdf", sub="s", prefix="p")
    assert "Contrat de prestation" in out["content"]
    assert "raw_url" not in out and "indisponible" in out["raw_unavailable"]


def test_render_media_unavailable_raises(monkeypatch):
    def boom(*a, **k):
        raise media_store.MediaError(500, "no_s3", "down")

    monkeypatch.setattr(media_store, "upload_private", boom)
    with pytest.raises(file_content.MediaUnavailable):
        file_content.render_for_agent(b"\x00\x01", "x.bin", "application/octet-stream", sub="s", prefix="p")


# --- pièce jointe d'e-mail : image et scan JPEG servis EN IMAGE (#1522) ---------
# Une URL signée sur notre stockage est injoignable d'un bac à sable qui filtre sa
# sortie : l'image ou le scan doit revenir en bloc image, et RIEN n'est déposé.

def _aucun_depot(monkeypatch):
    def interdit(*a, **k):
        raise AssertionError("upload_private ne doit pas être appelé")
    monkeypatch.setattr(media_store, "upload_private", interdit)


def _depot_signe(monkeypatch):
    monkeypatch.setattr(media_store, "upload_private", lambda *a, **k: "https://signed/z")
    monkeypatch.setattr(media_store, "presign_expiry", lambda: 3600)


def _blocs(res):
    import base64

    from fastmcp.tools.tool import ToolResult
    from mcp.types import ImageContent, TextContent
    assert isinstance(res, ToolResult)
    texte, *images = res.content
    assert isinstance(texte, TextContent) and "Nothing was stored" in texte.text
    assert images and all(isinstance(i, ImageContent) for i in images)
    assert res.structured_content is None
    return texte.text, [(i.mimeType, base64.b64decode(i.data)) for i in images]


def test_piece_jointe_image_revient_en_image_sans_depot(monkeypatch):
    from _pdf_texte import jpeg_minimal
    _aucun_depot(monkeypatch)
    jpeg = jpeg_minimal()
    # Le type annoncé ne compte pas : le format se lit dans les octets.
    res = file_content.render_attachment_for_agent(
        jpeg, "photo.jpg", "application/octet-stream", sub="s", prefix="p")
    texte, images = _blocs(res)
    assert "photo.jpg" in texte and str(len(jpeg)) in texte
    assert images == [("image/jpeg", jpeg)]


def test_piece_jointe_image_trop_grosse_part_en_url_et_dit_pourquoi(monkeypatch):
    _depot_signe(monkeypatch)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * file_content.INLINE_IMAGE_CAP
    out = file_content.render_attachment_for_agent(png, "grand.png", "image/png",
                                                   sub="s", prefix="p")
    assert out["encoding"] == "url" and out["url"] == "https://signed/z"
    assert "over the 3 MB" in out["image_unavailable"]


def test_piece_jointe_pdf_scanne_en_jpeg_revient_page_par_page(monkeypatch):
    from _pdf_texte import jpeg_minimal, pdf_scanne
    _aucun_depot(monkeypatch)
    jpeg = jpeg_minimal()
    res = file_content.render_attachment_for_agent(
        pdf_scanne([("/DCTDecode", jpeg), ("/DCTDecode", jpeg)]), "scan.pdf",
        "application/pdf", sub="s", prefix="p")
    texte, images = _blocs(res)
    assert "scanned PDF" in texte and "2 page(s)" in texte
    assert images == [("image/jpeg", jpeg), ("image/jpeg", jpeg)]


def test_piece_jointe_pdf_scanne_non_jpeg_garde_l_url_et_dit_pourquoi(monkeypatch):
    from _pdf_texte import pdf_scanne
    _depot_signe(monkeypatch)
    out = file_content.render_attachment_for_agent(
        pdf_scanne([("/CCITTFaxDecode", b"\x00\x01")]), "fax.pdf", "application/pdf",
        sub="s", prefix="p")
    assert out["encoding"] == "url" and out["text_unavailable"].startswith("empty")
    assert "/CCITTFaxDecode" in out["image_unavailable"]


def test_piece_jointe_pdf_scanne_trop_de_pages_garde_l_url(monkeypatch):
    from _pdf_texte import jpeg_minimal, pdf_scanne
    _depot_signe(monkeypatch)
    n = file_content.SCAN_PAGES_CAP + 1
    out = file_content.render_attachment_for_agent(
        pdf_scanne([("/DCTDecode", jpeg_minimal())] * n), "long.pdf", "application/pdf",
        sub="s", prefix="p")
    assert out["encoding"] == "url" and f"{n} pages" in out["image_unavailable"]


def test_piece_jointe_pdf_a_texte_et_texte_inchanges(monkeypatch):
    from _pdf_texte import pdf_texte
    _depot_signe(monkeypatch)
    pdf = pdf_texte(["Ligne de test 1", "Ligne de test 2"])
    for data, nom, mime in [(pdf, "f.pdf", "application/pdf"),
                            (b"# hello", "a.md", "text/markdown"),
                            (b"\x00\x01binaire", "x.bin", "application/octet-stream")]:
        avant = file_content.render_for_agent(data, nom, mime, sub="s", prefix="p")
        apres = file_content.render_attachment_for_agent(data, nom, mime, sub="s",
                                                         prefix="p")
        assert apres == avant and "image_unavailable" not in apres
