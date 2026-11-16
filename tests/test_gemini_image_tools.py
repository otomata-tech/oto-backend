"""Gemini images (Nano Banana): priced before the call, billed at what Google counted.

What this file locks:
- the bound of a call is the published per-image price, a thinking allowance, the prompt
  and the input images, at the published rates; the cost is Google's `usageMetadata`,
  modality by modality, at the same rates — never the bound: an answer without the
  breakdown is refused on the shared key, its cost said unknown on the customer's own;
- on the shared key: never served without a grant; the grant's quota is checked for the
  BOUND before anything is sent; a model without a price is refused before the client is
  built; the call notes its actual cost as `units` and `quantity`;
- on the customer's own key: the cost is noted as `quantity`, never `units`;
- `max_usd` below the bound refuses before anything is sent;
- a free-tier key's 429 says to enable billing, not "retry later";
- the images are deposited, never inline.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from oto_mcp import access as _access
from oto_mcp import session_org
from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import gemini_image as G

_VRAIE_RESOLUTION = _access.resolve_api_key
_NB = "gemini-nano-banana-2.1"
# Google's real answers, 09/11/2026, one-line prompt, 1:1 (1K, then 4K).
_USAGE = {"promptTokenCount": 11, "candidatesTokenCount": 1303, "totalTokenCount": 1747,
          "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 11}],
          "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 1120}],
          "thoughtsTokenCount": 433, "serviceTier": "standard"}
_USAGE_4K = {"promptTokenCount": 11, "candidatesTokenCount": 4016, "totalTokenCount": 4874,
             "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 11}],
             "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 3780}],
             "thoughtsTokenCount": 847, "serviceTier": "standard"}


@pytest.fixture
def client(monkeypatch):
    inst, built = MagicMock(), []

    def fabrique(**kw):
        built.append(kw)
        return inst

    monkeypatch.setattr("oto.tools.gemini.GeminiClient", fabrique)
    inst.built = built
    inst.create_image.return_value = {
        "images": [{"data": b"\x89PNG..", "mime_type": "image/png"}], "text": "",
        "usage_metadata": dict(_USAGE), "model_version": _NB, "finish_reason": "STOP",
        "response_id": "resp-1"}
    inst.list_models.return_value = {"models": [
        {"name": f"models/{_NB}", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3-pro-image", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]}]}
    return inst


@pytest.fixture
def depot(monkeypatch):
    vus = []

    def upload(prefix, owner, data, content_type, filename=None, **kw):
        vus.append((prefix, owner, data, content_type, filename))
        return f"https://signed.test/{filename}"

    monkeypatch.setattr("oto_mcp.media_store.upload_private", upload)
    monkeypatch.setattr("oto_mcp.media_store.presign_expiry", lambda: 3600)
    monkeypatch.setattr("oto_mcp.auth.hooks.current_user_sub_from_token", lambda: "s")
    return vus


@pytest.fixture
def releve():
    holder: dict = {}
    jeton = session_org.set_call_trace(holder)
    yield holder
    session_org.reset_call_trace(jeton)


@pytest.fixture
def cle(monkeypatch, releve):
    etat = {"mode": "platform", "units": []}

    def resoudre(provider, account=None, units=1):
        assert provider == "gemini_image"
        etat["units"].append(units)
        session_org.note_call_trace(key_mode=etat["mode"])
        return ("PLAT" if etat["mode"] == "platform" else "OWN"), etat["mode"] == "platform"

    monkeypatch.setattr("oto_mcp.access.resolve_api_key", resoudre)
    return etat


def _tool(name: str):
    from fastmcp import FastMCP
    m = FastMCP("t")
    G.register(m)
    return asyncio.run(m.get_tool(name)).fn


def _creer(**kw):
    return _tool("gemini_image_act")(op="create_image", prompt=kw.pop("prompt", "an apple"), **kw)


# --- price -------------------------------------------------------------------------------

def test_the_bound_is_the_image_a_thinking_allowance_and_the_input():
    p = G.PRICES[_NB]
    attendu = (p["image_tokens"]["2K"] * 30.0 + G.THINKING_ALLOWANCE_TOKENS * 7.5
               + (len("abc") + 2 * G.INPUT_IMAGE_TOKENS) * 1.5) / 1e6
    assert G.bound_usd(_NB, "2K", "abc", 2) == pytest.approx(attendu, abs=1e-6)
    assert G.bound_usd(_NB, None, "", 0) == G.bound_usd(_NB, "1K", "", 0)
    assert G.bound_usd("some-other-model", "1K", "", 0) is None


def test_the_cost_is_what_google_counted_unlisted_output_at_the_text_rate():
    """1,303 output tokens, 1,120 listed as IMAGE: the 183 others and the 433 of thinking
    at the text rate."""
    c = G.cost_of(_NB, _USAGE)
    assert c["usd"] == pytest.approx((11 * 1.5 + 1120 * 30.0 + (183 + 433) * 7.5) / 1e6,
                                     abs=1e-6)


@pytest.mark.parametrize("usage,size", [(_USAGE, "1K"), (_USAGE_4K, "4K")])
def test_the_measured_calls_stay_under_their_bound(usage, size):
    bound = G.bound_usd(_NB, size, "A red apple on a white table, studio photo", 0)
    assert G.cost_of(_NB, usage)["usd"] <= bound


def test_each_modality_is_billed_at_its_own_rate():
    """An edit: the input image at the input rate, the output image at the image rate."""
    usage = {"promptTokenCount": 1131, "candidatesTokenCount": 1219,
             "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 11},
                                     {"modality": "IMAGE", "tokenCount": 1120}],
             "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 1120}],
             "thoughtsTokenCount": 212}
    assert G.cost_of(_NB, usage)["usd"] == pytest.approx(
        (11 * 1.5 + 1120 * 1.5 + 1120 * 30.0 + (99 + 212) * 7.5) / 1e6, abs=1e-6)


def test_a_declined_image_bills_its_input_only():
    usage = {"promptTokenCount": 11,
             "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 11}]}
    assert G.cost_of(_NB, usage)["usd"] == pytest.approx(11 * 1.5 / 1e6, abs=1e-6)


@pytest.mark.parametrize("usage", [
    None, {},
    # output counted, but not by modality: an image billed at a guess
    {"promptTokenCount": 5, "candidatesTokenCount": 1300},
    # a modality without a rate
    {"promptTokenCount": 5, "candidatesTokenCount": 9,
     "candidatesTokensDetails": [{"modality": "AUDIO", "tokenCount": 9}]},
    # a list counting more than the total
    {"promptTokenCount": 5, "candidatesTokenCount": 9,
     "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 1120}]},
    {"promptTokenCount": "5"},
])
def test_without_a_per_modality_breakdown_nothing_is_billed_at_a_guess(usage):
    with pytest.raises(G.UsageUnreadable):
        G.cost_of(_NB, usage)


def test_on_the_shared_key_an_unreadable_usage_refuses_and_notes_nothing(
        client, cle, releve, depot):
    client.create_image.return_value = {**client.create_image.return_value,
                                        "usage_metadata": {"promptTokenCount": 11,
                                                           "candidatesTokenCount": 1303}}
    with pytest.raises(McpError, match=G.USAGE_UNREADABLE):
        _creer()
    assert "units" not in releve and "quantity" not in releve and depot == []


def test_on_the_customers_own_key_an_unreadable_usage_serves_and_says_unknown(
        client, cle, releve, depot):
    cle["mode"] = "byo_user"
    client.create_image.return_value = {**client.create_image.return_value,
                                        "usage_metadata": {}}
    out = _creer()
    assert out["cost"]["usd"] is None and releve["quantity"] == 0 and len(depot) == 1


def test_estimate_is_free_and_calls_nobody(client, releve):
    out = _tool("gemini_image")(op="estimate", image_size="4K", image_count=1)
    assert out["usd"] == G.bound_usd(_NB, "4K", "", 1) and out["priced"]
    assert client.built == [] and releve["quantity"] == 0


# --- shared key ---------------------------------------------------------------------------

def test_on_the_shared_key_the_quota_is_checked_for_the_bound_and_the_cost_noted(
        client, cle, releve, depot):
    out = _creer(image_size="2K", aspect_ratio="16:9")
    assert cle["units"] == [G.microdollars(G.bound_usd(_NB, "2K", "an apple", 0))]
    spent = G.microdollars(G.cost_of(_NB, _USAGE)["usd"])
    assert releve["units"] == spent and releve["quantity"] == spent
    client.create_image.assert_called_once_with("an apple", _NB, images=[], aspect_ratio="16:9",
                                                image_size="2K")
    assert out["images"][0]["url"].startswith("https://signed.test/")
    assert out["images"][0]["encoding"] == "url" and "data" not in out["images"][0]
    assert depot[0][0] == "gemini-image" and depot[0][3] == "image/png"


def test_an_unpriced_model_is_refused_on_the_shared_key_before_google(client, cle, releve, depot):
    with pytest.raises(McpError, match=G.UNPRICED_MODEL):
        _creer(model="gemini-3-pro-image")
    assert client.built == [] and "quantity" not in releve


def test_on_the_customers_own_key_the_cost_is_quantity_never_units(client, cle, releve, depot):
    cle["mode"] = "byo_user"
    _creer()
    assert releve["quantity"] > 0 and "units" not in releve


def test_on_the_customers_own_key_any_model_runs_and_notes_nothing_it_cannot_price(
        client, cle, releve, depot):
    cle["mode"] = "byo_user"
    out = _creer(model="gemini-3-pro-image")
    assert out["cost"]["usd"] is None and releve["quantity"] == 0


def test_max_usd_below_the_bound_refuses_before_anything(client, cle, releve):
    with pytest.raises(McpError, match=G.ABOVE_MAX_USD):
        _creer(image_size="4K", max_usd=0.01)
    assert cle["units"] == [] and client.built == []


@pytest.mark.parametrize("kw", [{"image_size": "4k"}, {"aspect_ratio": "7:3"},
                                {"images": [{"kind": "url", "url": "https://x.test"}] * 15}])
def test_a_bad_shape_is_refused_before_anything(client, cle, kw):
    with pytest.raises(McpError):
        _creer(**kw)
    assert cle["units"] == [] and client.built == []


def test_a_free_tier_key_is_told_to_enable_billing(client, cle, releve):
    from oto.tools.common.errors import UpstreamHTTPError
    err = UpstreamHTTPError.__new__(UpstreamHTTPError)
    err.status_code = 429
    err.body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message":
                          "Quota exceeded for metric: generativelanguage.googleapis.com/"
                          "generate_content_free_tier_requests, limit: 0"}}
    client.create_image.side_effect = err
    with pytest.raises(McpError, match="FREE tier"):
        _creer()
    assert "quantity" not in releve


def test_list_models_keeps_the_image_models_and_marks_the_priced_ones(client, cle, releve):
    out = _tool("gemini_image")(op="list_models")
    assert [(m["name"], m["priced"]) for m in out["models"]] == [
        (_NB, True), ("gemini-3-pro-image", False)]
    assert releve["quantity"] == 0


# --- the shared key is CLOSED without a grant: the real resolution path ------------------
# Same seams as `tests/test_higgsfield_platform_key.py::cle_partagee`.

@pytest.fixture
def cle_partagee(monkeypatch, client, releve, sans_droit_declare):
    from oto_mcp import access, grants_chain
    etat = {"share_down": [], "rate_limit_by": {}, "used": 0}
    monkeypatch.setattr(access, "resolve_api_key", _VRAIE_RESOLUTION)
    monkeypatch.setattr(access.db, "get_member_api_key", lambda sub, org, p: None)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access, "current_org", lambda sub: 42)
    monkeypatch.setattr(access.platform_rate, "enforce", lambda *a, **k: None)
    monkeypatch.setattr(access.credits, "reserver", lambda *a, **k: None)
    # The walk reduced to its PLATFORM rung, computed by the real platform grant (the
    # member, team and org rungs read the vault: no key there is the case at hand).
    from oto_mcp.access import platform_grant

    def platform_only(provider, sub, active_org, **kw):
        payload = platform_grant._resolve_platform_grant(sub, provider, active_org)
        return (access.CascadeRung("platform", "platform", payload["label"], payload)
                if payload else None)

    monkeypatch.setattr(access.chain_shadow, "barreau_gagnant", platform_only)
    monkeypatch.setattr("oto_mcp.connectors.cle_perso.interdite", lambda *a, **k: False)

    monkeypatch.setattr(access.credentials_store, "list_platform_instances", lambda p: [{
        "label": "shared", "share_mode": "closed", "share_down": list(etat["share_down"]),
        "share_side": [], "meta": {"rate_limit_by": dict(etat["rate_limit_by"])}}])
    monkeypatch.setattr(access.credentials_store, "get_credential",
                        lambda et, eid, p, account="": "PLAT")
    # The shared key's quota counts per ORG: the org's counter of the day.
    monkeypatch.setattr("oto_mcp.db.usage_org.du_jour",
                        lambda org, p: etat["used"])
    monkeypatch.setattr(grants_chain.db_grants, "edges_for", lambda ref, grantees: [])
    monkeypatch.setattr("oto_mcp.links.ou_poser_la_cle", lambda *a, **k: "")
    monkeypatch.setattr("oto_mcp.auth.hooks.current_user_sub_from_token", lambda: "user-sub")
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "user-sub")
    return etat


def _accorder(etat, quota):
    etat["share_down"] = ["user:user-sub"]
    etat["rate_limit_by"] = {"user:user-sub": quota}


def test_without_a_grant_the_shared_key_is_never_served(client, cle_partagee):
    with pytest.raises(McpError, match="No `gemini_image` key configured"):
        _creer()
    assert client.built == []


def test_a_call_dearer_than_what_is_left_of_the_quota_is_never_sent(client, cle_partagee):
    _accorder(cle_partagee, 1_000_000)
    cle_partagee["used"] = 1_000_000 - 1_000
    with pytest.raises(McpError):
        _creer(image_size="4K")
    assert client.built == []


def test_with_a_grant_the_shared_key_is_served(client, cle_partagee, depot):
    _accorder(cle_partagee, 10_000_000)
    _creer()
    assert client.built == [{"api_key": "PLAT"}]
