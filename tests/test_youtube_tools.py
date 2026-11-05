"""Tools `youtube` / `youtube_act` (neuvième service Google, 2026-11-04).

Ce que ce fichier verrouille :
1. la PAIRE — `youtube` ne fait que lire, `youtube_act` agit ; un argument qui n'est
   pas celui de l'op est REFUSÉ avant tout appel (une miniature ne touche jamais au
   titre, une mise à jour jamais à la miniature) ;
2. les mises en ligne se lisent par la playlist des uploads, jamais `search.list` ;
3. `update_video` lit, fusionne, écrit : les champs non demandés restent, `dry_run`
   rend le diff sans écrire ;
4. la miniature part en corps binaire, JPEG/PNG reconnus aux octets ;
5. les refus Google NOMMÉS par leur raison.

Le VRAI client d'oto-core (`oto.tools.google.youtube`) sur une `requests.Session` à
transport simulé, au seam `_client_for_user` : ni serveur, ni coffre, ni Google.
"""
import asyncio
import json

import pytest
import requests
from requests.adapters import BaseAdapter

from oto_mcp.mcp_errors import McpError

pytestmark = pytest.mark.exige_pin_oto_core

DATA = "https://www.googleapis.com/youtube/v3"


def _tool(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import youtube as T

    m = FastMCP("t")
    T.register(m)
    fn = asyncio.run(m.get_tool(name)).fn
    return lambda **kw: asyncio.run(fn(**kw))


class _Google(BaseAdapter):
    """Faux Google : enregistre chaque requête, répond par `handler(request)`."""

    def __init__(self):
        super().__init__()
        self.requests: list[requests.PreparedRequest] = []
        self.handler = lambda r: (200, {})

    def send(self, request, **kwargs):
        self.requests.append(request)
        status, body = self.handler(request)
        resp = requests.Response()
        resp.status_code = status
        resp._content = b"" if status == 204 else json.dumps(body).encode()
        resp.headers["Content-Type"] = "application/json"
        resp.url, resp.request = request.url, request
        return resp

    def close(self):
        pass


@pytest.fixture
def google(monkeypatch):
    from oto.tools.google.youtube import YouTubeClient
    from oto_mcp.tools import youtube as T

    g = _Google()
    s = requests.Session()
    s.mount("https://", g)
    monkeypatch.setattr(T, "_client_for_user",
                        lambda account=None: YouTubeClient("ya29.jeton", session=s))
    return g


VIDEO = {
    "id": "v1",
    "snippet": {"title": "Old", "description": "Keep me", "tags": ["a"], "categoryId": "28",
                "publishedAt": "2026-10-01T10:00:00Z",
                "thumbnails": {"high": {"url": "https://i.ytimg.com/vi/v1/hq.jpg"}}},
    "status": {"privacyStatus": "public", "embeddable": True, "license": "youtube",
               "publicStatsViewable": True, "uploadStatus": "processed"},
    "statistics": {"viewCount": "120", "likeCount": "4", "commentCount": "2"},
    "contentDetails": {"duration": "PT2M"},
}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_paire_et_annotations():
    from fastmcp import FastMCP
    from oto_mcp.tools import youtube as T

    m = FastMCP("t")
    T.register(m)
    lire, agir = asyncio.run(m.get_tool("youtube")), asyncio.run(m.get_tool("youtube_act"))
    assert lire.annotations.readOnlyHint is True
    assert agir.annotations.readOnlyHint is False and agir.annotations.destructiveHint is True
    assert lire.description and agir.description


def test_videos_par_la_playlist_des_uploads(google):
    def h(r):
        if r.url.startswith(f"{DATA}/channels"):
            return 200, {"items": [{"id": "UC1", "contentDetails": {
                "relatedPlaylists": {"uploads": "UU1"}}}]}
        if r.url.startswith(f"{DATA}/playlistItems"):
            return 200, {"items": [{"contentDetails": {"videoId": "v1"}}], "nextPageToken": "n2"}
        if r.url.startswith(f"{DATA}/videos"):
            return 200, {"items": [VIDEO]}
        raise AssertionError(r.url)
    google.handler = h
    out = _tool("youtube")(op="videos", max_results=10)
    assert out["channel_id"] == "UC1" and out["next_page_token"] == "n2"
    v = out["videos"][0]
    assert (v["id"], v["title"], v["privacy"], v["views"]) == ("v1", "Old", "public", "120")
    assert v["studio_url"] == "https://studio.youtube.com/video/v1/edit"
    assert not any("/search" in r.url for r in google.requests)
    assert all(r.headers["Authorization"] == "Bearer ya29.jeton" for r in google.requests)
    assert not any("ya29" in r.url for r in google.requests)


def test_un_argument_d_une_autre_op_est_refuse_sans_appel(google):
    with pytest.raises(McpError, match="does not take `title`"):
        _tool("youtube_act")(op="set_thumbnail", video_id="v1", title="Nouveau",
                             image={"kind": "url", "url": "https://x/y.png"})
    with pytest.raises(McpError, match="does not take `image`"):
        _tool("youtube_act")(op="update_video", video_id="v1", title="x",
                             image={"kind": "url", "url": "https://x/y.png"})
    with pytest.raises(McpError, match="does not take `video_id`"):
        _tool("youtube")(op="channel", video_id="v1")
    assert google.requests == []


def test_update_video_garde_le_reste_et_dry_run_n_ecrit_pas(google):
    google.handler = lambda r: (200, {"items": [VIDEO]}) if r.method == "GET" else (200, VIDEO)
    out = _tool("youtube_act")(op="update_video", video_id="v1", title="New", dry_run=True)
    assert out["changes"] == {"title": {"from": "Old", "to": "New"}}
    assert [r.method for r in google.requests] == ["GET"]

    google.requests.clear()
    _tool("youtube_act")(op="update_video", video_id="v1", description="New desc")
    put = google.requests[-1]
    assert put.method == "PUT" and "part=snippet" in put.url and "status" not in put.url
    body = json.loads(put.body)
    assert body["snippet"]["title"] == "Old" and body["snippet"]["tags"] == ["a"]
    assert body["snippet"]["description"] == "New desc"


def test_update_video_sans_champ_refuse(google):
    with pytest.raises(McpError, match="at least one field"):
        _tool("youtube_act")(op="update_video", video_id="v1")
    assert google.requests == []


def test_set_thumbnail_binaire(google, monkeypatch):
    from oto_mcp import file_source
    from oto_mcp.tools import youtube as T

    monkeypatch.setattr(T.file_source, "resolve", lambda src, max_bytes=0:
                        file_source.ResolvedFile(PNG, "thumb.png", "application/octet-stream"))
    google.handler = lambda r: (200, {"items": [{"high": {"url": "https://i.ytimg.com/t.jpg"}}]})
    out = _tool("youtube_act")(op="set_thumbnail", video_id="v1",
                               image={"kind": "project_file", "project_id": 1, "file_id": 2})
    assert out["thumbnail_set"] is True and out["thumbnail"] == "https://i.ytimg.com/t.jpg"
    r = google.requests[0]
    assert r.url.startswith("https://www.googleapis.com/upload/youtube/v3/thumbnails/set")
    assert "videoId=v1" in r.url and r.headers["Content-Type"] == "image/png"
    assert r.body == PNG


def test_set_thumbnail_refuse_un_gif(google, monkeypatch):
    from oto_mcp import file_source
    from oto_mcp.tools import youtube as T

    monkeypatch.setattr(T.file_source, "resolve", lambda src, max_bytes=0:
                        file_source.ResolvedFile(b"GIF89a....", "t.gif", "image/gif"))
    with pytest.raises(McpError, match="JPEG or PNG"):
        _tool("youtube_act")(op="set_thumbnail", video_id="v1", image={"kind": "url", "url": "u"})
    assert google.requests == []


def test_commentaires_de_la_chaine_par_defaut(google):
    def h(r):
        if r.url.startswith(f"{DATA}/channels"):
            return 200, {"items": [{"id": "UC1"}]}
        return 200, {"items": [{"id": "t1", "snippet": {
            "videoId": "v1", "totalReplyCount": 0,
            "topLevelComment": {"id": "c1", "snippet": {"authorDisplayName": "Ana",
                                                        "textDisplay": "Great"}}}}]}
    google.handler = h
    out = _tool("youtube")(op="comments", moderation_status="heldForReview")
    assert out["threads"][0]["comment"] == {"id": "c1", "author": "Ana", "text": "Great"} | {
        k: None for k in ("author_channel_id", "likes", "published_at", "moderation_status")}
    url = google.requests[-1].url
    assert "allThreadsRelatedToChannelId=UC1" in url and "moderationStatus=heldForReview" in url


def test_analytics_28_jours_par_defaut(google):
    google.handler = lambda r: (200, {"columnHeaders": [{"name": "day"}, {"name": "views"}],
                                      "rows": [["2026-10-01", 5]]})
    out = _tool("youtube")(op="analytics", end_date="2026-10-28", dimensions="day")
    assert out["start_date"] == "2026-10-01"
    assert out["columns"] == ["day", "views"] and out["rows"] == [["2026-10-01", 5]]
    assert "ids=channel%3D%3DMINE" in google.requests[0].url


def test_moderation(google):
    google.handler = lambda r: (204, None)
    out = _tool("youtube_act")(op="set_moderation", comment_ids=["c1"],
                               moderation_status="rejected", ban_author=True)
    assert out["ban_author"] is True
    assert "banAuthor=true" in google.requests[0].url


@pytest.mark.parametrize("body,attendu", [
    ({"error": {"code": 403, "message": "x", "errors": [{"reason": "quotaExceeded"}]}},
     "quota"),
    ({"error": {"code": 403, "message": "x", "details": [{"reason": "SERVICE_DISABLED"}]}},
     "not enabled"),
    ({"error": {"code": 403, "message": "x",
                "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}},
     "connect YouTube from its card"),
    ({"error": {"code": 404, "message": "x", "errors": [{"reason": "videoNotFound"}]}},
     "cannot find"),
])
def test_refus_nommes(google, body, attendu):
    google.handler = lambda r: (body["error"]["code"], body)
    with pytest.raises(McpError, match=attendu):
        _tool("youtube")(op="channel")


# --- upload_video -------------------------------------------------------------------

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


def _resolve(monkeypatch, data, name="clip.bin", mime="application/octet-stream"):
    from oto_mcp import file_source
    from oto_mcp.tools import youtube as T

    seen = {}

    def fake(src, max_bytes=0):
        seen["max_bytes"] = max_bytes
        return file_source.ResolvedFile(data, name, mime)
    monkeypatch.setattr(T.file_source, "resolve", fake)
    return seen


def test_upload_video_prive_par_defaut_et_reprenable(google, monkeypatch):
    seen = _resolve(monkeypatch, MP4)
    session = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=s1"

    def h(r):
        if r.method == "POST":
            return 200, {}
        return 201, {"id": "nv1", "snippet": {"title": "Demo"},
                     "status": {"privacyStatus": "private", "uploadStatus": "uploaded"}}
    google.handler = h
    # the session URI travels in the Location header of the POST
    orig = google.send

    def send(request, **kw):
        resp = orig(request, **kw)
        if request.method == "POST":
            resp.headers["Location"] = session
        return resp
    google.send = send

    out = _tool("youtube_act")(op="upload_video", title="Demo",
                               video={"kind": "project_file", "project_id": 1, "file_id": 9})
    assert out["video_id"] == "nv1" and out["uploaded"] is True
    assert out["video"]["privacy"] == "private"
    assert seen["max_bytes"] == 100 * 1024 * 1024
    init, put = google.requests
    body = json.loads(init.body)
    assert body["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
    assert body["snippet"] == {"title": "Demo", "categoryId": "22"}
    assert init.headers["X-Upload-Content-Type"] == "video/mp4"   # sniffed from `ftyp`
    assert put.url == session and put.body == MP4
    assert put.headers["Content-Range"] == f"bytes 0-{len(MP4) - 1}/{len(MP4)}"


def test_upload_video_refuse_un_argument_d_une_autre_op(google):
    with pytest.raises(McpError, match="does not take `image`"):
        _tool("youtube_act")(op="upload_video", title="T", video={"kind": "url", "url": "u"},
                             image={"kind": "url", "url": "u"})
    with pytest.raises(McpError, match="does not take `video_id`"):
        _tool("youtube_act")(op="upload_video", title="T", video_id="v1",
                             video={"kind": "url", "url": "u"})
    with pytest.raises(McpError, match="does not take `video`"):
        _tool("youtube_act")(op="set_thumbnail", video_id="v1",
                             image={"kind": "url", "url": "u"},
                             video={"kind": "url", "url": "u"})
    assert google.requests == []


def test_upload_video_sans_titre_refuse(google):
    with pytest.raises(McpError, match="requires `title`"):
        _tool("youtube_act")(op="upload_video", video={"kind": "url", "url": "u"})
    assert google.requests == []


def test_upload_video_refuse_une_image(google, monkeypatch):
    _resolve(monkeypatch, PNG, name="t.png", mime="image/png")
    with pytest.raises(McpError, match="not a video"):
        _tool("youtube_act")(op="upload_video", title="T", video={"kind": "url", "url": "u"})
    assert google.requests == []


def test_upload_video_limite_nommee(google, monkeypatch):
    _resolve(monkeypatch, MP4, name="a.mp4")
    google.handler = lambda r: (400, {"error": {"code": 400, "message": "x",
                                                "errors": [{"reason": "uploadLimitExceeded"}]}})
    with pytest.raises(McpError, match="upload limit"):
        _tool("youtube_act")(op="upload_video", title="T", video={"kind": "url", "url": "u"})
