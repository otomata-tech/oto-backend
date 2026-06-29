"""Client Pennylane : upload bytes + import facture fournisseur (oto-backend#60).

Vérifie que le client (oto-core) poste sur les bons endpoints avec le bon body,
sans I/O réseau (session HTTP stubée).
"""
from __future__ import annotations

from oto.tools.pennylane import PennylaneClient


class _Resp:
    def __init__(self, ok=True, payload=None):
        self.ok = ok
        self.status_code = 200 if ok else 422
        self._payload = payload or {}
        self.text = "err"
    def json(self):
        return self._payload


def test_upload_file_bytes_posts_multipart(monkeypatch):
    client = PennylaneClient(api_key="k")
    seen = {}
    def fake_post(url, files=None, timeout=None, **kw):
        seen["url"] = url
        seen["filename"] = files["file"][0]
        seen["content"] = files["file"][1].read()
        seen["content_type"] = files["file"][2]
        return _Resp(ok=True, payload={"id": 999, "filename": "f.pdf", "url": "http://x"})
    monkeypatch.setattr(client.session, "post", fake_post)
    out = client.upload_file_bytes(b"%PDF-data", "f.pdf")
    assert out["id"] == 999
    assert seen["url"].endswith("/file_attachments")
    assert seen["filename"] == "f.pdf"
    assert seen["content"] == b"%PDF-data"
    assert seen["content_type"] == "application/pdf"


def test_import_supplier_invoice_body(monkeypatch):
    client = PennylaneClient(api_key="k")
    captured = {}
    def fake_post(endpoint, data, retries=3):
        captured["endpoint"] = endpoint
        captured["body"] = data
        return {"id": 4242, "draft": True}
    monkeypatch.setattr(client, "post", fake_post)
    out = client.import_supplier_invoice(
        file_attachment_id=999, supplier_id=12, date="2026-04-07",
        deadline="2026-05-07", currency_amount_before_tax="68.26",
        currency_amount="81.91", currency_tax="13.65",
        invoice_lines=[{"label": "Spreadshirt", "amount": "81.91"}],
        external_reference="SP-27948534",
    )
    assert out["id"] == 4242
    assert captured["endpoint"] == "supplier_invoices/import"
    b = captured["body"]
    assert b["file_attachment_id"] == 999 and b["supplier_id"] == 12
    assert b["currency"] == "EUR"  # défaut
    # montants en string
    assert b["currency_amount"] == "81.91" and isinstance(b["currency_amount"], str)
    assert b["external_reference"] == "SP-27948534"
    assert len(b["invoice_lines"]) == 1


def test_upload_file_delegates_to_bytes(monkeypatch, tmp_path):
    # upload_file(path) lit le disque puis délègue à upload_file_bytes.
    client = PennylaneClient(api_key="k")
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-onefile")
    seen = {}
    monkeypatch.setattr(client, "upload_file_bytes",
                        lambda data, filename, *a, **k: seen.update(data=data, filename=filename) or {"id": 1})
    client.upload_file(str(p))
    assert seen["data"] == b"%PDF-onefile" and seen["filename"] == "doc.pdf"
