"""ElevenLabs — voices and models, the credit balance, and audio generation.

Wraps `oto.tools.elevenlabs.ElevenLabsClient` (`xi-api-key` header), key resolved
per call via `access.resolve_api_key("elevenlabs")`: the customer's own (user or org)
first, else the SHARED (platform) key when it is granted to the org.

**The shared key** serves the generations THIS INSTANCE chose (`platform_ops`, the
`platform.ops` connector setting); unset, speech only. Any other generation is refused
BEFORE a credit is spent, naming what the shared key serves. The reads (voices, one
voice, models) are served on it, free; `get_subscription` never is: on the shared key,
that account is ours.

**Metering.** Every generation notes the ElevenLabs credits it spent:
`quantity` (the `tool_calls.quantity` column, which the instance's billing prices per
unit, on the customer's own key too — it filters on `key_mode`) and, on the shared key
only, `units` (the grant's quota axis). Speech: `ceil(characters × the model's rate)`,
characters from ElevenLabs' `x-character-count` header (else the text's length), rate
0.5 for the flash and turbo models, 1 otherwise — the API's default model when
`model_id` is omitted (`eleven_multilingual_v2`) included. Music: the length asked for,
at `MUSIC_CREDITS_PER_MINUTE` (ElevenLabs sends no count for it). The reads note 0.

**Surface** (the `<ns>` / `<ns>_act` pair):
- `elevenlabs` (read-only) — the account's credits, its voices, one voice, the
  models;
- `elevenlabs_act` — the four generations, each of which spends credits:
  speech (optionally with each word's start and end), music, a sound effect,
  and previews of a voice described in words; and transcription (Scribe), billed
  per second of audio.

Audio never travels inline: it is deposited in private temporary storage and
served as a signed URL (`media_store.upload_private`), valid
`media_store.presign_expiry()` seconds and deleted after a day.

Calls to the client are written out in full (`client, served = _client(…)` then
`client.create_speech(…)`): this is what makes them verifiable by the version-skew probe
(`test_tools_client_methods_exist`).
"""
from __future__ import annotations

import logging
import math
from typing import Any, Callable, Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, session_org
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError
from .lecture import ACTE, LECTURE

logger = logging.getLogger(__name__)

_NAME = "elevenlabs"
_PREFIX = "elevenlabs-audio"

_READ_OPS = ("get_subscription", "list_voices", "get_voice", "list_models")
_ACT_OPS = ("create_speech", "create_music", "create_sound", "create_voice_design",
            "create_transcript")

# Keys removed from the LISTS by default, at any depth: a voice's training data
# (samples, fine-tuning state), its library sharing record, verification and safety
# details, the per-tier and per-language matrices. `get_voice` keeps everything.
_DROP_VOICES = frozenset({
    "samples", "fine_tuning", "sharing", "verified_languages", "voice_verification",
    "high_quality_base_model_ids", "safety_control", "permission_on_resource",
    "available_for_tiers", "favorited_at_unix", "is_bookmarked",
})
_DROP_MODELS = frozenset({"languages", "concurrency_group", "model_rates",
                          "requires_alpha_access", "serves_pro_voices"})
_HINT = "full=true returns the raw records."

# What OUR shared (platform) ElevenLabs key generates is a choice of the INSTANCE, not of
# the code (same device as `tools/forager.py`): the `platform.ops` setting
# (`connector_settings`, platform scope, `oto_admin_connector_setting`) lists them,
# comma-separated, `elevenlabs_act:<op>` or `elevenlabs_act` for every op. Unset: speech
# only — transcription, music, sound and voice design are opened on purpose (transcription
# bills a length the caller declares, see `MAX_TRANSCRIBE_BYTES_PLATFORM`).
OPS: dict[str, frozenset[str]] = {"elevenlabs_act": frozenset(_ACT_OPS)}
PLATFORM_OPS_KEY = "platform.ops"
PLATFORM_OPS_DEFAULT = "elevenlabs_act:create_speech"

# Speech models billed half a credit per character (ElevenLabs' price list). Any other
# model — and the API's default when `model_id` is omitted, `eleven_multilingual_v2` —
# bills one credit per character.
HALF_RATE_MODELS = frozenset({"eleven_flash_v2", "eleven_flash_v2_5",
                              "eleven_turbo_v2", "eleven_turbo_v2_5"})

# The longest `text` one generation takes ON THE SHARED KEY, in characters: one call must
# never spend a day's quota at once. Checked as soon as the resolved key turns out to be
# the shared one — before the client is built, ElevenLabs called or a credit noted. The
# customer's own key is not bounded here (ElevenLabs bounds it). A longer script is cut
# into lines, read one call each (`previous_text` / `next_text` keep them continuous).
MAX_TEXT_CHARS = 5_000
TEXT_TOO_LONG = "text_too_long"

# Speech to text (Scribe) is billed per second of audio, not per character: ElevenLabs'
# credit-cost table puts it at 330 credits a minute of audio. ⚠️ Read off ElevenLabs'
# pricing page (28/10), not measured: check it against the subscription's credits after
# one real call before trusting it. The quota and `quantity` stay in ElevenLabs credits,
# the grant's one unit.
TRANSCRIBE_CREDITS_PER_MINUTE = 330
# The longest clip one transcription takes ON THE SHARED KEY, in seconds (a dictation is
# a sentence or two; the customer's own key is not bounded here), and in bytes.
MAX_TRANSCRIBE_SECONDS = 300
MAX_TRANSCRIBE_BYTES = 10 * 1024 * 1024
AUDIO_TOO_LONG = "audio_too_long"
# On the shared key the length is DECLARED (`audio_seconds`, required there), not
# measured: the quota is checked against it, and the call bills at least where the last
# word ends. What bounds an understated length is the file's size: 1 MB is a few minutes
# of compressed speech at worst, so one call can never cost more than that.
MAX_TRANSCRIBE_BYTES_PLATFORM = 1 * 1024 * 1024
AUDIO_SECONDS_REQUIRED = "audio_seconds_required"
AUDIO_TOO_LARGE = "audio_too_large"

# Music is billed per minute of track, not per character, and ElevenLabs sends no
# `x-character-count` for it: every music call on 2 to 4 Nov 2026 noted 1 credit (the
# estimate below falls back on the prompt's length, and a composition plan has none).
# ElevenLabs' pricing page (FAQ, read 4 Nov 2026): "Eleven Music 900 credits per minute".
# Measured the same day on a Creator plan: a 20 s track from a prompt took 275 credits off
# the subscription (825 a minute), so the published rate bills slightly above the cost.
# The length is the one ASKED for (`music_length_ms`, or the sum of the plan's sections),
# which is what the quota is checked against before the call; on the shared key a prompt
# without a length is refused (the model would choose it, so nothing bounds the bill).
MUSIC_CREDITS_PER_MINUTE = 900
MUSIC_LENGTH_REQUIRED = "music_length_required"


def parse_platform_ops(value: str) -> frozenset[tuple[str, Optional[str]]]:
    """`platform.ops` as `(tool, op)` pairs, `op=None` for every op of the tool.
    Raises `ValueError` naming what it does not know — the admin console refuses the
    value with it, rather than storing a list that would silently serve less."""
    out = set()
    for item in (x.strip() for x in value.split(",")):
        if not item:
            continue
        tool, _, op = item.partition(":")
        tool, op = tool.strip(), op.strip() or None
        if tool not in OPS:
            raise ValueError(f"{tool!r} is not an ElevenLabs tool the shared key can serve "
                             f"(one of: {', '.join(sorted(OPS))})")
        if op is not None and op not in OPS[tool]:
            raise ValueError(f"{tool} has no op {op!r} (one of: {', '.join(sorted(OPS[tool]))})")
        out.add((tool, op))
    return frozenset(out)


def platform_ops() -> frozenset[tuple[str, Optional[str]]]:
    """What the shared key generates on this instance, read at each call. Unset: the
    default. A stored value the tool no longer understands RAISES rather than serving
    the default in its place (same rule as `tools/forager.platform_ops`)."""
    from ..db import connector_settings
    value = connector_settings.get_connector_setting(
        "platform", "platform", _NAME, PLATFORM_OPS_KEY)
    if value is None:
        return parse_platform_ops(PLATFORM_OPS_DEFAULT)
    try:
        return parse_platform_ops(value)
    except ValueError as e:
        raise RuntimeError(f"elevenlabs: the instance setting `{PLATFORM_OPS_KEY}` "
                           f"({value!r}) is not understood: {e} — fix it with "
                           "oto_admin_connector_setting") from e


def _served(tool: str, op: Optional[str], ops) -> bool:
    return (tool, None) in ops or (tool, op) in ops


def rate(model_id: Optional[str]) -> float:
    """Credits per character of a speech model."""
    return 0.5 if model_id in HALF_RATE_MODELS else 1.0


def speech_credits(text: str, model_id: Optional[str],
                   character_count: Optional[int] = None) -> int:
    """The ElevenLabs credits one speech call spends: the characters ElevenLabs counted
    (`x-character-count`), else the text's length, times the model's rate, rounded UP —
    a whole number, which is all the journal's `quantity` keeps."""
    chars = character_count if isinstance(character_count, int) and character_count >= 0 \
        else len(text or "")
    return int(math.ceil(chars * rate(model_id)))


def transcribe_credits(seconds: Optional[float]) -> int:
    """The ElevenLabs credits a transcription of `seconds` of audio spends, rounded UP
    (at least 1)."""
    s = seconds if isinstance(seconds, (int, float)) and seconds > 0 else 0
    return max(1, int(math.ceil(s * TRANSCRIBE_CREDITS_PER_MINUTE / 60)))


def heard_seconds(result: Any) -> float:
    """Where the last word ends in a transcription (`words[].end`, seconds), 0 when
    nothing was heard — a floor on the clip's length, not its length."""
    words = result.get("words") if isinstance(result, dict) else None
    ends = [w.get("end") for w in words or [] if isinstance(w, dict)]
    return max([e for e in ends if isinstance(e, (int, float))], default=0.0)


def asked_music_ms(music_length_ms: Optional[int], composition_plan: Any) -> Optional[int]:
    """The track's asked length in milliseconds: `music_length_ms`, else the sum of the
    plan's `sections[].duration_ms`; None when neither says it."""
    if isinstance(music_length_ms, int) and not isinstance(music_length_ms, bool) \
            and music_length_ms > 0:
        return music_length_ms
    sections = composition_plan.get("sections") if isinstance(composition_plan, dict) else None
    total = sum(s.get("duration_ms") for s in sections or []
                if isinstance(s, dict) and isinstance(s.get("duration_ms"), (int, float))
                and not isinstance(s.get("duration_ms"), bool) and s.get("duration_ms") > 0)
    return int(total) if total else None


def music_credits(length_ms: Optional[int]) -> int:
    """The ElevenLabs credits a track of `length_ms` spends, rounded UP (at least 1)."""
    ms = length_ms if isinstance(length_ms, (int, float)) and length_ms > 0 else 0
    return max(1, int(math.ceil(ms * MUSIC_CREDITS_PER_MINUTE / 60_000)))


def generation_credits(op: str, audio: Any, fallback_text: Optional[str]) -> int:
    """Credits of a music, sound or voice-design call, served on the shared key only
    when the instance opened it. ElevenLabs' `x-character-count` when it sent one;
    else an ESTIMATE (the prompt's length, at least 1), logged — no price list here
    says what these cost per second. Check the header before opening these ops."""
    count = audio.get("character_count") if isinstance(audio, dict) else None
    if isinstance(count, int) and count >= 0:
        return count
    estimate = max(1, len(fallback_text or ""))
    logger.info("elevenlabs : %s sans x-character-count — %d crédits estimés", op,
                   estimate)
    return estimate


class ElevenLabsASec(RuntimeError):
    """The ElevenLabs account is out of credits. ElevenLabs says so with a 401
    (`detail.status = quota_exceeded`), which alone would read as a refused key: this
    carries `status_code = 402` ON PURPOSE, so the taxonomy marks the account empty,
    not the key dead (same device as `serper.SerperASec`)."""
    status_code = 402


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _borner_texte(text: Optional[str]) -> None:
    """On the shared key: refuses (`text_too_long`) a `text` longer than
    `MAX_TEXT_CHARS` — nothing done, nothing spent."""
    if text is not None and len(text) > MAX_TEXT_CHARS:
        raise _bad(f"{TEXT_TOO_LONG}: `text` is {len(text)} characters, at most "
                   f"{MAX_TEXT_CHARS} per call on the shared key. Cut it into lines and "
                   "read them one call each (`previous_text` / `next_text` keep them "
                   "continuous), or set your own ElevenLabs key, which lifts this limit. "
                   "Nothing was done.")


def _slim(value: Any, drop: frozenset, dropped: set) -> Any:
    """Copy of `value` without the keys in `drop`, at any depth."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in drop:
                dropped.add(k)
                continue
            out[k] = _slim(v, drop, dropped)
        return out
    if isinstance(value, list):
        return [_slim(v, drop, dropped) for v in value]
    return value


def _view(payload: Any, full: bool, drop: frozenset) -> Any:
    """Tightened view of a list (unless `full`); the answer NAMES what it removed."""
    if full:
        return payload
    dropped: set = set()
    out = _slim(payload, drop, dropped)
    if dropped and isinstance(out, dict):
        out["projection"] = {"omitted": sorted(dropped), "hint": _HINT}
    elif dropped and isinstance(out, list):
        out = {"items": out, "projection": {"omitted": sorted(dropped), "hint": _HINT}}
    return out


def _detail_status(e) -> str:
    """`detail.status` of an ElevenLabs error body (`quota_exceeded`,
    `invalid_api_key`, `missing_permissions`…), or ''."""
    body = getattr(e, "body", None)
    detail = body.get("detail") if isinstance(body, dict) else None
    return str(detail.get("status") or "") if isinstance(detail, dict) else ""


def _detail_message(e) -> str:
    body = getattr(e, "body", None)
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)[:400]
    return str(detail if detail is not None else body)[:400]


def _upstream_message(e) -> str:
    status, kind = e.status_code, _detail_status(e)
    if status == 401 and kind == "missing_permissions":
        return ("ElevenLabs: this key is restricted and lacks the permission this call "
                f"needs ({_detail_message(e)}). Widen the key's permissions in ElevenLabs, "
                "or use another key.")
    if status == 401:
        return ("ElevenLabs refused the API key (HTTP 401): missing, revoked or mistyped. "
                "Retrying unchanged fails the same way.")
    if status == 403:
        return ("ElevenLabs refused the operation (HTTP 403): the plan does not include "
                f"it ({_detail_message(e)}). Some voices, models and output formats are "
                "paid only.")
    if status == 404:
        return "ElevenLabs: not found (404). Check the voice id (`op='list_voices'`)."
    if status in (400, 422):
        return f"ElevenLabs rejected the request (HTTP {status}): {_detail_message(e)}"
    if status == 429:
        return ("ElevenLabs: limit reached (429), too many requests at once for the plan. "
                "Retry in a moment.")
    if status >= 500:
        return f"ElevenLabs is temporarily unavailable (HTTP {status}); retry later."
    return f"ElevenLabs refused the request (HTTP {status}): {_detail_message(e)}"


def _refuse_ignored(op: str, **provided) -> None:
    """An argument supplied that THIS op does not use is an intent error."""
    for name, value in provided.items():
        if value is not None and value is not False:
            raise _bad(f"op='{op}' does not use `{name}`. Nothing was done.")


def _need(value, name: str, op: str):
    if value is None or value == "":
        raise _bad(f"op='{op}' requires `{name}`.")
    return value


def words_of(alignment: Optional[dict]) -> list[dict]:
    """Characters with their times → words with theirs: `[{"word", "start", "end"}]`,
    seconds. A word is a run of non-space characters, punctuation kept on it;
    a performance tag in square brackets (`[warmly]`) is not a word."""
    if not isinstance(alignment, dict):
        return []
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    words: list[dict] = []
    cur: Optional[dict] = None
    in_tag = False
    for i, ch in enumerate(chars):
        if ch == "[":
            in_tag = True
        if in_tag:
            if ch == "]":
                in_tag = False
            continue
        if not ch.strip():
            if cur:
                words.append(cur)
            cur = None
            continue
        if i >= len(starts) or i >= len(ends):
            break
        if cur is None:
            cur = {"word": "", "start": starts[i], "end": ends[i]}
        cur["word"] += ch
        cur["end"] = ends[i]
    if cur:
        words.append(cur)
    return [{"word": w["word"], "start": round(w["start"], 3), "end": round(w["end"], 3)}
            for w in words]


def _deposit(audio: dict, stem: str, sub: str) -> dict:
    """Audio bytes → private temporary storage, served as a signed URL."""
    from .. import media_store
    filename = f"{stem}.{audio['extension']}"
    try:
        url = media_store.upload_private(_PREFIX, sub, audio["content"],
                                         audio["content_type"], filename)
    except media_store.MediaError as e:
        raise _bad(f"ElevenLabs: the audio was generated but temporary storage is "
                   f"unavailable ({e}); nothing can be served.") from None
    out = {"filename": filename, "mimeType": audio["content_type"],
           "size": len(audio["content"]), "output_format": audio.get("output_format"),
           "encoding": "url", "url": url, "expires_in": media_store.presign_expiry()}
    for key in ("request_id", "character_count"):
        if audio.get(key) is not None:
            out[key] = audio[key]
    return out


def _verify(fields: dict, config: dict | None = None) -> Optional[dict]:  # noqa: ARG001
    """Probe for "test the connection": the subscription, the cheapest authenticated
    call, which also says whether credits are left this period.

    Returns the balance it read (`{"quota": …}`, credits left of the period's limit):
    it used to be read then THROWN AWAY, so `oto_admin_platform_balances` showed the
    platform key as `auth+quota` with `balance: null` — "not measured" — every day."""
    from oto.tools.elevenlabs import ElevenLabsClient
    from oto.tools.common.errors import UpstreamHTTPError

    try:
        sub = ElevenLabsClient(api_key=fields.get("key")).probe()
    except UpstreamHTTPError as e:
        if e.status_code == 401 and _detail_status(e) == "quota_exceeded":
            raise connector_verify.QuotaEpuise(f"ElevenLabs: {_detail_message(e)}")
        if e.status_code in (401, 403):
            raise connector_verify.NonAutorise(f"ElevenLabs HTTP {e.status_code}: {e.body}")
        raise RuntimeError(f"ElevenLabs HTTP {e.status_code}: {e.body}")
    except ValueError as e:
        raise connector_verify.NonAutorise(str(e))
    used, limit = sub.get("character_count"), sub.get("character_limit")
    if isinstance(used, int) and isinstance(limit, int) and limit > 0 and used >= limit:
        raise connector_verify.QuotaEpuise(
            f"ElevenLabs: {used} of {limit} credits used this period.")
    if isinstance(used, int) and isinstance(limit, int) and limit > 0:
        return {"quota": {"restant": limit - used, "unite": "credits", "limite": limit}}
    return None


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.elevenlabs import ElevenLabsClient

    connector_verify.register(_NAME, _verify, couvre=connector_verify.AUTH_QUOTA)

    def _client(tool: str, op: str, units: int = 1, text: Optional[str] = None,
                music_length: Optional[int] = None
                ) -> tuple[ElevenLabsClient, Callable[[int], None]]:
        """`(client, served)`. The cascade runs whole (own key > platform grant), the
        grant's quota checked for `units`; on the platform key, a `text` over
        `MAX_TEXT_CHARS`, a generation this instance does not serve (`platform_ops`), or
        the balance read, is refused BEFORE a credit is spent, naming what the shared
        key serves.

        `served(credits)` notes what the call spent, once ElevenLabs has answered: a
        call that raises notes nothing. `quantity` on every generation (the journal's
        billed quantity, any key), `units` on the platform key (the quota axis; 0 for
        a read)."""
        key, is_platform = access.resolve_api_key(_NAME, units=units)
        if is_platform:
            _borner_texte(text)
            if tool == _NAME:
                refused = op == "get_subscription"
            else:
                ops = platform_ops()
                refused = tool not in OPS or not _served(tool, op, ops)
            if not refused and music_length == 0:
                raise _bad(f"ElevenLabs ({MUSIC_LENGTH_REQUIRED}): on the shared key a track "
                           "needs its length, `music_length_ms` (3,000 to 300,000) or a "
                           "`composition_plan` whose sections carry `duration_ms`: music is "
                           f"billed per minute ({MUSIC_CREDITS_PER_MINUTE} ElevenLabs credits). "
                           "Nothing was spent.")
            if refused:
                from ..auth.hooks import current_user_sub_from_token
                from .. import links
                sub = current_user_sub_from_token()
                what = ("the balance read" if tool == _NAME else ", ".join(sorted(
                    t if o is None else f"{t} op={o!r}" for t, o in ops)) or "nothing")
                covers = ("the voices and models, never the balance" if tool == _NAME
                          else what)
                raise _bad(
                    f"{tool} op={op!r} needs your own ElevenLabs key: the shared key "
                    f"here covers {covers}. Set your own key"
                    f"{links.ou_poser_la_cle(sub, org=access.current_org(sub), connecteur=_NAME)}.")
        client = ElevenLabsClient(api_key=key)
        generation = tool == "elevenlabs_act"

        def served(credits: int) -> None:
            if is_platform:
                session_org.note_call_trace(
                    units=credits, **({"quantity": credits} if generation else {}))
            elif generation:
                session_org.note_call_trace(quantity=credits)
        return client, served

    def _run(fn):
        restricted = None
        try:
            return fn()
        except UpstreamHTTPError as e:
            if e.status_code == 401 and _detail_status(e) == "quota_exceeded":
                raise _bad("ElevenLabs: the account has no credits left for this period "
                           "(quota_exceeded). The call was correct: top up or wait for the "
                           "reset (`op='get_subscription'` of `elevenlabs` says when); do not "
                           "retry it.") from ElevenLabsASec(str(e))
            if e.status_code == 401 and _detail_status(e) == "missing_permissions":
                restricted = _upstream_message(e)
            else:
                raise _bad(_upstream_message(e))
        except ValueError as e:
            raise _bad(str(e))
        # A key restricted on purpose is not a dead key. Raised OUTSIDE the `except`:
        # `from None` would still leave the 401 in `__context__`, which the taxonomy
        # walks (`error_taxonomy`), and the key would be painted red.
        raise _bad(restricted)

    def _transcribe(source: Any, language_code: Optional[str],
                    audio_seconds: Optional[float], model_id: Optional[str]) -> dict:
        """Speech to text. The file is read server-side (`file_source`) BEFORE the key
        is resolved: a source that cannot be read spends nothing. On the shared key,
        before ElevenLabs is called: an op the instance does not serve, a missing
        `audio_seconds`, a clip over `MAX_TRANSCRIBE_SECONDS` or a file over
        `MAX_TRANSCRIBE_BYTES_PLATFORM` is refused; the quota is checked against
        `audio_seconds`, and the call bills the longer of `audio_seconds` and where the
        last word ends (an understated length cannot bill less than what was heard)."""
        from .. import file_source
        if audio_seconds is not None and (isinstance(audio_seconds, bool) or not isinstance(
                audio_seconds, (int, float)) or audio_seconds <= 0):
            raise _bad("`audio_seconds` must be a positive number of seconds.")
        try:
            rf = file_source.resolve(source, max_bytes=MAX_TRANSCRIBE_BYTES)
        except file_source.FileSourceError as e:
            raise _bad(f"ElevenLabs: the audio could not be read ({e}). Nothing was done.")
        key, is_platform = access.resolve_api_key(_NAME, units=transcribe_credits(audio_seconds))
        if is_platform:
            if not _served("elevenlabs_act", "create_transcript", platform_ops()):
                raise _bad("elevenlabs_act op='create_transcript' needs your own "
                           "ElevenLabs key: the shared key here does not transcribe.")
            if audio_seconds is None:
                raise _bad(f"{AUDIO_SECONDS_REQUIRED}: on the shared key, declare the "
                           "clip's length in seconds (`audio_seconds`): the quota is "
                           "checked against it. Your own ElevenLabs key lifts this rule. "
                           "Nothing was done.")
            if audio_seconds > MAX_TRANSCRIBE_SECONDS:
                raise _bad(f"{AUDIO_TOO_LONG}: {audio_seconds:.0f} s of audio, at most "
                           f"{MAX_TRANSCRIBE_SECONDS} s per call on the shared key. Cut "
                           "the clip, or set your own ElevenLabs key. Nothing was done.")
            if len(rf.data) > MAX_TRANSCRIBE_BYTES_PLATFORM:
                raise _bad(f"{AUDIO_TOO_LARGE}: the audio is {len(rf.data)} bytes, at most "
                           f"{MAX_TRANSCRIBE_BYTES_PLATFORM} per call on the shared key "
                           f"({MAX_TRANSCRIBE_BYTES_PLATFORM // (1024 * 1024)} MB). Cut or "
                           "compress the clip, or set your own ElevenLabs key. Nothing was "
                           "done.")
        client = ElevenLabsClient(api_key=key)
        result = _run(lambda: client.transcribe_speech(
            rf.data, filename=rf.filename, content_type=rf.mime,
            model_id=model_id or "scribe_v2", language_code=language_code))
        credits = transcribe_credits(max(audio_seconds or 0, heard_seconds(result)))
        session_org.note_call_trace(quantity=credits,
                                    **({"units": credits} if is_platform else {}))
        return {"text": result.get("text") or "", "language_code": result.get("language_code"),
                "language_probability": result.get("language_probability"),
                "words": [{"word": w.get("text"), "start": w.get("start"), "end": w.get("end")}
                          for w in result.get("words") or []
                          if isinstance(w, dict) and w.get("type", "word") == "word"],
                "credits": credits}

    @mcp.tool(name="elevenlabs", annotations=LECTURE)
    def elevenlabs(
        op: Literal["get_subscription", "list_voices", "get_voice", "list_models"],
        voice_id: Optional[str] = None,
        search: Optional[str] = None,
        voice_type: Optional[Literal["personal", "community", "default", "workspace",
                                     "non-default"]] = None,
        category: Optional[Literal["premade", "cloned", "generated", "professional"]] = None,
        page_size: Optional[int] = None,
        next_page_token: Optional[str] = None,
        full: bool = False,
    ) -> Any:
        """ElevenLabs account, read-only: credits, voices and models. Nothing here
        spends credits; the generations are `elevenlabs_act`.

        `op`:
        - `get_subscription` — plan, credits used this period (`character_count`)
          against the allowance (`character_limit`), reset time
          (`next_character_count_reset_unix`), voice slots. Read it before a batch.
        - `list_voices` — the voices the account can use (its own and the default
          ones): id, name, category, labels (accent, age, gender, use case), preview
          URL. Narrow with `search`, `voice_type`, `category`; page with
          `next_page_token` while `has_more` is true.
        - `get_voice` — one voice (`voice_id`): labels, description, default settings.
        - `list_models` — the models and what each can do (languages, maximum
          characters per request). A model id is what `create_speech` takes.

        The two lists are tightened by default (a voice's samples, fine-tuning,
        sharing and verification records; a model's language and rate matrices),
        and the answer's `projection` names what was removed: `full=true` returns
        the raw records.

        On the SHARED key (granted by oto, no key of your own) the reads are free, and
        `get_subscription` is refused: that balance is the instance's, not yours.

        A restricted key may lack the permission a read needs (`user_read`,
        `voices_read`, `models_read`) even when it can generate.

        Args:
            op: What to read (see above).
            voice_id: The voice to read, for `get_voice`.
            search: Words matched against a voice's name, description, labels and category (`list_voices`).
            voice_type: `personal` (the account's own), `community` (saved from the library), `default`, `workspace`, or `non-default` (`list_voices`).
            category: `premade`, `cloned`, `generated` or `professional` (`list_voices`).
            page_size: Voices per page, 1 to 100, default 10 (`list_voices`).
            next_page_token: From the previous page of `list_voices`.
            full: `list_voices` / `list_models`: the raw records instead of the tightened view.
        """
        if op not in _READ_OPS:
            raise _bad(f"Unknown op {op!r}: expected one of {', '.join(_READ_OPS)}.")
        if op != "list_voices":
            _refuse_ignored(op, search=search, voice_type=voice_type, category=category,
                            page_size=page_size, next_page_token=next_page_token)
        if op != "get_voice":
            _refuse_ignored(op, voice_id=voice_id)
        if op not in ("list_voices", "list_models"):
            _refuse_ignored(op, full=full)
        vid = _need(voice_id, "voice_id", op) if op == "get_voice" else None
        client, served = _client(_NAME, op)
        if op == "get_subscription":
            out = _run(lambda: client.get_subscription())
        elif op == "list_models":
            out = _view(_run(lambda: client.list_models()), full, _DROP_MODELS)
        elif op == "get_voice":
            out = _run(lambda: client.get_voice(vid))
        else:
            out = _view(_run(lambda: client.list_voices(
                search=search, voice_type=voice_type, category=category,
                page_size=page_size, next_page_token=next_page_token)), full, _DROP_VOICES)
        served(0)
        return out

    @mcp.tool(name="elevenlabs_act", annotations=ACTE)
    def elevenlabs_act(
        op: Literal["create_speech", "create_music", "create_sound", "create_voice_design",
                    "create_transcript"],
        text: Optional[str] = None,
        voice_id: Optional[str] = None,
        model_id: Optional[str] = None,
        voice_settings: Optional[dict] = None,
        previous_text: Optional[str] = None,
        next_text: Optional[str] = None,
        language_code: Optional[str] = None,
        timestamps: bool = False,
        prompt: Optional[str] = None,
        composition_plan: Optional[dict] = None,
        music_length_ms: Optional[int] = None,
        force_instrumental: Optional[bool] = None,
        respect_sections_durations: Optional[bool] = None,
        duration_seconds: Optional[float] = None,
        prompt_influence: Optional[float] = None,
        loop: Optional[bool] = None,
        source: Optional[dict] = None,
        audio_seconds: Optional[float] = None,
        voice_description: Optional[str] = None,
        auto_generate_text: Optional[bool] = None,
        seed: Optional[int] = None,
        output_format: Optional[str] = None,
    ) -> dict:
        """Generate audio with ElevenLabs. EVERY call spends the account's credits
        (characters for speech, seconds for music and sound) and is not retried:
        read `elevenlabs` op='get_subscription' before a batch. On the shared key, `text`
        is at most 5,000 characters per call (`text_too_long`): cut a longer script
        into lines, or use your own key, which lifts the limit.

        On the SHARED key (granted by oto, no key of your own), only the generations
        this instance serves run — by default `create_speech`; the others ask for your
        own ElevenLabs key.

        The audio comes back as a signed URL (`url`, valid `expires_in` seconds,
        deleted after a day), never inline: download it right away.

        `op`:
        - `create_speech` — `text` read by `voice_id` (from `elevenlabs`
          op='list_voices'), with `model_id`, `voice_settings`, `seed` and
          `language_code` optional. `previous_text` / `next_text` make a line sound
          continuous with its neighbours when a script is read line by line. With
          `timestamps=true` the answer also carries `words` (each word's start and
          end, seconds) and the raw per-character `alignment`.
        - `create_music` — a track from a `prompt` (length `music_length_ms`, 3,000
          to 300,000) or from a `composition_plan` (sections, each with its
          duration and positive and negative styles), never both;
          `respect_sections_durations`, `force_instrumental`, `seed` optional. A
          prompt naming an artist or a copyrighted work is refused. Billed per
          minute of track (~900 credits a minute). On the shared key, when the
          instance serves it: the length is required (`music_length_ms`, or each
          section's `duration_ms`), the quota is checked against it.
        - `create_sound` — a sound effect described in `text`;
          `duration_seconds` (0.5 to 30, else the model decides),
          `prompt_influence` (0 to 1), `loop` optional.
        - `create_transcript` — the words said in an audio file (Scribe): `source`
          designates the file (a `{"kind": "transit", "ref"}` an oto tool returned, a
          project file, a URL…), `language_code` pins the language (else detected),
          `audio_seconds` its length. Returns `text`, `language_code` and `words`
          (each with its start and end, seconds). Billed per second of audio
          (~330 credits a minute). On the shared key, when the instance serves it:
          `audio_seconds` is required, at most 300 seconds and 1 MB of audio per call.
        - `create_voice_design` — up to three previews of a voice described in
          `voice_description`, each reading `text` (or a text the model writes with
          `auto_generate_text=true`); each preview has its `generated_voice_id` and
          its own URL. Saving a preview as a voice of the account is not done here.

        `output_format` is `<codec>_<sample rate>[_<bitrate>]`, default
        `mp3_44100_128`; some (`mp3_44100_192`, `pcm_44100`) are paid plans only.

        Args:
            op: What to generate (see above).
            text: The words to read (`create_speech`), the sound to make (`create_sound`), or the preview's line (`create_voice_design`); at most 5000 characters on the shared key.
            voice_id: The voice that reads, for `create_speech`.
            model_id: Model id, e.g. eleven_multilingual_v2 or eleven_v3 for speech, music_v1 for music; the API's default when omitted.
            voice_settings: Speech only: {stability, similarity_boost, style, use_speaker_boost, speed}, each optional.
            previous_text: Speech only: the line before this one, for continuity.
            next_text: Speech only: the line after this one, for continuity.
            language_code: Speech and transcription: ISO 639-1 code to force the language, when the model supports it.
            timestamps: Speech only: also return each word's start and end.
            prompt: Music only: the track described in words.
            composition_plan: Music only: {positive_global_styles, negative_global_styles, sections: [{section_name, duration_ms, positive_local_styles, negative_local_styles, lines}]}.
            music_length_ms: Music from a prompt only: the length, 3000 to 300000.
            force_instrumental: Music only: no vocals.
            respect_sections_durations: Music from a plan only: keep each section's duration exactly.
            duration_seconds: Sound only: 0.5 to 30 seconds.
            prompt_influence: Sound only: how literally to follow the text, 0 to 1.
            loop: Sound only: make it loop seamlessly.
            source: Transcription only: the audio file, e.g. {"kind": "transit", "ref": "..."} or {"kind": "url", "url": "https://..."}.
            audio_seconds: Transcription only: the clip's length in seconds — what the quota is checked against before the call; required on the shared key.
            voice_description: Voice design only: the voice in words, e.g. age, accent, tone, pace.
            auto_generate_text: Voice design only: let the model write the preview's line.
            seed: 0 to 4294967295, to make a take reproducible as far as the model allows.
            output_format: e.g. mp3_44100_128 (default), mp3_48000_192, pcm_24000, opus_48000_64.
        """
        if op not in _ACT_OPS:
            raise _bad(f"Unknown op {op!r}: expected one of {', '.join(_ACT_OPS)}. "
                       "Nothing was done.")
        speech = dict(voice_id=voice_id, voice_settings=voice_settings,
                      previous_text=previous_text, next_text=next_text,
                      language_code=language_code, timestamps=timestamps)
        music = dict(prompt=prompt, composition_plan=composition_plan,
                     music_length_ms=music_length_ms, force_instrumental=force_instrumental,
                     respect_sections_durations=respect_sections_durations)
        sound = dict(duration_seconds=duration_seconds, prompt_influence=prompt_influence,
                     loop=loop)
        design = dict(voice_description=voice_description, auto_generate_text=auto_generate_text)
        heard = dict(source=source, audio_seconds=audio_seconds)
        sub = access.current_user_sub_or_raise()

        if op == "create_transcript":
            _refuse_ignored(op, text=text, seed=seed, output_format=output_format,
                            voice_id=voice_id, voice_settings=voice_settings,
                            previous_text=previous_text, next_text=next_text,
                            timestamps=timestamps, **music, **sound, **design)
            return _transcribe(_need(source, "source", op), language_code, audio_seconds,
                               model_id)

        if op == "create_speech":
            _refuse_ignored(op, **music, **sound, **design, **heard)
            vid, line = _need(voice_id, "voice_id", op), _need(text, "text", op)
            client, served = _client("elevenlabs_act", op,
                                     units=max(1, speech_credits(line, model_id)), text=line)
            audio = _run(lambda: client.create_speech(
                vid, line, model_id=model_id, voice_settings=voice_settings,
                previous_text=previous_text, next_text=next_text, seed=seed,
                language_code=language_code, output_format=output_format,
                timestamps=timestamps))
            served(speech_credits(line, model_id, audio.get("character_count")))
            out = _deposit(audio, "speech", sub)
            if timestamps:
                out["words"] = words_of(audio.get("alignment"))
                out["alignment"] = audio.get("alignment")
            return out

        if op == "create_music":
            _refuse_ignored(op, text=text, **speech, **sound, **design, **heard)
            length = asked_music_ms(music_length_ms, composition_plan)
            client, served = _client("elevenlabs_act", op, units=music_credits(length),
                                     music_length=length if length else 0)
            audio = _run(lambda: client.create_music(
                prompt=prompt, composition_plan=composition_plan,
                music_length_ms=music_length_ms, model_id=model_id, seed=seed,
                force_instrumental=force_instrumental,
                respect_sections_durations=respect_sections_durations,
                output_format=output_format))
            # The header when ElevenLabs sends one, never less than the length asked for.
            served(max(music_credits(length), generation_credits(op, audio, prompt)))
            return _deposit(audio, "music", sub)

        if op == "create_sound":
            _refuse_ignored(op, seed=seed, **speech, **music, **design, **heard)
            what = _need(text, "text", op)
            client, served = _client("elevenlabs_act", op, text=what)
            audio = _run(lambda: client.create_sound(
                what, duration_seconds=duration_seconds, prompt_influence=prompt_influence,
                loop=loop, model_id=model_id, output_format=output_format))
            served(generation_credits(op, audio, what))
            return _deposit(audio, "sound", sub)

        # op == "create_voice_design" — the only one left (guard above).
        _refuse_ignored(op, **speech, **music, **sound, **heard)
        described = _need(voice_description, "voice_description", op)
        client, served = _client("elevenlabs_act", op, text=text)
        result = _run(lambda: client.create_voice_design(
            described, text=text, auto_generate_text=auto_generate_text,
            model_id=model_id, seed=seed, output_format=output_format))
        served(generation_credits(op, result, text or described))
        previews = []
        for i, preview in enumerate(result.get("previews") or [], start=1):
            served = _deposit(preview, f"voice-preview-{i}", sub)
            served.update(generated_voice_id=preview.get("generated_voice_id"),
                          duration_secs=preview.get("duration_secs"),
                          language=preview.get("language"))
            previews.append(served)
        return {"text": result.get("text"), "previews": previews}
