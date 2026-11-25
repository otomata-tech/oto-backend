"""`github_files op='write'` with an IMAGE: `source_url` fetched server-side, or `content_base64`.

The fetch goes through `file_source.resolve(kind='url')` under a mock transport; the egress
guard's resolution is replaced at its seam (`egress.resolved_addresses`), the GitHub client by
a fake that records its calls. Every refusal asserts the fake recorded NOTHING: no commit.

The images below are REAL files (written by an image library, its size read back by it), not
bytes built from the same reading of the spec as the parser under test.
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
from unittest.mock import patch

import httpx
import pytest

from oto_mcp import egress, file_content
from oto_mcp import file_source as fs
from oto_mcp.mcp_errors import McpError

PNG_7x5 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAcAAAAFCAIAAAAG+GGPAAAAFElEQVR4nGM8ISfHgAGYMIWoIgoAcO4BDp5SBC0AAAAASUVORK5CYII=")
PNG_4x4 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAADElEQVR4nGNgIB0AAAA0AAF2Xq7DAAAAAElFTkSuQmCC")
JPEG_7x5_PROGRESSIVE = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9PDkzODdASFxOQERXRTc4UG1R"
    "V19iZ2hnPk1xeXBkeFxlZ2P/2wBDARESEhgVGC8aGi9jQjhCY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2Nj"
    "Y2NjY2NjY2NjY2NjY2P/wgARCAAFAAcDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAT/xAAVAQEBAAAAAAAAAAAA"
    "AAAAAAADBf/aAAwDAQACEAMQAAABjAVP/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAA"
    "AAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAA"
    "AP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9oADAMBAAIAAwAAABAD/8QAFBEBAAAAAAAA"
    "AAAAAAAAAAAAAP/aAAgBAwEBPxB//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPxB//8QAFBABAAAAAAAAAAAAAAAA"
    "AAAAAP/aAAgBAQABPxB//9k=")
WEBP_LOSSY_7x5 = base64.b64decode(
    "UklGRjYAAABXRUJQVlA4ICoAAACQAQCdASoHAAUAAsBMJaACdLoAA5gA/u2QP4hd7G2//PTP62/j/xkAAAA=")
WEBP_LOSSLESS_7x5 = base64.b64decode("UklGRh4AAABXRUJQVlA4TBEAAAAvBgABAAdQjyLXo/+BiOh/AAA=")
WEBP_EXTENDED_6x9 = base64.b64decode(
    "UklGRkgAAABXRUJQVlA4WAoAAAAQAAAABQAACAAAQUxQSAoAAAABB1CyiAhERP8DVlA4IBgAAAAwAQCdASoGAAkAAsBMJaQAA3AA/vfggAA=")

PUBLIC = {"93.184.216.34"}


def _resolve(hote, port):
    """A literal address resolves to itself; `internal.test` to a private one; the rest public."""
    try:
        return {str(ipaddress.ip_address(hote.strip("[]")))}
    except ValueError:
        return {"10.0.0.5"} if hote == "internal.test" else PUBLIC


@pytest.mark.parametrize("data,fmt,dims", [
    (PNG_7x5, "png", (7, 5)),
    (PNG_4x4, "png", (4, 4)),
    (JPEG_7x5_PROGRESSIVE, "jpeg", (7, 5)),
    (WEBP_LOSSY_7x5, "webp", (7, 5)),
    (WEBP_LOSSLESS_7x5, "webp", (7, 5)),
    (WEBP_EXTENDED_6x9, "webp", (6, 9)),
])
def test_the_header_gives_format_and_size(data, fmt, dims):
    assert file_content.image_format(data) == fmt
    assert file_content.image_dimensions(data) == dims


def test_no_size_for_what_is_not_an_image():
    assert file_content.image_dimensions(b"<html>hello</html>") is None
    assert file_content.image_dimensions(PNG_7x5[:20]) is None


class _FakeGitHub:
    def __init__(self):
        self.calls = []

    def create_or_update_file(self, *a, **kw):
        self.calls.append((a, kw))
        return {"content": {"path": a[2], "sha": "blob1", "html_url": "https://github.example/f"},
                "commit": {"sha": "commit1"}}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_credential_fields",
                        lambda provider, account=None: {"token": "t", "base_url": ""})
    monkeypatch.setattr(egress, "resolved_addresses", _resolve)
    monkeypatch.setattr(fs.url_perimeter, "perimeter_of_call", lambda: None)


@pytest.fixture
def github():
    fake = _FakeGitHub()
    with patch("oto.tools.github.client.GitHubClient", return_value=fake):
        yield fake


def _serve(monkeypatch, routes: dict):
    def handler(request):
        rep = routes[str(request.url)]
        return rep if isinstance(rep, httpx.Response) else httpx.Response(200, content=rep)
    vrai = httpx.Client

    class Client(vrai):
        def __init__(self, **kw):
            super().__init__(transport=httpx.MockTransport(handler), **kw)
    monkeypatch.setattr(httpx, "Client", Client)


def _write(**kw):
    from fastmcp import FastMCP
    from oto_mcp.tools import github as gh
    m = FastMCP("t")
    gh.register(m)
    tool = {t.name: t for t in asyncio.run(m._list_tools())}["github_files"]
    args = {"op": "write", "owner": "o", "repo": "r", "message": "add logo",
            "branch": "feat/logos", **kw}
    res = asyncio.run(tool.run(args))
    return getattr(res, "structured_content", None) or res


def test_a_png_from_a_url_is_committed_as_bytes(monkeypatch, github):
    _serve(monkeypatch, {"https://logos.example/x.png": httpx.Response(
        200, content=PNG_4x4, headers={"content-type": "image/png"})})
    out = _write(path="public/logos/x.png", source_url="https://logos.example/x.png",
                 require_square=True)
    (a, kw), = github.calls
    assert a[2] == "public/logos/x.png" and a[4] == PNG_4x4 and isinstance(a[4], bytes)
    assert kw == {"sha": None, "branch": "feat/logos"}
    assert out == {"path": "public/logos/x.png", "sha": "blob1", "commit_sha": "commit1",
                   "html_url": "https://github.example/f", "branch": "feat/logos",
                   "bytes": len(PNG_4x4), "type": "png", "width": 4, "height": 4}


def test_overwriting_passes_the_sha(monkeypatch, github):
    _serve(monkeypatch, {"https://logos.example/x.png": PNG_4x4})
    _write(path="x.png", source_url="https://logos.example/x.png", sha="old")
    assert github.calls[0][1]["sha"] == "old"


def test_a_redirect_to_a_public_cdn_is_followed(monkeypatch, github):
    _serve(monkeypatch, {
        "https://img.example/a": httpx.Response(302, headers={"location": "https://cdn.example/a.webp"}),
        "https://cdn.example/a.webp": WEBP_LOSSY_7x5})
    out = _write(path="hero.webp", source_url="https://img.example/a")
    assert out["type"] == "webp" and (out["width"], out["height"]) == (7, 5)


def test_html_served_as_png_is_refused(monkeypatch, github):
    _serve(monkeypatch, {"https://logos.example/x.png": httpx.Response(
        200, content=b"<!doctype html><title>Not found</title>",
        headers={"content-type": "image/png"})})
    with pytest.raises(McpError, match="not a png, jpeg or webp"):
        _write(path="x.png", source_url="https://logos.example/x.png")
    assert github.calls == []


def test_svg_is_refused(monkeypatch, github):
    _serve(monkeypatch, {"https://logos.example/x.svg": b'<svg xmlns="http://www.w3.org/2000/svg"/>'})
    with pytest.raises(McpError, match="not a png, jpeg or webp"):
        _write(path="x.svg", source_url="https://logos.example/x.svg")
    assert github.calls == []


def test_too_big_is_refused(monkeypatch, github):
    _serve(monkeypatch, {"https://logos.example/x.png": PNG_7x5})
    with pytest.raises(McpError, match="not fetched"):
        _write(path="x.png", source_url="https://logos.example/x.png", max_bytes=50)
    assert github.calls == []


def test_more_than_two_megabytes_is_refused_whatever_max_bytes(monkeypatch, github):
    _serve(monkeypatch, {"https://logos.example/x.png": PNG_7x5 + b"\0" * (2 * 1024 * 1024)})
    with pytest.raises(McpError, match="not fetched"):
        _write(path="x.png", source_url="https://logos.example/x.png", max_bytes=10 ** 9)
    assert github.calls == []


@pytest.mark.parametrize("url", ["https://internal.test/logo.png", "https://127.0.0.1/logo.png",
                                 "https://169.254.169.254/latest"])
def test_a_private_address_is_refused_before_any_request(monkeypatch, github, url):
    def _jamais(request):
        raise AssertionError("no request may leave")
    vrai = httpx.Client
    monkeypatch.setattr(httpx, "Client",
                        lambda **kw: vrai(transport=httpx.MockTransport(_jamais), **kw))
    with pytest.raises(McpError, match="not fetched"):
        _write(path="x.png", source_url=url)
    assert github.calls == []


def test_a_redirect_to_a_private_address_is_refused(monkeypatch, github):
    _serve(monkeypatch, {"https://logos.example/x.png": httpx.Response(
        302, headers={"location": "https://internal.test/x.png"})})
    with pytest.raises(McpError, match="not fetched"):
        _write(path="x.png", source_url="https://logos.example/x.png")
    assert github.calls == []


def test_http_is_refused(github):
    with pytest.raises(McpError, match="https://"):
        _write(path="x.png", source_url="http://logos.example/x.png")
    assert github.calls == []


def test_non_square_is_refused_when_required(monkeypatch, github):
    _serve(monkeypatch, {"https://logos.example/x.png": PNG_7x5})
    with pytest.raises(McpError, match="7x5"):
        _write(path="x.png", source_url="https://logos.example/x.png", require_square=True)
    assert github.calls == []


def test_an_extension_that_lies_is_refused(monkeypatch, github):
    _serve(monkeypatch, {"https://logos.example/x": JPEG_7x5_PROGRESSIVE})
    with pytest.raises(McpError, match="matching extension"):
        _write(path="x.png", source_url="https://logos.example/x")
    assert github.calls == []


def test_content_base64_is_decoded_and_checked(github):
    out = _write(path="x.jpg", content_base64=base64.b64encode(JPEG_7x5_PROGRESSIVE).decode())
    assert github.calls[0][0][4] == JPEG_7x5_PROGRESSIVE
    assert out["type"] == "jpeg" and out["width"] == 7
    with pytest.raises(McpError, match="not a png"):
        _write(path="x.png", content_base64=base64.b64encode(b"<html></html>").decode())
    with pytest.raises(McpError, match="base64"):
        _write(path="x.png", content_base64="%%%not base64%%%")
    assert len(github.calls) == 1


@pytest.mark.parametrize("kw", [{}, {"content": "x", "source_url": "https://logos.example/x.png"},
                                {"content_base64": "AA==", "source_url": "https://a.example/x"}])
def test_exactly_one_content_source(github, kw):
    with pytest.raises(McpError, match="exactly one"):
        _write(path="x.png", **kw)
    assert github.calls == []


def test_text_writes_are_unchanged(github):
    _write(path="a.txt", content="hello")
    (a, kw), = github.calls
    assert a[4] == "hello"
