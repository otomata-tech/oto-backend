"""Pièces jointes de gmail_compose (#206/#226/#235).

Le GmailClient attend des CHEMINS locaux ; le serveur MCP n'a pas le disque de
l'utilisateur → `_resolve_attachments` résout des refs `file_source` (drive/gmail/
url, ex. une URL signée d'oto_upload_url) en fichiers temporaires, passés au client,
puis nettoyés. On teste l'écriture temp, le cleanup, la défense path-traversal et la
propagation d'erreur (sans fuite de temp).
"""
from __future__ import annotations

import os

import pytest

from oto_mcp.file_source import FileSourceError, ResolvedFile
from oto_mcp.tools import gmail


def test_no_attachments_is_noop():
    for val in (None, []):
        paths, cleanup = gmail._resolve_attachments(val)
        assert paths == []
        cleanup()  # ne lève pas


def test_resolves_to_temp_files_then_cleans_up(monkeypatch):
    files = iter([
        ResolvedFile(b"PDF-BYTES-1", "plaquette.pdf", "application/pdf"),
        ResolvedFile(b"DATA-2", "notes.txt", "text/plain"),
    ])
    monkeypatch.setattr(gmail.file_source, "resolve", lambda s: next(files))
    srcs = [{"kind": "url", "url": "https://x/signed"}, {"kind": "drive", "file_id": "y"}]
    paths, cleanup = gmail._resolve_attachments(srcs)
    assert len(paths) == 2
    assert os.path.basename(paths[0]) == "plaquette.pdf"
    with open(paths[0], "rb") as f:
        assert f.read() == b"PDF-BYTES-1"
    with open(paths[1], "rb") as f:
        assert f.read() == b"DATA-2"
    cleanup()
    assert not os.path.exists(paths[0]) and not os.path.exists(paths[1])


def test_filename_is_basename_only(monkeypatch):
    # anti path-traversal : un filename « ../../evil » ne sort jamais du tmpdir
    monkeypatch.setattr(gmail.file_source, "resolve",
                        lambda s: ResolvedFile(b"x", "../../evil.sh", "text/plain"))
    paths, cleanup = gmail._resolve_attachments([{"kind": "url", "url": "u"}])
    try:
        assert os.path.basename(paths[0]) == "evil.sh"
        assert ".." not in os.path.relpath(paths[0], os.path.dirname(os.path.dirname(paths[0])))
    finally:
        cleanup()


def test_bad_ref_raises_and_cleans_up(monkeypatch):
    def _resolve(s):
        if s["kind"] == "url":
            return ResolvedFile(b"ok", "a.pdf", "application/pdf")
        raise FileSourceError("ref illisible")
    monkeypatch.setattr(gmail.file_source, "resolve", _resolve)
    with pytest.raises(FileSourceError, match="illisible"):
        gmail._resolve_attachments([{"kind": "url", "url": "u"}, {"kind": "drive", "file_id": "bad"}])
