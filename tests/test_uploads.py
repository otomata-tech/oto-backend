"""Upload out-of-bande de contenu volumineux (issue oto-backend#105).

Handler `oto_upload_url` (mint) + matérialisation côté réception. On monkeypatche
les seams (db/media_store/ownership) et le secret HMAC — pas de DB ni S3.
"""
import os

import pytest

os.environ.setdefault("OTO_MCP_OAUTH_STATE_SECRET", "test-secret")

from oto_mcp import upload_tokens as ut
from oto_mcp.capabilities import uploads as U
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="u1", org_id=42)


@pytest.fixture
def seams(monkeypatch):
    monkeypatch.setattr(ut, "check_target_access", lambda sub, target: None)  # autz OK par défaut
    return monkeypatch


# --- mint (capacité) --------------------------------------------------------

def test_mint_doc_create_returns_signed_url(seams):
    out = U._upload_url(CTX, U.UploadUrlInput(target="doc", op="create",
                                              project_id=5, title="Transcript"))
    assert out["method"] == "PUT" and "/api/upload/" in out["url"]
    tok = out["url"].rsplit("/", 1)[1]
    p = ut.verify(tok)
    assert p["sub"] == "u1" and p["org"] == 42
    assert p["target"] == {"kind": "doc", "op": "create", "project_id": 5,
                           "parent_id": None, "title": "Transcript", "doc_kind": "source"}


def test_mint_doc_create_requires_title(seams):
    with pytest.raises(AuthzDenied) as e:
        U._upload_url(CTX, U.UploadUrlInput(target="doc", op="create", project_id=5))
    assert e.value.code == "missing_title"


def test_mint_doc_update_seals_doc_id(seams):
    out = U._upload_url(CTX, U.UploadUrlInput(target="doc", op="update", doc_id=9))
    p = ut.verify(out["url"].rsplit("/", 1)[1])
    assert p["target"] == {"kind": "doc", "op": "update", "doc_id": 9}


def test_mint_project_file_requires_filename(seams):
    with pytest.raises(AuthzDenied) as e:
        U._upload_url(CTX, U.UploadUrlInput(target="project_file", project_id=5))
    assert e.value.code == "missing_filename"


def test_mint_fail_fast_on_no_write(monkeypatch):
    def deny(sub, target):
        raise ut.UploadError(403, "forbidden", "nope")
    monkeypatch.setattr(ut, "check_target_access", deny)
    with pytest.raises(AuthzDenied) as e:
        U._upload_url(CTX, U.UploadUrlInput(target="doc", op="create",
                                            project_id=5, title="T"))
    assert e.value.code == "forbidden" and e.value.status == 403


# --- verify (jeton) ---------------------------------------------------------

def test_verify_rejects_tamper():
    tok, _ = ut.sign("u1", 42, {"kind": "doc", "op": "update", "doc_id": 1})
    assert ut.verify(tok[:-4] + "AAAA") is None


def test_verify_rejects_expired():
    tok, _ = ut.sign("u1", 42, {"kind": "doc", "op": "update", "doc_id": 1}, ttl=-1)
    assert ut.verify(tok) is None


def test_verify_rejects_wrong_typ():
    # Un payload signé du bon secret mais typ != upload (ex. state OAuth) est refusé.
    import base64, hashlib, hmac, json
    bad = json.dumps({"typ": "oauth", "sub": "x"}).encode()
    sig = hmac.new(b"test-secret", bad, hashlib.sha256).digest()
    b64 = lambda d: base64.urlsafe_b64encode(d).rstrip(b"=").decode()
    assert ut.verify(f"{b64(bad)}.{b64(sig)}") is None


# --- materialize (réception) ------------------------------------------------

def test_materialize_doc_create(monkeypatch):
    calls = {}
    import oto_mcp.db as db
    def fake_create(pid, title, **k):
        calls["create"] = (pid, title, k)
        return 77
    monkeypatch.setattr(db, "create_doc", fake_create)
    monkeypatch.setattr(db, "log_project_activity", lambda *a, **k: None)
    target = {"kind": "doc", "op": "create", "project_id": 5, "parent_id": None,
              "title": "T", "doc_kind": "source"}
    res = ut.materialize("u1", target, b"# hello\nverbatim", None)
    assert res == {"ok": True, "kind": "doc", "op": "create", "doc_id": 77,
                   "project_id": 5, "bytes": 16, "chars": 16}
    assert calls["create"][2]["body_md"] == "# hello\nverbatim"


def test_materialize_doc_rejects_non_utf8(monkeypatch):
    import oto_mcp.db as db
    monkeypatch.setattr(db, "update_doc", lambda *a, **k: None)
    with pytest.raises(ut.UploadError) as e:
        ut.materialize("u1", {"kind": "doc", "op": "update", "doc_id": 1},
                       b"\xff\xfe\x00binary", None)
    assert e.value.code == "not_utf8"


def test_materialize_project_file_ignores_curl_default_ct(monkeypatch):
    seen = {}
    import oto_mcp.db as db
    import oto_mcp.media_store as ms
    def fake_upload(prefix, owner, data, ctype, filename, *, max_bytes=None):
        seen["ctype"] = ctype
        return f"project-files/{owner}/deadbeef/{filename}"
    monkeypatch.setattr(ms, "upload_object", fake_upload)
    monkeypatch.setattr(db, "add_project_file",
                        lambda pid, key, filename, **k: {"id": 3, "s3_key": key, "filename": filename})
    monkeypatch.setattr(db, "log_project_activity", lambda *a, **k: None)
    target = {"kind": "project_file", "project_id": 5, "filename": "r.pdf",
              "title": None, "description": None, "content_type": None}
    # curl --data-binary pose application/x-www-form-urlencoded → ne doit PAS coller au PDF
    res = ut.materialize("u1", target, b"%PDF-1.7 ...", "application/x-www-form-urlencoded")
    assert seen["ctype"] == "application/octet-stream"
    assert "s3_key" not in res["file"]              # la clé S3 ne fuite jamais


def test_parse_rows_ndjson_and_csv():
    rows = ut._parse_rows(b'{"a":1}\n\n{"a":2}\n', "ndjson")
    assert rows == [{"a": 1}, {"a": 2}]
    rows = ut._parse_rows(b"email,n\na@x,A\nb@y,B\n", "csv")
    assert rows == [{"email": "a@x", "n": "A"}, {"email": "b@y", "n": "B"}]


def test_parse_rows_rejects_bad_ndjson():
    with pytest.raises(ut.UploadError) as e:
        ut._parse_rows(b'{"a":1}\nnot json\n', "ndjson")
    assert e.value.code == "bad_ndjson"
    with pytest.raises(ut.UploadError) as e:
        ut._parse_rows(b'[1,2,3]\n', "ndjson")   # array, pas un objet
    assert e.value.code == "bad_ndjson"
    with pytest.raises(ut.UploadError) as e:
        ut._parse_rows(b'   \n', "ndjson")
    assert e.value.code == "empty_dataset"


def test_materialize_datastore_batch(monkeypatch):
    seen = {}
    class FakeStore:
        def _write_rows_to_ns(self, ns_id, rows, *, key):
            seen["ns_id"], seen["rows"], seen["key"] = ns_id, rows, key
            return {"inserted": 2, "updated": 0, "count": 2, "key": key, "ids": ["r1", "r2"]}
    import oto_mcp.datastore as ds
    monkeypatch.setattr(ds, "make_store", lambda sub: FakeStore())
    target = {"kind": "datastore", "ns_id": 7, "namespace": "boites",
              "format": "ndjson", "key": "siren"}
    res = ut.materialize("u1", target, b'{"siren":"1"}\n{"siren":"2"}', None)
    assert seen["ns_id"] == 7 and seen["key"] == "siren" and len(seen["rows"]) == 2
    assert res == {"ok": True, "kind": "datastore", "namespace": "boites",
                   "inserted": 2, "updated": 0, "count": 2, "bytes": 27}


def test_mint_datastore_seals_resolved_ns_id(monkeypatch):
    class FakeStore:
        def resolve_ns_id_for_write(self, ns): return 42
        def declared_key(self, ns): return "email"
    import oto_mcp.datastore as ds
    monkeypatch.setattr(ds, "make_store", lambda sub: FakeStore())
    monkeypatch.setattr(ut, "check_target_access", lambda sub, target: None)
    out = U._upload_url(CTX, U.UploadUrlInput(target="datastore", namespace="contacts"))
    p = ut.verify(out["url"].rsplit("/", 1)[1])
    assert p["target"] == {"kind": "datastore", "ns_id": 42, "namespace": "contacts",
                           "format": "ndjson", "key": "email"}


def test_target_label():
    assert "contacts" in ut.target_label({"kind": "datastore", "namespace": "contacts", "format": "csv"})
    assert "Transcript" in ut.target_label({"kind": "doc", "op": "create", "title": "Transcript", "project_id": 5})
    assert "r.pdf" in ut.target_label({"kind": "project_file", "filename": "r.pdf", "project_id": 5})


def test_materialize_project_file_prefers_declared_ct(monkeypatch):
    seen = {}
    import oto_mcp.db as db
    import oto_mcp.media_store as ms
    monkeypatch.setattr(ms, "upload_object",
                        lambda *a, **k: seen.setdefault("ctype", a[3]) or "k/x/f")
    monkeypatch.setattr(db, "add_project_file", lambda *a, **k: {"id": 1, "s3_key": "k/x/f"})
    monkeypatch.setattr(db, "log_project_activity", lambda *a, **k: None)
    target = {"kind": "project_file", "project_id": 5, "filename": "r.csv",
              "title": None, "description": None, "content_type": "text/csv"}
    ut.materialize("u1", target, b"a,b,c", "application/x-www-form-urlencoded")
    assert seen["ctype"] == "text/csv"
