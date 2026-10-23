"""`hubspot_object(op="merge")` — fusionner des doublons dans un survivant.

Une fusion HubSpot est IRRÉVERSIBLE : ce fichier fige ce qui la précède (les refus,
la lecture de chaque perdant, le `dry_run` qui n'écrit rien), ce qu'elle rend par
perdant (merged | already_merged | not_found | error, sans jamais arrêter le lot),
le suivi d'un survivant dont HubSpot change l'id, et la reprise d'un 429 — celle du
client oto-core, jouée ici sur un VRAI `HubSpotClient` dont seul le transport est
simulé. Aucun appel réseau.
"""
import asyncio

import pytest
from oto.tools.common.errors import UpstreamHTTPError

from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import hubspot_merge


def _rec(rid, name="Acme", domain="acme.test", contacts=0, deals=0, more=False):
    assoc = {"contacts": {"results": [{"id": f"c{i}"} for i in range(contacts)]},
             "deals": {"results": [{"id": f"d{i}"} for i in range(deals)]}}
    if more:
        assoc["contacts"]["paging"] = {"next": {"after": "x"}}
    return {"id": rid, "properties": {"name": name, "domain": domain},
            "associations": assoc}


def _http(status, message="boom"):
    return UpstreamHTTPError(status, {"message": message}, service="hubspot")


class FakeHub:
    """Ce que `merge` touche du client : `get_object` (lectures) et `_request` (POST)."""

    def __init__(self, records, merges=None):
        self.records = records          # id → record, ou exception à lever
        self.merges = merges or {}      # perdant → réponse, ou exception
        self.posts = []

    def get_object(self, otype, rid, properties=None, associations=None):
        r = self.records[rid]
        if isinstance(r, Exception):
            raise r
        return r

    def _request(self, method, path, json=None, _budget=None):
        assert method == "POST" and path.endswith("/merge")
        self.posts.append((path, dict(json)))
        r = self.merges.get(json["objectIdToMerge"])
        if isinstance(r, Exception):
            raise r
        return r if r is not None else {"id": json["primaryObjectId"]}


@pytest.fixture
def hub(monkeypatch):
    """Monte `hubspot_object` sur un FastMCP nu, client remplacé par un FakeHub."""
    import oto.tools.hubspot.client as hs

    box = {}
    monkeypatch.setattr(hs, "HubSpotClient", lambda *a, **k: box["c"])
    monkeypatch.setattr("oto_mcp.auth.hubspot.bearer", lambda *a, **k: "k")

    def call(client, **kw):
        from fastmcp import FastMCP
        from oto_mcp.tools import hubspot as H

        box["c"] = client
        m = FastMCP("t")
        H.register(m)
        return asyncio.run(m.get_tool("hubspot_object")).fn(**kw)
    return call


# --- succès ------------------------------------------------------------------------

def test_each_loser_is_merged_into_the_primary_in_order(hub):
    c = FakeHub({"1": _rec("1"), "2": _rec("2"), "3": _rec("3")})
    out = hub(c, op="merge", object_type="companies", object_id="1",
              merge_ids=["2", 3])
    assert c.posts == [
        ("/crm/v3/objects/companies/merge", {"primaryObjectId": "1", "objectIdToMerge": "2"}),
        ("/crm/v3/objects/companies/merge", {"primaryObjectId": "1", "objectIdToMerge": "3"}),
    ]
    assert [r["status"] for r in out["results"]] == ["merged", "merged"]
    assert out["survivor_id"] == "1" and out["survivor_id_changed"] is False
    assert out["counts"] == {"merged": 2}


def test_a_new_survivor_id_is_surfaced_and_used_for_the_next_loser(hub):
    """HubSpot peut rendre un NOUVEL id pour le survivant : le perdant suivant est
    fusionné dans CELUI-LÀ, et la réponse le dit."""
    c = FakeHub({"1": _rec("1"), "2": _rec("2"), "3": _rec("3")},
                merges={"2": {"id": "900"}})
    out = hub(c, op="merge", object_type="contacts", object_id="1",
              merge_ids=["2", "3"])
    assert [j["primaryObjectId"] for _, j in c.posts] == ["1", "900"]
    assert out["primary_id"] == "1"
    assert out["survivor_id"] == "900" and out["survivor_id_changed"] is True
    assert out["results"][0]["survivor_id"] == "900"


# --- dry_run -----------------------------------------------------------------------

def test_dry_run_reads_and_reports_without_writing(hub):
    c = FakeHub({"1": _rec("1", "Acme", "acme.test", contacts=3, deals=1),
                 "2": _rec("2", "ACME SAS", "acme.test", contacts=2, more=True),
                 "3": _rec("900"),
                 "4": _http(404)})
    out = hub(c, op="merge", object_type="companies", object_id="1",
              merge_ids=["2", "3", "4"], dry_run=True)
    assert c.posts == []
    assert out["dry_run"] is True and out["would"] == "merge"
    assert "IRREVERSIBLE" in out["note"]
    assert out["primary"]["properties"] == {"name": "Acme", "domain": "acme.test"}
    assert out["primary"]["associated"] == {"contacts": 3, "deals": 1}
    would, merged, missing = out["results"]
    assert would["status"] == "would_merge"
    assert would["properties"]["name"] == "ACME SAS"
    assert would["associated"] == {"contacts": "2+", "deals": 0}  # page suivante = minorant
    assert merged == {"id": "3", "status": "already_merged", "resolves_to": "900",
                      "into_primary": False}
    assert missing == {"id": "4", "status": "not_found"}


# --- verdicts par perdant ------------------------------------------------------------

def test_already_merged_loser_is_not_posted(hub):
    """Un id fusionné résout vers son survivant : on ne le fusionne pas une 2e fois."""
    c = FakeHub({"1": _rec("1"), "2": _rec("1"), "3": _rec("3")})
    out = hub(c, op="merge", object_type="companies", object_id="1",
              merge_ids=["2", "3"])
    assert [j["objectIdToMerge"] for _, j in c.posts] == ["3"]
    assert out["results"][0] == {"id": "2", "status": "already_merged",
                                 "resolves_to": "1", "into_primary": True}


def test_404_on_read_or_on_merge_is_not_found_and_the_batch_goes_on(hub):
    c = FakeHub({"1": _rec("1"), "2": _http(404), "3": _rec("3"), "4": _rec("4")},
                merges={"3": _http(404)})
    out = hub(c, op="merge", object_type="deals", object_id="1",
              merge_ids=["2", "3", "4"])
    assert [r["status"] for r in out["results"]] == ["not_found", "not_found", "merged"]


def test_an_error_is_recorded_with_status_and_message_and_never_stops_the_batch(hub):
    c = FakeHub({"1": _rec("1"), "2": _rec("2"), "3": _http(403, "scope"),
                 "4": _rec("4")},
                merges={"2": _http(500, "upstream down")})
    out = hub(c, op="merge", object_type="tickets", object_id="1",
              merge_ids=["2", "3", "4"])
    e2, e3, ok = out["results"]
    assert (e2["status"], e2["http_status"], e2["message"]) == ("error", 500, "upstream down")
    assert (e3["status"], e3["http_status"]) == ("error", 403)
    assert ok["status"] == "merged"
    assert out["counts"] == {"error": 2, "merged": 1}


# --- refus ---------------------------------------------------------------------------

@pytest.mark.parametrize("kw,needle", [
    ({"object_id": "1", "merge_ids": ["1", "2"]}, "cannot be merged into itself"),
    ({"object_id": "1", "merge_ids": []}, "non-empty list"),
    ({"object_id": "1"}, "non-empty list"),
    ({"object_id": "1", "merge_ids": [str(i) for i in range(2, 13)]}, "at most 10"),
    ({"object_id": "1", "merge_ids": ["2", "2"]}, "repeats"),
    ({"merge_ids": ["2"]}, "requires object_id"),
    ({"object_id": "1", "merge_ids": ["2"], "object_type": "notes"}, "does not support"),
    ({"object_id": "1", "merge_ids": ["2"], "object_type": "2-7"}, "does not support"),
    ({"object_id": "1", "merge_ids": ["2"], "object_type": None}, "requires object_type"),
])
def test_refusals_before_any_call(hub, kw, needle):
    c = FakeHub({})
    kw = {"object_type": "companies", **kw}
    with pytest.raises(McpError, match=needle):
        hub(c, op="merge", **kw)
    assert c.posts == []


def test_a_primary_that_was_itself_merged_is_refused_naming_the_survivor(hub):
    c = FakeHub({"1": _rec("77"), "2": _rec("2")})
    with pytest.raises(McpError, match="object_id='77'"):
        hub(c, op="merge", object_type="companies", object_id="1", merge_ids=["2"])
    assert c.posts == []


def test_a_missing_primary_refuses_the_call(hub):
    c = FakeHub({"1": _http(404), "2": _rec("2")})
    with pytest.raises(Exception):
        hub(c, op="merge", object_type="companies", object_id="1", merge_ids=["2"])
    assert c.posts == []


def test_merge_arguments_are_refused_on_other_ops(hub):
    """`dry_run=true` ignoré par op='delete' supprimerait pour de vrai."""
    from unittest.mock import MagicMock

    c = MagicMock()
    with pytest.raises(McpError, match="belong to op='merge'"):
        hub(c, op="delete", object_type="companies", object_id="1", dry_run=True)
    with pytest.raises(McpError, match="belong to op='merge'"):
        hub(c, op="get", object_type="companies", object_id="1", merge_ids=["2"])
    c.delete_object.assert_not_called()


# --- gouvernance ---------------------------------------------------------------------

def test_description_says_irreversible_and_dry_run_first(hub):
    from fastmcp import FastMCP
    from oto_mcp.tools import hubspot as H

    m = FastMCP("t")
    H.register(m)
    desc = asyncio.run(m.get_tool("hubspot_object")).description
    assert "IRREVERSIBLE" in desc and "dry_run=true" in desc and "At most 10" in desc


def test_a_recipe_can_never_merge():
    """Même régime que `delete` : jamais ouvert à une recette."""
    from oto_mcp.recipes import ecritures

    assert "merge" in ecritures.OPS_DESTRUCTRICES
    verdict, _ = ecritures.juger("hubspot_object", {"op": "merge"})
    assert verdict == "refus"


# --- 429 : la reprise du client oto-core, sur un vrai HubSpotClient -------------------

class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.content = b"x"
        self.text = str(self._body)

    def json(self):
        return self._body


def _real_client(monkeypatch, script):
    """Un `HubSpotClient` réel dont le transport rejoue `script` (méthode, chemin) →
    liste de réponses, consommées dans l'ordre."""
    import oto.tools.hubspot.client as hs

    monkeypatch.setattr(hs.time, "sleep", lambda s: None)
    c = hs.HubSpotClient(api_key="pat-test")
    seen = []

    def request(method, url, timeout=None, **kw):
        path = url.removeprefix(hs.HubSpotClient.BASE_URL)
        seen.append((method, path))
        return script[(method, path)].pop(0)
    c.session.request = request
    return c, seen


def test_a_429_on_merge_is_retried_by_the_client(monkeypatch):
    rec = lambda i: _Resp(200, _rec(i))  # noqa: E731
    c, seen = _real_client(monkeypatch, {
        ("GET", "/crm/v3/objects/companies/1"): [rec("1")],
        ("GET", "/crm/v3/objects/companies/2"): [rec("2")],
        ("POST", "/crm/v3/objects/companies/merge"): [
            _Resp(429, {"policyName": "TEN_SECONDLY_ROLLING"}, {"Retry-After": "1"}),
            _Resp(200, {"id": "1"})],
    })
    out = hubspot_merge.merge(c, "companies", "1", ["2"])
    assert out["results"][0]["status"] == "merged"
    assert seen.count(("POST", "/crm/v3/objects/companies/merge")) == 2


def test_a_429_that_does_not_clear_is_an_error_and_the_next_loser_still_runs(monkeypatch):
    rec = lambda i: _Resp(200, _rec(i))  # noqa: E731
    daily = _Resp(429, {"policyName": "DAILY", "message": "daily limit"})
    c, _ = _real_client(monkeypatch, {
        ("GET", "/crm/v3/objects/companies/1"): [rec("1")],
        ("GET", "/crm/v3/objects/companies/2"): [rec("2")],
        ("GET", "/crm/v3/objects/companies/3"): [rec("3")],
        ("POST", "/crm/v3/objects/companies/merge"): [daily, _Resp(200, {"id": "1"})],
    })
    out = hubspot_merge.merge(c, "companies", "1", ["2", "3"])
    first, second = out["results"]
    assert (first["status"], first["http_status"]) == ("error", 429)
    assert second["status"] == "merged"
