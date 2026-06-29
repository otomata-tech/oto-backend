"""Résolveur « fichier côté oto » (oto-backend#60).

Teste le dispatch par `kind` + les gardes (kind inconnu, dépassement de taille)
sans I/O réseau : les clients Drive/Gmail et la résolution de credentials sont
monkeypatchés.
"""
from __future__ import annotations

import sys
import types

import pytest

from oto_mcp import file_source as fs


def _inject(monkeypatch, module_path: str, attr: str, value):
    """Injecte un faux module dans sys.modules pour que le `from … import` lazy de
    file_source résolve un stub — sans importer le vrai client (qui tire l'extra
    `google`, absent du venv de test mais présent en prod)."""
    mod = types.ModuleType(module_path)
    setattr(mod, attr, value)
    monkeypatch.setitem(sys.modules, module_path, mod)


@pytest.fixture(autouse=True)
def _no_google(monkeypatch):
    # Pas de credentials réels : les résolveurs drive/gmail instancient un client
    # stub, on n'atteint jamais Google.
    monkeypatch.setattr(fs, "_google_creds", lambda account: object())


def test_unknown_kind_raises():
    with pytest.raises(fs.FileSourceError):
        fs.resolve({"kind": "ftp", "path": "x"})


def test_non_dict_raises():
    with pytest.raises(fs.FileSourceError):
        fs.resolve("drive:123")


def test_drive_dispatch(monkeypatch):
    class FakeDrive:
        def __init__(self, **kw): pass
        def get_file_bytes(self, file_id):
            return {"filename": "Contrat.pdf", "mimeType": "application/pdf",
                    "size": 4, "data": b"%PDF"}
    _inject(monkeypatch, "oto.tools.google.drive.lib.drive_client", "DriveClient", FakeDrive)
    rf = fs.resolve({"kind": "drive", "file_id": "1AbC"})
    assert rf.data == b"%PDF" and rf.filename == "Contrat.pdf" and rf.mime == "application/pdf"


def test_drive_missing_file_id():
    with pytest.raises(fs.FileSourceError):
        fs.resolve({"kind": "drive"})


def test_gmail_dispatch(monkeypatch):
    class FakeGmail:
        def __init__(self, **kw): pass
        def get_attachment(self, mid, filename, index):
            assert (mid, filename, index) == ("m1", "facture.pdf", 0)
            return {"filename": "facture.pdf", "mimeType": "application/pdf",
                    "size": 3, "data": b"abc"}
    _inject(monkeypatch, "oto.tools.google.gmail.lib.gmail_client", "GmailClient", FakeGmail)
    rf = fs.resolve({"kind": "gmail", "message_id": "m1", "filename": "facture.pdf"})
    assert rf.data == b"abc"


def test_gmail_missing_args():
    with pytest.raises(fs.FileSourceError):
        fs.resolve({"kind": "gmail", "message_id": "m1"})


def test_size_cap_enforced(monkeypatch):
    class FakeDrive:
        def __init__(self, **kw): pass
        def get_file_bytes(self, file_id):
            return {"filename": "big.bin", "mimeType": "application/octet-stream",
                    "size": 10, "data": b"x" * 10}
    _inject(monkeypatch, "oto.tools.google.drive.lib.drive_client", "DriveClient", FakeDrive)
    with pytest.raises(fs.FileSourceError):
        fs.resolve({"kind": "drive", "file_id": "1"}, max_bytes=5)


def test_url_requires_http():
    with pytest.raises(fs.FileSourceError):
        fs.resolve({"kind": "url", "url": "file:///etc/passwd"})


@pytest.mark.parametrize("host", [
    "http://127.0.0.1/x",            # loopback
    "http://localhost/x",            # loopback (résolu)
    "http://169.254.169.254/latest", # IMDS cloud
    "http://10.0.0.5/x",             # privé
    "http://192.168.1.1/x",          # privé
])
def test_url_ssrf_blocked(host):
    # Anti-SSRF : une cible interne/réservée est refusée AVANT toute requête.
    with pytest.raises(fs.FileSourceError):
        fs.resolve({"kind": "url", "url": host})


def test_assert_public_host_passes_for_global():
    # Un host public ne lève pas (résolution réelle d'une IP globale).
    fs._assert_public_host("example.com")
