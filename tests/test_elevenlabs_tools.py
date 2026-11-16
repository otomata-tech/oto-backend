"""Tools `elevenlabs` / `elevenlabs_act` and the ElevenLabs probe.

What this file locks — the generic tripwires already cover registry, publisher,
logo, module derivation, served prose, the `<ns>`/`<ns>_act` convention and the
join to the client:
- the SURFACE (the pair) and the routing to the right client method, with the
  resolved key passed as is;
- audio is never inline: deposited privately, served as a signed URL;
- `timestamps` turns the per-character alignment into words;
- intent refusals (an argument of another op, a missing required argument);
- the translation of upstream refusals, and above all an EMPTY account (401
  `quota_exceeded`) told apart from a REFUSED key, by a 402 in the chain;
- the probe: 401 → `NonAutorise`, quota → `QuotaEpuise`, balance read;
- the shared key: closed without a grant (real resolution path), refused before any
  credit once its quota is spent, granted only WITH a quota in credits (every grant
  path), and, on it only, a `text` over `MAX_TEXT_CHARS` refused before the client.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from oto_mcp.connectors import verify as cv
from oto_mcp.mcp_errors import McpError

AUDIO = b"ID3fake"


@pytest.fixture
def client(monkeypatch):
    """The fake client, the kwargs of each construction, and the deposits made."""
    inst, built, deposits = MagicMock(), [], []

    def fabrique(**kw):
        built.append(kw)
        return inst

    def upload_private(prefix, owner_id, data, content_type, filename=None, *, expiry=None):
        deposits.append((prefix, owner_id, data, content_type, filename))
        return f"https://media.test/{filename}?sig=x"

    monkeypatch.setattr("oto.tools.elevenlabs.ElevenLabsClient", fabrique)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None, units=1: ("key-de-test", False))
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "user-sub")
    monkeypatch.setattr("oto_mcp.media_store.upload_private", upload_private)
    inst.built, inst.deposits = built, deposits
    return inst


def _mcp():
    from fastmcp import FastMCP
    from oto_mcp.tools import elevenlabs as E

    m = FastMCP("t")
    E.register(m)
    return m


def _tool(name: str):
    return asyncio.run(_mcp().get_tool(name)).fn


def _upstream(status, body=None):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(status, body or {"detail": {"status": "x", "message": "m"}},
                             service="elevenlabs")


def _audio(**extra):
    return {"content": AUDIO, "content_type": "audio/mpeg", "extension": "mp3",
            "output_format": "mp3_44100_128", **extra}


# --- surface ------------------------------------------------------------------

def test_surface_is_the_pair(client):
    tools = {t.name: t for t in asyncio.run(_mcp().list_tools())}
    assert set(tools) == {"elevenlabs", "elevenlabs_act"}
    assert tools["elevenlabs"].annotations.readOnlyHint is True
    assert tools["elevenlabs_act"].annotations.readOnlyHint is False


def test_registry_declares_one_key_byo_or_a_granted_platform_key():
    from oto_mcp import providers
    c = providers.REGISTRY["elevenlabs"]
    assert c.kind == "tools" and c.secret_kind == "api_key" and c.keyed
    assert c.auth_modes == frozenset({"byo_user", "byo_org", "platform"})
    assert c.platform_key_open is False, "the shared key needs a grant"
    assert c.default_quota == 0


def test_the_key_is_passed_as_is(client):
    client.get_subscription.return_value = {"tier": "creator"}
    assert _tool("elevenlabs")(op="get_subscription") == {"tier": "creator"}
    assert client.built == [{"api_key": "key-de-test"}]


# --- reads ----------------------------------------------------------------------

def test_list_voices_routes_its_filters(client):
    client.list_voices.return_value = {"voices": []}
    _tool("elevenlabs")(op="list_voices", search="warm", category="premade", page_size=20)
    client.list_voices.assert_called_once_with(search="warm", voice_type=None,
                                               category="premade", page_size=20,
                                               next_page_token=None)


def test_get_voice_needs_its_id(client):
    with pytest.raises(McpError, match="requires `voice_id`"):
        _tool("elevenlabs")(op="get_voice")
    client.get_voice.return_value = {"voice_id": "v1"}
    assert _tool("elevenlabs")(op="get_voice", voice_id="v1") == {"voice_id": "v1"}


def test_a_read_refuses_another_ops_argument(client):
    with pytest.raises(McpError, match="does not use `search`"):
        _tool("elevenlabs")(op="list_models", search="x")
    with pytest.raises(McpError, match="does not use `voice_id`"):
        _tool("elevenlabs")(op="list_voices", voice_id="v1")


# --- generations ------------------------------------------------------------------

def test_speech_is_served_as_a_signed_url(client):
    client.create_speech.return_value = _audio(request_id="r1", character_count=11)
    out = _tool("elevenlabs_act")(op="create_speech", voice_id="v1", text="Hello there",
                                  model_id="eleven_v3", seed=7)
    client.create_speech.assert_called_once_with(
        "v1", "Hello there", model_id="eleven_v3", voice_settings=None, previous_text=None,
        next_text=None, seed=7, language_code=None, output_format=None, timestamps=False)
    assert client.deposits == [("elevenlabs-audio", "user-sub", AUDIO, "audio/mpeg", "speech.mp3")]
    assert out["encoding"] == "url" and out["url"].startswith("https://media.test/speech.mp3")
    assert out["size"] == len(AUDIO) and out["character_count"] == 11
    assert "content" not in out and "words" not in out


def test_speech_with_timestamps_gives_words(client):
    alignment = {"characters": list("[calm] Hi there."),
                 "character_start_times_seconds": [0.0] * 7 + [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9],
                 "character_end_times_seconds": [0.0] * 7 + [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]}
    client.create_speech.return_value = _audio(alignment=alignment)
    out = _tool("elevenlabs_act")(op="create_speech", voice_id="v1", text="[calm] Hi there.",
                                  timestamps=True)
    assert out["words"] == [{"word": "Hi", "start": 0.1, "end": 0.3},
                            {"word": "there.", "start": 0.4, "end": 1.0}]
    assert out["alignment"] == alignment


def test_music_from_a_plan(client):
    client.create_music.return_value = _audio()
    plan = {"sections": [{"section_name": "Intro", "duration_ms": 3000}]}
    out = _tool("elevenlabs_act")(op="create_music", composition_plan=plan,
                                  respect_sections_durations=True, seed=3)
    client.create_music.assert_called_once_with(
        prompt=None, composition_plan=plan, music_length_ms=None, model_id=None, seed=3,
        force_instrumental=None, respect_sections_durations=True, output_format=None)
    assert out["filename"] == "music.mp3"


def test_music_refuses_a_speech_argument(client):
    with pytest.raises(McpError, match="does not use `voice_id`"):
        _tool("elevenlabs_act")(op="create_music", prompt="calm", voice_id="v1")
    client.create_music.assert_not_called()


def test_sound(client):
    client.create_sound.return_value = _audio()
    _tool("elevenlabs_act")(op="create_sound", text="a soft click", duration_seconds=1.0)
    client.create_sound.assert_called_once_with(
        "a soft click", duration_seconds=1.0, prompt_influence=None, loop=None,
        model_id=None, output_format=None)


def test_voice_design_serves_each_preview(client):
    client.create_voice_design.return_value = {"text": "Read this.", "previews": [
        {**_audio(), "generated_voice_id": "g1", "duration_secs": 2.0, "language": "en"},
        {**_audio(), "generated_voice_id": "g2", "duration_secs": 2.1, "language": "en"}]}
    out = _tool("elevenlabs_act")(op="create_voice_design",
                                  voice_description="A calm, low narrator.", text="Read this.")
    assert [p["generated_voice_id"] for p in out["previews"]] == ["g1", "g2"]
    assert [p["filename"] for p in out["previews"]] == ["voice-preview-1.mp3", "voice-preview-2.mp3"]
    assert all("content" not in p for p in out["previews"])


def test_speech_needs_voice_and_text(client):
    with pytest.raises(McpError, match="requires `voice_id`"):
        _tool("elevenlabs_act")(op="create_speech", text="x")
    with pytest.raises(McpError, match="requires `text`"):
        _tool("elevenlabs_act")(op="create_speech", voice_id="v1")


def test_storage_down_is_said(client, monkeypatch):
    from oto_mcp import media_store

    def down(*a, **k):
        raise media_store.MediaError(503, "storage_unavailable", "no bucket")

    monkeypatch.setattr("oto_mcp.media_store.upload_private", down)
    client.create_sound.return_value = _audio()
    with pytest.raises(McpError, match="temporary storage is unavailable"):
        _tool("elevenlabs_act")(op="create_sound", text="click")


# --- upstream refusals ------------------------------------------------------------

def _chain(exc):
    seen, cur = [], exc
    while cur is not None:
        seen.append(cur)
        cur = cur.__cause__ or cur.__context__
    return seen


def test_empty_account_is_a_402_not_a_dead_key(client):
    client.create_speech.side_effect = _upstream(401, {"detail": {"status": "quota_exceeded",
                                                                  "message": "quota"}})
    with pytest.raises(McpError, match="no credits left") as e:
        _tool("elevenlabs_act")(op="create_speech", voice_id="v1", text="x")
    assert any(getattr(x, "status_code", None) == 402 for x in _chain(e.value))


def test_restricted_key_is_not_marked_dead(client):
    client.list_voices.side_effect = _upstream(401, {"detail": {"status": "missing_permissions",
                                                                "message": "voices_read"}})
    with pytest.raises(McpError, match="restricted") as e:
        _tool("elevenlabs")(op="list_voices")
    assert not any(getattr(x, "status_code", None) == 401 for x in _chain(e.value))


def test_refused_key_keeps_its_401_in_the_chain(client):
    client.get_subscription.side_effect = _upstream(401, {"detail": {"status": "invalid_api_key"}})
    with pytest.raises(McpError, match="refused the API key") as e:
        _tool("elevenlabs")(op="get_subscription")
    assert any(getattr(x, "status_code", None) == 401 for x in _chain(e.value))


def test_local_refusal_of_the_client_is_served(client):
    client.create_music.side_effect = ValueError("Give either `prompt` or `composition_plan`")
    with pytest.raises(McpError, match="Give either"):
        _tool("elevenlabs_act")(op="create_music")


# --- probe ------------------------------------------------------------------------

def _probe_with(monkeypatch, behaviour):
    inst = MagicMock()
    inst.probe.side_effect = behaviour if isinstance(behaviour, Exception) else None
    if not isinstance(behaviour, Exception):
        inst.probe.return_value = behaviour
    monkeypatch.setattr("oto.tools.elevenlabs.ElevenLabsClient", lambda **kw: inst)
    from oto_mcp.tools import elevenlabs as E
    return E._verify


def test_probe_ok(monkeypatch):
    verify = _probe_with(monkeypatch, {"tier": "creator", "character_count": 10,
                                       "character_limit": 100})
    verify({"key": "k"})


def test_probe_returns_the_balance_it_read(monkeypatch):
    """`auth+quota` with `balance: null` read as "not measured" every morning: the
    probe now hands back what it read."""
    verify = _probe_with(monkeypatch, {"tier": "creator", "character_count": 30,
                                       "character_limit": 100})
    assert verify({"key": "k"}) == {"quota": {"restant": 70, "unite": "credits",
                                              "limite": 100}}
    verify = _probe_with(monkeypatch, {"tier": "creator"})
    assert verify({"key": "k"}) is None


def test_probe_refused_key(monkeypatch):
    verify = _probe_with(monkeypatch, _upstream(401, {"detail": {"status": "invalid_api_key"}}))
    with pytest.raises(cv.NonAutorise):
        verify({"key": "k"})


def test_probe_empty_account(monkeypatch):
    verify = _probe_with(monkeypatch, {"tier": "creator", "character_count": 100,
                                       "character_limit": 100})
    with pytest.raises(cv.QuotaEpuise):
        verify({"key": "k"})
    verify = _probe_with(monkeypatch, _upstream(401, {"detail": {"status": "quota_exceeded"}}))
    with pytest.raises(cv.QuotaEpuise):
        verify({"key": "k"})


def test_probe_coverage_is_auth_and_quota(client):
    _mcp()
    assert cv.couverture("elevenlabs") == cv.AUTH_QUOTA


# --- tightened lists ----------------------------------------------------------------

def test_voice_list_is_tightened_by_default_and_says_so(client):
    raw = {"voices": [{"voice_id": "v1", "name": "A", "samples": [{"x": 1}],
                       "fine_tuning": {"state": {}}, "labels": {"accent": "us"}}],
           "has_more": False}
    client.list_voices.return_value = raw
    out = _tool("elevenlabs")(op="list_voices")
    assert out["voices"] == [{"voice_id": "v1", "name": "A", "labels": {"accent": "us"}}]
    assert out["projection"]["omitted"] == ["fine_tuning", "samples"]
    assert _tool("elevenlabs")(op="list_voices", full=True) == raw


def test_model_list_is_a_list_and_stays_one_under_projection(client):
    client.list_models.return_value = [{"model_id": "m1", "languages": [{"x": 1}]}]
    out = _tool("elevenlabs")(op="list_models")
    assert out == {"items": [{"model_id": "m1"}],
                   "projection": {"omitted": ["languages"], "hint": "full=true returns the raw records."}}


def test_full_belongs_to_the_lists(client):
    with pytest.raises(McpError, match="does not use `full`"):
        _tool("elevenlabs")(op="get_subscription", full=True)


# --- the platform key: speech by default, metered in ElevenLabs credits --------------

@pytest.fixture
def platform(monkeypatch, client):
    """The shared key wins the cascade; `quota` = the `units` the resolver was asked to
    cover."""
    quota: list = []

    def resolve(provider, account=None, units=1):
        quota.append(units)
        return ("platform-key", True)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key", resolve)
    monkeypatch.setattr("oto_mcp.auth.hooks.current_user_sub_from_token", lambda: "user-sub")
    monkeypatch.setattr("oto_mcp.access.current_org", lambda sub: 1)
    monkeypatch.setattr("oto_mcp.links.ou_poser_la_cle", lambda *a, **k: "")
    return quota


@pytest.fixture
def trace(monkeypatch):
    seen: list = []
    monkeypatch.setattr("oto_mcp.session_org.note_call_trace", lambda **kw: seen.append(kw))
    return seen


@pytest.fixture
def setting(monkeypatch):
    """The `platform.ops` row of `connector_settings`: None = unset."""
    row = {"value": None}

    def _get(scope_type, scope_id, connector, key, *a, **kw):
        assert (scope_type, scope_id, connector, key) == (
            "platform", "platform", "elevenlabs", "platform.ops")
        return row["value"]
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting", _get)
    return row


@pytest.mark.parametrize("model_id, chars, credits", [
    ("eleven_flash_v2_5", 11, 6),          # half rate, rounded UP to a whole credit
    ("eleven_turbo_v2", 10, 5),
    ("eleven_flash_v2", 1, 1),
    ("eleven_turbo_v2_5", 0, 0),
    ("eleven_multilingual_v2", 11, 11),
    ("eleven_v3", 11, 11),
    (None, 11, 11),                        # the API's default model: full rate
    ("some_future_model", 7, 7),
])
def test_speech_credits(model_id, chars, credits):
    from oto_mcp.tools import elevenlabs as E
    assert E.speech_credits("x" * 99, model_id, chars) == credits
    assert isinstance(E.speech_credits("x", model_id, chars), int)


def test_speech_credits_fall_back_on_the_texts_length():
    from oto_mcp.tools import elevenlabs as E
    assert E.speech_credits("Hello there", "eleven_flash_v2_5") == 6
    assert E.speech_credits("Hello there", None, None) == 11


def test_platform_speech_runs_and_notes_credits_both_axes(client, platform, trace, setting):
    client.create_speech.return_value = _audio(character_count=11)
    out = _tool("elevenlabs_act")(op="create_speech", voice_id="v1", text="Hello there",
                                  model_id="eleven_flash_v2_5")
    assert out["url"].startswith("https://media.test/")
    assert client.built == [{"api_key": "platform-key"}]
    assert platform == [6], "the grant's quota is checked for the call's credits"
    assert trace == [{"units": 6, "quantity": 6}]


def test_platform_speech_without_the_header_counts_the_text(client, platform, trace, setting):
    client.create_speech.return_value = _audio()
    _tool("elevenlabs_act")(op="create_speech", voice_id="v1", text="Hello there")
    assert trace == [{"units": 11, "quantity": 11}]


@pytest.mark.parametrize("kwargs", [
    {"op": "create_music", "prompt": "calm piano"},
    {"op": "create_sound", "text": "a soft click"},
    {"op": "create_voice_design", "voice_description": "calm", "text": "Hi."},
])
def test_unset_the_platform_key_refuses_every_other_generation(
        client, platform, trace, setting, kwargs):
    with pytest.raises(McpError, match="needs your own ElevenLabs key"):
        _tool("elevenlabs_act")(**kwargs)
    assert not client.method_calls, "a refused op must not reach ElevenLabs (no credit spent)"
    assert trace == []


def test_the_setting_opens_another_generation(client, platform, trace, setting):
    setting["value"] = "elevenlabs_act:create_sound"
    client.create_sound.return_value = _audio(character_count=40)
    _tool("elevenlabs_act")(op="create_sound", text="a soft click")
    assert trace == [{"units": 40, "quantity": 40}]
    with pytest.raises(McpError, match="needs your own ElevenLabs key"):
        _tool("elevenlabs_act")(op="create_speech", voice_id="v1", text="Hi")
    setting["value"] = "elevenlabs_act"
    client.create_music.return_value = _audio(character_count=500)
    _tool("elevenlabs_act")(op="create_music", prompt="calm piano", music_length_ms=20_000)
    assert trace[-1] == {"units": 500, "quantity": 500}, "a header above the length wins"


@pytest.mark.parametrize("length_ms, credits", [
    (20_000, 300), (50_000, 750), (60_000, 900), (1, 1), (None, 1), (0, 1),
])
def test_music_credits(length_ms, credits):
    from oto_mcp.tools import elevenlabs as E
    assert E.music_credits(length_ms) == credits


def test_music_length_from_the_length_or_the_plan():
    from oto_mcp.tools import elevenlabs as E
    plan = {"sections": [{"duration_ms": 4000}, {"duration_ms": 42_000}, {"duration_ms": 4000}]}
    assert E.asked_music_ms(None, plan) == 50_000
    assert E.asked_music_ms(30_000, plan) == 30_000
    assert E.asked_music_ms(None, {"sections": [{"section_name": "x"}]}) is None
    assert E.asked_music_ms(None, None) is None
    assert E.asked_music_ms(True, None) is None


def test_platform_music_bills_its_length_when_elevenlabs_sends_no_count(
        client, platform, trace, setting):
    setting["value"] = "elevenlabs_act:create_speech,elevenlabs_act:create_music"
    client.create_music.return_value = _audio()
    plan = {"sections": [{"duration_ms": 4000}, {"duration_ms": 42_000}, {"duration_ms": 4000}]}
    _tool("elevenlabs_act")(op="create_music", composition_plan=plan,
                            respect_sections_durations=True)
    assert platform == [750], "the grant's quota is checked for the track before the call"
    assert trace == [{"units": 750, "quantity": 750}]


def test_platform_music_without_a_length_is_refused_before_any_credit(
        client, platform, trace, setting):
    setting["value"] = "elevenlabs_act:create_music"
    with pytest.raises(McpError, match="music_length_required"):
        _tool("elevenlabs_act")(op="create_music", prompt="calm piano")
    assert not client.method_calls
    assert trace == []


def test_own_key_music_needs_no_length(client, trace, setting):
    client.create_music.return_value = _audio()
    _tool("elevenlabs_act")(op="create_music", prompt="calm piano")
    assert client.create_music.called
    assert trace == [{"quantity": 10}], "no length: the prompt's estimate, as before"


def test_platform_reads_are_free_but_the_balance_is_never_served(
        client, platform, trace, setting):
    client.list_voices.return_value = {"voices": []}
    _tool("elevenlabs")(op="list_voices")
    assert trace == [{"units": 0}], "a read: no quantity, 0 units"
    with pytest.raises(McpError, match="needs your own ElevenLabs key"):
        _tool("elevenlabs")(op="get_subscription")
    client.get_subscription.assert_not_called()


def test_own_key_notes_the_quantity_only_and_ignores_the_setting(client, trace, setting):
    setting["value"] = "elevenlabs_act:create_sound"
    client.create_speech.return_value = _audio(character_count=10)
    _tool("elevenlabs_act")(op="create_speech", voice_id="v1", text="0123456789")
    assert trace == [{"quantity": 10}]
    client.list_voices.return_value = {"voices": []}
    _tool("elevenlabs")(op="list_voices")
    assert trace == [{"quantity": 10}], "a read on the own key notes nothing"


def test_a_failed_generation_notes_nothing(client, platform, trace, setting):
    client.create_speech.side_effect = _upstream(500)
    with pytest.raises(McpError):
        _tool("elevenlabs_act")(op="create_speech", voice_id="v1", text="Hi")
    assert trace == []


@pytest.mark.parametrize("value, error", [
    ("elevenlabs", "not an ElevenLabs tool"),
    ("elevenlabs_act:nope", "has no op"),
    ("forager_person", "not an ElevenLabs tool"),
])
def test_the_setting_refuses_what_the_tool_would_not_understand(value, error):
    from oto_mcp.tools import elevenlabs as E
    with pytest.raises(ValueError, match=error):
        E.parse_platform_ops(value)


def test_the_default_is_speech_only():
    from oto_mcp.tools import elevenlabs as E
    assert E.parse_platform_ops(E.PLATFORM_OPS_DEFAULT) == {("elevenlabs_act", "create_speech")}


def test_an_unreadable_setting_RAISES_rather_than_serving_the_default(monkeypatch):
    from oto_mcp.tools import elevenlabs as E
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **kw: "elevenlabs_act:gone")
    with pytest.raises(RuntimeError, match="not understood"):
        E.platform_ops()


# --- the shared key is CLOSED without a grant: the real resolution path -----------------
#
# `access.resolve_api_key` is the REAL one here (taken before any fixture stubs it): the
# cascade, the platform rung and its quota run as in production; only what reads the
# database is stubbed — no own key, no org, the vault's platform instance, the day's usage
# (same seams as `tests/test_free_tier_platform_key.py`).

from oto_mcp import access as _access  # noqa: E402

#: Read on the facade at import, before any fixture stubs it.
_VRAIE_RESOLUTION = _access.resolve_api_key


@pytest.fixture
def cle_partagee(monkeypatch, client, trace, sans_droit_declare):
    """The shared ElevenLabs key in the vault, CLOSED (`share_mode='closed'`); `etat`
    holds who it is granted to (`share_down`), the grant's quota and today's usage."""
    from oto_mcp import access, grants_chain
    etat = {"share_down": [], "rate_limit_by": {}, "used": 0}
    monkeypatch.setattr(access, "resolve_api_key", _VRAIE_RESOLUTION)
    monkeypatch.setattr(access.db, "get_member_api_key", lambda sub, org, p: None)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(access.credentials_store, "list_platform_instances", lambda p: [{
        "label": "shared", "share_mode": "closed", "share_down": list(etat["share_down"]),
        "share_side": [], "meta": {"rate_limit_by": dict(etat["rate_limit_by"])}}])
    monkeypatch.setattr(access.credentials_store, "get_credential",
                        lambda et, eid, p, account="": "PLAT")
    monkeypatch.setattr(access.db, "get_usage_today", lambda sub, p: etat["used"])
    monkeypatch.setattr(grants_chain.db_grants, "edges_for", lambda ref, grantees: [])
    monkeypatch.setattr("oto_mcp.links.ou_poser_la_cle", lambda *a, **k: "")
    return etat


def _accorder(etat, quota):
    etat["share_down"] = ["user:user-sub"]
    etat["rate_limit_by"] = {"user:user-sub": quota}


def test_without_a_grant_the_shared_key_is_never_served(client, cle_partagee):
    with pytest.raises(McpError, match="No `elevenlabs` key configured"):
        _tool("elevenlabs_act")(op="create_speech", voice_id="v1", text="Hello there")
    assert client.built == [], "no client built: the shared key was not served"
    assert not client.method_calls


def test_with_a_grant_the_shared_key_is_served(client, cle_partagee, setting):
    _accorder(cle_partagee, 100)
    client.create_speech.return_value = _audio(character_count=11)
    _tool("elevenlabs_act")(op="create_speech", voice_id="v1", text="Hello there")
    assert client.built == [{"api_key": "PLAT"}]


def test_an_exhausted_quota_refuses_before_any_credit_is_spent(client, cle_partagee, setting):
    _accorder(cle_partagee, 100)
    cle_partagee["used"] = 100
    with pytest.raises(McpError, match="Platform quota elevenlabs exceeded"):
        _tool("elevenlabs_act")(op="create_speech", voice_id="v1", text="Hello there")
    assert client.built == [] and not client.method_calls


def test_a_call_bigger_than_what_is_left_refuses_before_any_credit_is_spent(
        client, cle_partagee, setting):
    """95 of 100 credits spent, the line costs 11: refused whole, never served on credit."""
    _accorder(cle_partagee, 100)
    cle_partagee["used"] = 95
    with pytest.raises(McpError):
        _tool("elevenlabs_act")(op="create_speech", voice_id="v1", text="Hello there")
    assert client.built == [] and not client.method_calls


# --- the text ceiling, on the SHARED key only: refused before the client exists --------

def test_on_the_shared_key_a_text_over_the_ceiling_is_refused_before_the_client(
        client, platform, trace, setting):
    """The key is resolved (it is what says the shared key serves), then the refusal
    comes BEFORE the client is built: nothing called, nothing noted, nothing settled."""
    from oto_mcp.tools import elevenlabs as E
    setting["value"] = "elevenlabs_act"   # every op served: the ceiling refuses, not the ops
    ops = ({"op": "create_speech", "voice_id": "v1"},
           {"op": "create_sound"},
           {"op": "create_voice_design", "voice_description": "calm"})
    for kwargs in ops:
        with pytest.raises(McpError, match="text_too_long") as e:
            _tool("elevenlabs_act")(text="x" * (E.MAX_TEXT_CHARS + 1), **kwargs)
        assert str(E.MAX_TEXT_CHARS) in str(e.value), "the refusal says the ceiling"
        assert "shared key" in str(e.value) and "your own ElevenLabs key" in str(e.value)
    assert len(platform) == len(ops), "the resolution was called, once per op"
    assert client.built == [] and not client.method_calls and trace == []


def test_on_the_shared_key_a_text_at_the_ceiling_is_served(client, platform, trace, setting):
    from oto_mcp.tools import elevenlabs as E
    client.create_speech.return_value = _audio(character_count=E.MAX_TEXT_CHARS)
    _tool("elevenlabs_act")(op="create_speech", voice_id="v1",
                            text="x" * E.MAX_TEXT_CHARS)
    assert platform == [E.MAX_TEXT_CHARS]
    assert client.create_speech.called


def test_on_the_own_key_a_text_over_the_ceiling_is_served(client, trace, setting):
    """The customer's own key loses nothing: ElevenLabs bounds it, not oto."""
    from oto_mcp.tools import elevenlabs as E
    client.create_speech.return_value = _audio(character_count=E.MAX_TEXT_CHARS + 1)
    _tool("elevenlabs_act")(op="create_speech", voice_id="v1",
                            text="x" * (E.MAX_TEXT_CHARS + 1))
    assert client.built == [{"api_key": "key-de-test"}]
    assert client.create_speech.called
    assert trace == [{"quantity": E.MAX_TEXT_CHARS + 1}]


def test_the_served_description_says_the_ceiling():
    """The docstring writes the number: tied here to the constant, its single source."""
    from oto_mcp.tools import elevenlabs as E
    tool = asyncio.run(_mcp().get_tool("elevenlabs_act"))
    assert f"{E.MAX_TEXT_CHARS:,}" in tool.description
    assert str(E.MAX_TEXT_CHARS) in f"{tool.description}{tool.parameters}", (
        "the `text` argument says its ceiling")


def test_the_served_description_says_how_music_is_billed():
    """The music rate is written out, tied to its constant, with the shared key's rule."""
    from oto_mcp.tools import elevenlabs as E
    tool = asyncio.run(_mcp().get_tool("elevenlabs_act"))
    assert f"~{E.MUSIC_CREDITS_PER_MINUTE} credits a minute" in tool.description
    assert "the length is required" in tool.description


# --- a grant of the shared key REQUIRES a daily quota, in ElevenLabs credits ----------

def _ctx():
    from oto_mcp.capabilities._types import ResolvedCtx
    return ResolvedCtx(sub="admin", org_id=1)


@pytest.fixture
def octrois(monkeypatch):
    """Every grant path's reads stubbed; `faits` = what was WRITTEN (grants, option marks)."""
    from oto_mcp import credentials_store
    from oto_mcp.capabilities import platform_connectors as pc, users_admin as ua
    faits: list = []
    monkeypatch.setattr(credentials_store, "list_platform_instances",
                        lambda p: [{"label": "shared"}])
    monkeypatch.setattr(credentials_store, "platform_grant",
                        lambda p, s, daily_quota=None, label=None:
                        faits.append(("grant", p, s, daily_quota)))
    monkeypatch.setattr(ua.db, "get_user", lambda s: {"sub": s})
    monkeypatch.setattr(ua.org_store, "get_org", lambda oid: {"id": oid})
    monkeypatch.setattr(ua.group_store, "get_group", lambda gid: {"id": gid})
    monkeypatch.setattr(ua.db, "set_option_comp",
                        lambda *a, **k: faits.append(("option",) + a))
    monkeypatch.setattr(pc.access, "paid_option_for", lambda n: None)
    monkeypatch.setattr(pc.db, "set_option_comp",
                        lambda *a, **k: faits.append(("option",) + a))
    return faits


def _par_chaque_chemin(daily_quota):
    """One grant of the shared ElevenLabs key by each path, `daily_quota` passed where
    the path carries one."""
    from oto_mcp.capabilities import admin_console as ac, platform_connectors as pc
    from oto_mcp.capabilities import users_admin as ua
    ctx = _ctx()
    return {
        "platform.key.grant": lambda: ua._grant_key(ctx, ua.GrantKeyInput(
            target="u1", provider="elevenlabs", daily_quota=daily_quota)),
        "platform.org.grant_key": lambda: ua._grant_org_key(ctx, ua.OrgGrantKeyInput(
            org_id=2, provider="elevenlabs", daily_quota=daily_quota)),
        "platform.group.grant_key": lambda: ua._grant_group_key(ctx, ua.GroupGrantKeyInput(
            group_id=3, provider="elevenlabs", daily_quota=daily_quota)),
        "oto_admin_key_grant user": lambda: ac._key_grant(ctx, ac.KeyGrantInput(
            op="grant", scope="user", target="u1", provider="elevenlabs",
            daily_quota=daily_quota)),
        "oto_admin_key_grant org": lambda: ac._key_grant(ctx, ac.KeyGrantInput(
            op="grant", scope="org", org_id=2, provider="elevenlabs",
            daily_quota=daily_quota)),
        "oto_admin_key_grant group": lambda: ac._key_grant(ctx, ac.KeyGrantInput(
            op="grant", scope="group", group_id=3, provider="elevenlabs",
            daily_quota=daily_quota)),
        # The gestures that carry no quota: refused whatever `daily_quota` is.
        "platform.connector.access_set": lambda: pc._set_platform_access(
            ctx, pc.PlatformAccessSetInput(provider="elevenlabs", scope="org", id=2, on=True)),
        "platform.option.set": lambda: ua._set_option(ctx, ua.OptionInput(
            entity_type="org", entity_id="2", option="elevenlabs", on=True)),
    }


@pytest.mark.parametrize("daily_quota", [None, 0])
def test_a_grant_without_a_quota_is_refused_by_every_path(octrois, daily_quota):
    from oto_mcp.capabilities._types import AuthzDenied
    for chemin, octroyer in _par_chaque_chemin(daily_quota).items():
        with pytest.raises(AuthzDenied) as e:
            octroyer()
        assert e.value.status == 400 and e.value.code == "grant_quota_required", chemin
        assert "daily_quota" in str(e.value) and "ElevenLabs credits" in str(e.value), chemin
    assert octrois == [], "nothing written: neither a grant nor an option mark"


def test_a_grant_with_a_quota_in_credits_goes_through(octrois):
    sans_quota = {"platform.connector.access_set", "platform.option.set"}
    for chemin, octroyer in _par_chaque_chemin(500).items():
        if chemin not in sans_quota:
            octroyer()
    assert octrois and all(f == ("grant", "elevenlabs", f[2], 500) for f in octrois)
    assert len(octrois) == 6


def test_the_store_itself_refuses_a_grant_without_a_quota(monkeypatch):
    """The rule holds BELOW the console too: `platform_grant` plays it before touching
    the vault — a script or a future path cannot grant unlimited."""
    from oto_mcp import credentials_store
    monkeypatch.setattr(credentials_store, "_connect",
                        lambda: pytest.fail("the vault must not be touched"))
    for quota in (None, 0, -5):
        with pytest.raises(credentials_store.QuotaDOctroiRequis, match="ElevenLabs credits"):
            credentials_store.platform_grant("elevenlabs", "org:2", daily_quota=quota)


def test_the_quota_rule_is_declared_by_the_connector_only():
    from oto_mcp import credentials_store, providers
    assert providers.REGISTRY["elevenlabs"].grant_quota_unit == "ElevenLabs credits"
    assert providers.REGISTRY["serper"].grant_quota_unit == ""
    credentials_store.exiger_quota_d_octroi("serper", None)   # optional elsewhere


def test_the_grant_tools_say_the_unit():
    from oto_mcp.capabilities import admin_console, users_admin  # noqa: F401 — declare
    from oto_mcp.capabilities.registry import by_key
    for key in ("admin.key_grant", "platform.key.grant", "platform.org.grant_key",
                "platform.group.grant_key"):
        assert "`elevenlabs` (ElevenLabs credits)" in by_key(key).description, key


# --- transcription (Scribe): billed per second of audio, in ElevenLabs credits ---------

@pytest.fixture
def clip(monkeypatch):
    """`file_source.resolve` returns a fixed clip; `seen` = the sources asked for."""
    from oto_mcp import file_source
    seen: list = []

    def resolve(source, *, max_bytes=None, **kw):
        seen.append((source, max_bytes))
        return file_source.ResolvedFile(b"webm-bytes", "dictation.webm", "audio/webm")
    monkeypatch.setattr("oto_mcp.file_source.resolve", resolve)
    return seen


_SRC = {"kind": "transit", "ref": "copilot-dictation/" + "a" * 32}
#: The instance opens transcription on the shared key: it is not in the default.
_OUVERT = "elevenlabs_act:create_speech,elevenlabs_act:create_transcript"


@pytest.mark.parametrize("seconds, credits", [(None, 1), (0.1, 1), (6, 33), (60, 330),
                                              (10.01, 56)])
def test_transcribe_credits(seconds, credits):
    from oto_mcp.tools import elevenlabs as E
    assert E.transcribe_credits(seconds) == credits


def test_transcribe_on_the_platform_key_bills_the_longer_of_length_and_words(
        client, platform, trace, setting, clip):
    setting["value"] = _OUVERT
    client.transcribe_speech.return_value = {
        "text": "Bonjour Marie", "language_code": "fra",
        "words": [{"text": "Bonjour", "start": 0.1, "end": 0.6, "type": "word"},
                  {"text": " ", "start": 0.6, "end": 0.7, "type": "spacing"},
                  {"text": "Marie", "start": 0.7, "end": 12.0, "type": "word"}]}
    out = _tool("elevenlabs_act")(op="create_transcript", source=_SRC, audio_seconds=6,
                                  language_code="fr")
    assert clip == [(_SRC, 10 * 1024 * 1024)]
    assert platform == [33], "the quota is checked against the declared length"
    client.transcribe_speech.assert_called_once_with(
        b"webm-bytes", filename="dictation.webm", content_type="audio/webm",
        model_id="scribe_v2", language_code="fr")
    assert trace == [{"quantity": 66, "units": 66}], "12 s heard beats 6 s declared"
    assert out["text"] == "Bonjour Marie"
    assert [w["word"] for w in out["words"]] == ["Bonjour", "Marie"]


def test_transcribe_on_the_customers_key_notes_quantity_only(client, trace, setting, clip):
    client.transcribe_speech.return_value = {"text": "Hi", "words": []}
    _tool("elevenlabs_act")(op="create_transcript", source=_SRC, audio_seconds=2)
    assert trace == [{"quantity": 11}]


def test_transcribe_too_long_on_the_shared_key_is_refused_before_elevenlabs(
        client, platform, trace, setting, clip):
    setting["value"] = _OUVERT
    with pytest.raises(McpError, match="audio_too_long"):
        _tool("elevenlabs_act")(op="create_transcript", source=_SRC, audio_seconds=301)
    assert not client.method_calls and trace == []


def test_transcribe_unset_the_shared_key_does_not_transcribe(
        client, platform, trace, setting, clip):
    with pytest.raises(McpError, match="does not transcribe"):
        _tool("elevenlabs_act")(op="create_transcript", source=_SRC, audio_seconds=3)
    assert not client.method_calls and trace == []


def test_transcribe_without_a_length_on_the_shared_key_is_refused_before_elevenlabs(
        client, platform, trace, setting, clip):
    setting["value"] = _OUVERT
    with pytest.raises(McpError, match="audio_seconds_required") as e:
        _tool("elevenlabs_act")(op="create_transcript", source=_SRC)
    assert "own ElevenLabs key" in str(e.value) and "Nothing was done." in str(e.value)
    assert not client.method_calls and trace == []


def test_transcribe_too_large_on_the_shared_key_is_refused_before_elevenlabs(
        client, platform, trace, setting, clip, monkeypatch):
    from oto_mcp.tools import elevenlabs as E
    setting["value"] = _OUVERT
    monkeypatch.setattr(E, "MAX_TRANSCRIBE_BYTES_PLATFORM", len(b"webm-bytes") - 1)
    with pytest.raises(McpError, match="audio_too_large"):
        _tool("elevenlabs_act")(op="create_transcript", source=_SRC, audio_seconds=3)
    assert not client.method_calls and trace == []


def test_transcribe_on_the_customers_key_needs_no_length(client, trace, setting, clip):
    client.transcribe_speech.return_value = {"text": "Hi", "words": []}
    _tool("elevenlabs_act")(op="create_transcript", source=_SRC)
    assert trace == [{"quantity": 1}]


def test_transcribe_closed_by_the_setting(client, platform, trace, setting, clip):
    setting["value"] = "elevenlabs_act:create_speech"
    with pytest.raises(McpError, match="does not transcribe"):
        _tool("elevenlabs_act")(op="create_transcript", source=_SRC, audio_seconds=3)
    assert not client.method_calls and trace == []


def test_transcribe_unreadable_source_spends_nothing(client, platform, trace, monkeypatch):
    from oto_mcp import file_source

    def refuse(source, **kw):
        raise file_source.FileSourceError("expired")
    monkeypatch.setattr("oto_mcp.file_source.resolve", refuse)
    with pytest.raises(McpError, match="could not be read"):
        _tool("elevenlabs_act")(op="create_transcript", source=_SRC, audio_seconds=3)
    assert platform == [] and trace == []


@pytest.mark.parametrize("kwargs", [
    {"source": None},
    {"source": _SRC, "voice_id": "v1"},
    {"source": _SRC, "audio_seconds": -1},
])
def test_transcribe_intent_refusals(client, clip, kwargs):
    with pytest.raises(McpError):
        _tool("elevenlabs_act")(op="create_transcript", **kwargs)
    assert not client.method_calls


def test_speech_refuses_a_source(client):
    with pytest.raises(McpError, match="does not use `source`"):
        _tool("elevenlabs_act")(op="create_speech", voice_id="v1", text="Hi", source=_SRC)
