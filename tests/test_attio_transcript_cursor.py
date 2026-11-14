"""La transcription d'un appel se lit au-delà de sa première page (signal d'usage 1226).

`GET /v2/meetings/{meeting_id}/call_recordings/{call_recording_id}/transcript` pagine
ses segments au CURSEUR : la référence OpenAPI d'Attio déclare un paramètre de requête
`cursor` (« Use the `next_cursor` from the previous response to get the next page of
speech segments within this transcript ») et une réponse `{data, pagination:
{next_cursor}}`, `next_cursor` nul sur la dernière page. `attio_meeting op="transcript"`
ne prenait pas de curseur : une longue réunion ne se lisait qu'à sa première page.

**Le banc exerce la chaîne RÉELLE, socket excepté** (comme
`tests/test_attio_upstream_error.py`) : le vrai tool, la vraie fabrique `_client()`, le
vrai `AttioClient` de l'oto-core épinglé. Un `MagicMock` à la place du client
accepterait n'importe quel argument nommé — y compris un `cursor=` que la méthode
épinglée ne prend pas et qui lèverait `TypeError` en prod.
"""
import asyncio
import json

import pytest

from oto_mcp.tools import attio

MEETING = "708192a3-b4c5-40d1-a253-6e7f8091a2b3"
RECORDING = "8192a3b4-c5d6-41e2-b364-7f8091a2b3c4"
CHEMIN = f"meetings/{MEETING}/call_recordings/{RECORDING}/transcript"


def _page(segments: list[str], suivant: str | None) -> dict:
    return {
        "data": {
            "id": {"workspace_id": "w-0", "meeting_id": MEETING, "call_recording_id": RECORDING},
            "transcript": [{"speech": s, "start_time": i, "end_time": i + 1,
                            "speaker": {"name": "Speaker A"}} for i, s in enumerate(segments)],
            "raw_transcript": " ".join(segments),
            "web_url": "https://app.example.test/calls/x",
        },
        "pagination": {"next_cursor": suivant},
    }


class _Reponse:
    def __init__(self, corps: dict):
        self.status_code = 200
        self.ok = True
        self.text = json.dumps(corps)
        self.content = self.text.encode()
        self.headers = {}

    def json(self):
        return json.loads(self.text)


@pytest.fixture
def amont(monkeypatch):
    """Attio répond une page par curseur reçu ; rend `(transcription, appels)`."""
    import requests
    from fastmcp import FastMCP

    monkeypatch.setattr("oto_mcp.access.resolve_api_key", lambda *a, **k: ("k", False))
    pages = {None: _page(["Hello,"], "cur-2"), "cur-2": _page(["the end."], None)}
    appels: list = []

    def _faux_request(method, url, **kw):
        params = kw.get("params") or {}
        appels.append((method, url, params))
        return _Reponse(pages[params.get("cursor")])

    monkeypatch.setattr(requests, "request", _faux_request)
    serveur = FastMCP("banc")
    attio.register(serveur)
    return asyncio.run(serveur.get_tool("attio_meeting")).fn, appels


def test_la_premiere_page_rend_le_curseur_suivant(amont):
    transcription, appels = amont
    page = transcription(op="transcript", meeting_id=MEETING, call_recording_id=RECORDING)
    assert page["pagination"]["next_cursor"] == "cur-2"
    assert [s["speech"] for s in page["data"]["transcript"]] == ["Hello,"]
    (methode, url, params), = appels
    assert methode == "GET" and url.endswith("/" + CHEMIN)
    assert params == {}, "sans curseur, l'appel reste celui d'avant"


def test_un_curseur_vide_lit_la_premiere_page(amont):
    """`cursor=""` n'est pas un curseur : il ne part pas en `?cursor=` chez Attio."""
    transcription, appels = amont
    page = transcription(op="transcript", meeting_id=MEETING, call_recording_id=RECORDING,
                         cursor="")
    assert page["pagination"]["next_cursor"] == "cur-2"
    (_, _, params), = appels
    assert params == {}


def test_le_curseur_atteint_la_page_suivante(amont):
    """Le geste qui manquait : `next_cursor` repassé en `cursor`."""
    transcription, appels = amont
    page = transcription(op="transcript", meeting_id=MEETING, call_recording_id=RECORDING,
                         cursor="cur-2")
    assert [s["speech"] for s in page["data"]["transcript"]] == ["the end."]
    assert page["pagination"]["next_cursor"] is None
    (methode, url, params), = appels
    assert methode == "GET" and url.endswith("/" + CHEMIN)
    assert params == {"cursor": "cur-2"}


def test_toute_la_transcription_se_lit_en_suivant_les_curseurs(amont):
    transcription, appels = amont
    lu, curseur = [], None
    for _ in range(5):
        page = transcription(op="transcript", meeting_id=MEETING,
                             call_recording_id=RECORDING, cursor=curseur)
        lu += [s["speech"] for s in page["data"]["transcript"]]
        curseur = page["pagination"]["next_cursor"]
        if curseur is None:
            break
    assert lu == ["Hello,", "the end."]
    assert len(appels) == 2


def test_la_description_dit_le_curseur_de_la_transcription():
    """L'agent ne devine pas le geste : le tool le dit, pour `transcript` aussi."""
    from fastmcp import FastMCP

    serveur = FastMCP("banc")
    attio.register(serveur)
    tool = asyncio.run(serveur.get_tool("attio_meeting"))
    assert "transcript" in tool.parameters["properties"]["cursor"]["description"]
    puce = next(l for l in tool.description.splitlines() if '**"transcript"**' in l)
    suite = tool.description[tool.description.index(puce):].split("\n\n")[0]
    assert "next_cursor" in suite and "cursor" in suite
