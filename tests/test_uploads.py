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
