"""GitHub binary writes — the bytes of an image committed by `github_files op='write'`.

A hosted agent has no shell and no file system: it cannot base64 a logo itself, and
bytes pasted into a tool argument break past a few hundred kilobytes. So the SERVER
fetches the image (`source_url`) or decodes it (`content_base64`), checks it, and
commits it; the model never sees the bytes.

The fetch is `file_source.resolve(kind='url')` — the platform's agent-URL policy:
project perimeter, egress guard on the first URL and on every redirect hop (no
private, loopback or link-local address, no https→http), streamed size cap. On top
of it: https only, a 10 s budget for the whole download, 2 MB at most.

What is committed must BE an image, read from its magic bytes: png, jpeg or webp.
The `Content-Type` is not trusted (an HTML error page served 200 as `image/png`), and
SVG is refused (XML: scripts, external references).
"""
from __future__ import annotations

import base64
import binascii
import os
import time
from typing import Optional

from .. import file_content, file_source

#: The cap of a binary write, and the ceiling of `max_bytes`.
MAX_BYTES = 2 * 1024 * 1024
#: The budget of the whole download (every hop and every read included).
FETCH_BUDGET_S = 10.0
#: The formats a binary write accepts.
FORMATS = ("png", "jpeg", "webp")
_EXTENSIONS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}


class BinaryRefused(ValueError):
    """A binary write refused before any commit: the message names what to fix."""


def _cap(max_bytes: Optional[int]) -> int:
    if max_bytes is None:
        return MAX_BYTES
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise BinaryRefused("`max_bytes` must be a positive integer.")
    return min(max_bytes, MAX_BYTES)


def _fetch(url: str, cap: int) -> bytes:
    if not isinstance(url, str) or not url.lower().startswith("https://"):
        raise BinaryRefused("`source_url` must be an https:// URL.")
    try:
        return file_source.resolve(
            {"kind": "url", "url": url}, max_bytes=cap, follow_redirects=True,
            deadline=time.monotonic() + FETCH_BUDGET_S).data
    except file_source.FileSourceError as e:
        raise BinaryRefused(f"`source_url` not fetched: {e}") from None


def _decode(texte: str, cap: int) -> bytes:
    if len(texte) > (cap * 4) // 3 + 8:
        raise BinaryRefused(f"`content_base64` decodes to more than {cap} bytes.")
    try:
        return base64.b64decode(texte, validate=True)
    except (binascii.Error, ValueError):
        raise BinaryRefused("`content_base64` is not valid base64.") from None


def image_to_commit(path: str, *, source_url: Optional[str] = None,
                    content_base64: Optional[str] = None,
                    max_bytes: Optional[int] = None,
                    require_square: bool = False) -> tuple[bytes, dict]:
    """The checked bytes of the image to write at `path`, and what is known of them
    (`bytes`, `type`, `width`, `height`). Raises `BinaryRefused` — nothing is committed."""
    cap = _cap(max_bytes)
    data = _fetch(source_url, cap) if source_url is not None else _decode(content_base64, cap)
    if len(data) > cap:
        raise BinaryRefused(f"image of {len(data)} bytes > max {cap} bytes.")
    fmt = file_content.image_format(data)
    if fmt not in FORMATS:
        raise BinaryRefused(
            "not a png, jpeg or webp image (read from the file's first bytes, not its "
            "name or Content-Type; svg, gif and html are refused) — check the URL "
            "serves the image itself.")
    attendu = _EXTENSIONS.get(os.path.splitext(path or "")[1].lower())
    if attendu and attendu != fmt:
        raise BinaryRefused(
            f"the image is a {fmt}, but `path` ends in {os.path.splitext(path)[1]}: "
            f"write it under the matching extension.")
    dims = file_content.image_dimensions(data)
    if require_square:
        if dims is None:
            raise BinaryRefused("`require_square`: the image's size could not be read.")
        if dims[0] != dims[1]:
            raise BinaryRefused(f"`require_square`: the image is {dims[0]}x{dims[1]}.")
    info = {"bytes": len(data), "type": fmt,
            "width": dims[0] if dims else None, "height": dims[1] if dims else None}
    return data, info


def receipt(res, path: str, branch: Optional[str], info: dict) -> dict:
    """The answer of a binary write: GitHub's commit reduced to what the next step
    needs — the blob `sha` (to overwrite it later) and the commit's."""
    res = res if isinstance(res, dict) else {}
    contenu = res.get("content") or {}
    commit = res.get("commit") or {}
    return {"path": contenu.get("path") or path, "sha": contenu.get("sha"),
            "commit_sha": commit.get("sha"), "html_url": contenu.get("html_url"),
            "branch": branch, **info}
