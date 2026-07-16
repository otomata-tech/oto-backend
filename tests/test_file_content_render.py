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


def test_render_media_unavailable_raises(monkeypatch):
    def boom(*a, **k):
        raise media_store.MediaError(500, "no_s3", "down")

    monkeypatch.setattr(media_store, "upload_private", boom)
    with pytest.raises(file_content.MediaUnavailable):
        file_content.render_for_agent(b"\x00\x01", "x.bin", "application/octet-stream", sub="s", prefix="p")
