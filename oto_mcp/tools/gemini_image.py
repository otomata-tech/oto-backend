"""Gemini images ("Nano Banana") — image generation and editing by Google's Gemini image
models, each call priced before it runs and billed at what Google counted.

Wraps `oto.tools.gemini.GeminiClient.create_image` (`x-goog-api-key` header), key resolved
per call via `access.resolve_api_key("gemini_image")`: the customer's own (user or org),
or the platform key when the org is granted it.

**Surface** (the `<ns>` / `<ns>_act` pair):
- `gemini_image` (read-only, free) — the image models the key can call (`list_models`),
  and the price bound of a call (`estimate`, from the published price table, no call to
  Google);
- `gemini_image_act` — `create_image`: one image from a prompt, optionally following or
  editing up to fourteen input images.

**The price.** Google bills tokens, at a rate per modality: input (prompt, input images),
image output, and the model's thinking (text output rate; it cannot be turned off). Its
answer never carries a price, only the tokens it counted: the rates are Google's published
ones (`PRICES`). The output image's token count is fixed per size, so a call has a BOUND
known before it is sent: the image's tokens, a thinking allowance, the prompt and the
input images. The answer's `usageMetadata` gives what Google counted BY MODALITY
(`promptTokensDetails`, `candidatesTokensDetails`, each a list of `{modality,
tokenCount}`), plus `thoughtsTokenCount`: the call bills THAT, each modality at its rate
(`cost_of`). An answer without that breakdown is never billed at a guess
(`UsageUnreadable`): on the shared key the call is refused, on the customer's own key the
cost is said unknown. `max_usd` (optional) refuses a call whose bound is above it, before
Google is called.

**The shared key** (closed, on grant, with a daily quota in micro-dollars of cost). It
serves only the models priced in `PRICES` — any other model is refused before Google is
called. The grant's quota is checked for the call's BOUND (`units`), and the call notes
its actual cost as `units` and `quantity` (micro-dollars, rounded up). A call Google
refuses with an HTTP error notes nothing (Google bills nothing). On the customer's own key
the cost is noted as `quantity` too, as `elevenlabs` does (the billing filters on
`key_mode`); an unpriced model notes 0.

Images never travel inline: each is deposited in private temporary storage and served as
a signed URL (`media_store.upload_private`), valid `media_store.presign_expiry()` seconds
and deleted after a day.

Calls to the client are written out in full (`client = _client(…)` then
`client.create_image(…)`): this is what makes them verifiable by the version-skew probe
(`test_tools_client_methods_exist`).
"""
from __future__ import annotations

import logging
import math
from typing import Any, Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, session_org
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError
from .lecture import ACTE, LECTURE

logger = logging.getLogger(__name__)

_NAME = "gemini_image"
_PREFIX = "gemini-image"

_READ_OPS = ("list_models", "estimate")
_ACT_OPS = ("create_image",)

DEFAULT_MODEL = "gemini-nano-banana-2.1"
SIZES = ("1K", "2K", "4K")
ASPECT_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9",
                 "1:4", "4:1", "1:8", "8:1")

# What each priced model costs, in US dollars per million tokens BY MODALITY (the
# `modality` of Google's `ModalityTokenCount`), and how many tokens its output image holds
# by size. Rates read off Google's pricing page (paid tier, standard), checked again on
# 13/11/2026. Image tokens MEASURED on two real calls (`candidatesTokensDetails` IMAGE):
# 1,120 at 1K, 3,780 at 4K — the pricing page's figures; 2K's 1,680 is the page's.
# The thinking, and any output token the per-modality list leaves out, are billed at the
# TEXT output rate (Google: "text and thinking").
# Only these models are served on the shared key: an unpriced model has no bound.
PRICES: dict[str, dict] = {
    "gemini-nano-banana-2.1": {
        "input_per_m": {"TEXT": 1.50, "IMAGE": 1.50},
        "output_per_m": {"IMAGE": 30.0, "TEXT": 7.50},
        "image_tokens": {"1K": 1120, "2K": 1680, "4K": 3780},
    },
}
# The bound's allowance for the output that is NOT the image, in tokens, billed at the
# text output rate: the thinking (`thoughtsTokenCount`, it cannot be turned off) and the
# part of `candidatesTokenCount` the per-modality list leaves out. Measured 09/11/2026 on
# a one-line prompt at the default thinking level: 433 + 183 at 1K, 847 + 236 at 4K. The
# allowance keeps more than twice the 4K figure; a longer prompt may think longer.
THINKING_ALLOWANCE_TOKENS = 2_500
# The bound's count for one INPUT image, in tokens: Google counts an input image as a
# fixed number of tokens — 1,120 measured 09/11/2026 (`promptTokensDetails` IMAGE, a
# 1024×1024 JPEG reference).
INPUT_IMAGE_TOKENS = 1_120
# A prompt holds fewer tokens than characters; one per character is a bound.
MAX_INPUT_IMAGES = 14
MAX_INPUT_IMAGE_BYTES = 7 * 1024 * 1024
MAX_PROMPT_CHARS = 20_000

UNPRICED_MODEL = "model_not_on_shared_key"
ABOVE_MAX_USD = "above_max_usd"
USAGE_UNREADABLE = "gemini_usage_unreadable"


class UsageUnreadable(ValueError):
    """Google's `usageMetadata` does not say, modality by modality, what it counted: the
    call cannot be billed at what it cost."""


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def microdollars(usd: float) -> int:
    """A dollar amount in micro-dollars, rounded UP (the `round` before `ceil` keeps float
    noise from billing one micro-dollar too many — `tools/higgsfield.py::microdollars`)."""
    return math.ceil(round(float(usd) * 1e6, 6))


def bound_usd(model: str, image_size: Optional[str], prompt: str, n_images: int) -> Optional[float]:
    """The most one call may cost, in US dollars, or None for a model without a price."""
    p = PRICES.get(model)
    if p is None:
        return None
    size = image_size or "1K"
    out = (p["image_tokens"][size] * p["output_per_m"]["IMAGE"]
           + THINKING_ALLOWANCE_TOKENS * p["output_per_m"]["TEXT"])
    inp = (len(prompt or "") * p["input_per_m"]["TEXT"]
           + n_images * INPUT_IMAGE_TOKENS * p["input_per_m"]["IMAGE"])
    return round((out + inp) / 1e6, 6)


def _count(usage: dict, field: str) -> int:
    """A token count of `usageMetadata`: absent = 0 (Google leaves out an empty count),
    anything but a whole number ≥ 0 is unreadable."""
    v = usage.get(field, 0)
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise UsageUnreadable(f"`{field}` is {v!r}, not a token count")
    return v


def _by_modality(usage: dict, field: str, total: int, rates: dict[str, float]) -> dict[str, int]:
    """`{modality: tokens}` of a `*TokensDetails` list, the tokens it leaves out of
    `total` under `""`. Unreadable: a missing list when `total` is not 0 and the rates
    differ by modality, a modality without a rate, a list counting more than `total`."""
    details = usage.get(field)
    if details is None:
        if total and len(set(rates.values())) > 1:
            raise UsageUnreadable(f"`{field}` is missing: the {total} tokens cannot be "
                                  "priced by modality")
        return {"": total}
    if not isinstance(details, list):
        raise UsageUnreadable(f"`{field}` is not a list")
    out: dict[str, int] = {}
    for d in details:
        m = d.get("modality") if isinstance(d, dict) else None
        n = d.get("tokenCount", 0) if isinstance(d, dict) else None
        if m not in rates:
            raise UsageUnreadable(f"`{field}` counts the modality {m!r}, which has no rate")
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise UsageUnreadable(f"`{field}` gives {n!r} tokens for {m}")
        out[m] = out.get(m, 0) + n
    listed = sum(out.values())
    if listed > total:
        raise UsageUnreadable(f"`{field}` lists {listed} tokens, more than the {total} "
                              "counted")
    if total > listed:
        out[""] = total - listed
    return out


def cost_of(model: str, usage: Any) -> dict:
    """What a call cost, from Google's `usageMetadata`, each modality at its rate.

    Returns `{"usd": float | None, "basis": str}`. Input: each modality of
    `promptTokensDetails` at its input rate. Output: each modality of
    `candidatesTokensDetails` at its output rate (IMAGE at the image rate); the thinking
    (`thoughtsTokenCount`) and the output tokens the list leaves out (183 of 1,303 on a
    measured 1K call) at the TEXT output rate — Google's "text and thinking" rate. Tokens
    left out of the INPUT list take the TEXT input rate. An unpriced model has no cost
    (`usd` None).

    Raises `UsageUnreadable` when the answer does not say what it counted by modality:
    the call is never billed at a guess (the bound would overbill a 1K image by about
    40 %)."""
    p = PRICES.get(model)
    if p is None:
        return {"usd": None, "basis": "no price for this model"}
    if not isinstance(usage, dict) or "promptTokenCount" not in usage:
        raise UsageUnreadable("the answer carries no `usageMetadata`")
    inp, outp = p["input_per_m"], p["output_per_m"]
    prompt = _by_modality(usage, "promptTokensDetails", _count(usage, "promptTokenCount"), inp)
    output = _by_modality(usage, "candidatesTokensDetails",
                          _count(usage, "candidatesTokenCount"), outp)
    thoughts = _count(usage, "thoughtsTokenCount")
    usd = (sum(n * inp[m or "TEXT"] for m, n in prompt.items())
           + sum(n * outp[m or "TEXT"] for m, n in output.items())
           + thoughts * outp["TEXT"]) / 1e6
    image_t = output.get("IMAGE", 0)
    text_t = sum(n for m, n in output.items() if m != "IMAGE") + thoughts
    return {"usd": round(usd, 6),
            "basis": (f"{sum(prompt.values())} input, {image_t} image and {text_t} text and "
                      "thinking tokens, as Google counted them by modality, at Google's "
                      "published rates")}


def _error_body(e) -> dict:
    body = getattr(e, "body", None)
    err = body.get("error") if isinstance(body, dict) else None
    return err if isinstance(err, dict) else {}


def _upstream_message(e) -> str:
    status, err = e.status_code, _error_body(e)
    message = str(err.get("message") or getattr(e, "body", "") or "")[:500]
    if status == 429 and "free_tier" in message:
        return ("Gemini refused the call: this key's Google project is on the FREE tier, "
                "which has no image quota (`limit: 0`). Enable billing on the project in "
                "AI Studio, then retry. Nothing was spent.")
    if status == 429:
        return (f"Gemini: quota or rate limit reached (429): {message} Retry later, or "
                "raise the project's limits in AI Studio. Nothing was spent.")
    if status in (401, 403) or (status == 400 and "API key" in message):
        return (f"Gemini refused the API key (HTTP {status}): missing, revoked, mistyped, "
                "or without access to this model. Retrying unchanged fails the same way.")
    if status == 404:
        return (f"Gemini: no such model (404). `gemini_image op='list_models'` lists the "
                f"ones this key can call. {message}")
    if status == 400:
        return f"Gemini rejected the request (HTTP 400): {message} Nothing was spent."
    if status >= 500:
        return f"Gemini is temporarily unavailable (HTTP {status}); retry later."
    return f"Gemini refused the request (HTTP {status}): {message}"


def _refuse_ignored(op: str, **provided) -> None:
    for name, value in provided.items():
        if value is not None and value is not False and value != []:
            raise _bad(f"op='{op}' does not use `{name}`. Nothing was done.")


def _check_shape(model: str, image_size: Optional[str], aspect_ratio: Optional[str],
                 prompt: Optional[str], images: Optional[list]) -> None:
    if image_size is not None and image_size not in SIZES:
        raise _bad(f"`image_size` is one of {', '.join(SIZES)} (uppercase K), not "
                   f"{image_size!r}. Nothing was done.")
    if aspect_ratio is not None and aspect_ratio not in ASPECT_RATIOS:
        raise _bad(f"`aspect_ratio` is one of {', '.join(ASPECT_RATIOS)}, not "
                   f"{aspect_ratio!r}. Nothing was done.")
    if prompt is not None and len(prompt) > MAX_PROMPT_CHARS:
        raise _bad(f"`prompt` is {len(prompt)} characters, at most {MAX_PROMPT_CHARS}. "
                   "Nothing was done.")
    if images is not None and (not isinstance(images, list) or len(images) > MAX_INPUT_IMAGES):
        raise _bad(f"`images` is a list of at most {MAX_INPUT_IMAGES} file references. "
                   "Nothing was done.")


def _deposit(image: dict, stem: str, index: int, sub: str) -> dict:
    """Image bytes → private temporary storage, served as a signed URL."""
    from .. import media_store
    mime = image["mime_type"]
    ext = "png" if "png" in mime else "webp" if "webp" in mime else "jpg"
    filename = f"{stem}-{index + 1}.{ext}"
    try:
        url = media_store.upload_private(_PREFIX, sub, image["data"], mime, filename)
    except media_store.MediaError as e:
        raise _bad(f"Gemini: the image was generated but temporary storage is unavailable "
                   f"({e}); nothing can be served.") from None
    return {"filename": filename, "mimeType": mime, "size": len(image["data"]),
            "encoding": "url", "url": url, "expires_in": media_store.presign_expiry()}


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """Probe for "test the connection": the model list, free and authenticated. It says
    the key works, not that the project is billed: a free-tier key passes, then is
    refused at the first image."""
    from oto.tools.gemini import GeminiClient
    from oto.tools.common.errors import UpstreamHTTPError

    try:
        GeminiClient(api_key=fields.get("key")).list_models(page_size=1)
    except UpstreamHTTPError as e:
        if e.status_code in (400, 401, 403):
            raise connector_verify.NonAutorise(f"Gemini HTTP {e.status_code}: {e.body}")
        raise RuntimeError(f"Gemini HTTP {e.status_code}: {e.body}")
    except ValueError as e:
        raise connector_verify.NonAutorise(str(e))


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.gemini import GeminiClient

    connector_verify.register(_NAME, _verify)

    def _client(model: str, units: int = 1) -> tuple[GeminiClient, bool]:
        """`(client, is_platform)`. The cascade runs whole (own key > platform grant); on
        the platform key the grant's quota is checked for `units` micro-dollars, and a
        model without a price is refused before Google is called."""
        key, is_platform = access.resolve_api_key(_NAME, units=units)
        if is_platform and model not in PRICES:
            raise _bad(f"{UNPRICED_MODEL}: the shared key serves {', '.join(sorted(PRICES))}"
                       f", not {model!r}. Use one of those, or set your own Gemini key. "
                       "Nothing was spent.")
        return GeminiClient(api_key=key), is_platform

    def _run(fn):
        try:
            return fn()
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))
        except ValueError as e:
            raise _bad(str(e))

    def _read_images(images: Optional[list]) -> list[dict]:
        """The input images' bytes, read server-side BEFORE the key is resolved: a source
        that cannot be read spends nothing."""
        from .. import file_source
        out = []
        for i, source in enumerate(images or []):
            try:
                rf = file_source.resolve(source, max_bytes=MAX_INPUT_IMAGE_BYTES)
            except file_source.FileSourceError as e:
                raise _bad(f"Gemini: input image {i + 1} could not be read ({e}). Nothing "
                           "was done.")
            if not (rf.mime or "").startswith("image/"):
                raise _bad(f"Gemini: input image {i + 1} is {rf.mime!r}, not an image. "
                           "Nothing was done.")
            out.append({"data": rf.data, "mime_type": rf.mime})
        return out

    @mcp.tool(name="gemini_image", annotations=LECTURE)
    def gemini_image(
        op: Literal["list_models", "estimate"],
        model: Optional[str] = None,
        image_size: Optional[Literal["1K", "2K", "4K"]] = None,
        prompt: Optional[str] = None,
        image_count: Optional[int] = None,
    ) -> Any:
        """Gemini images (Nano Banana), read-only and free: the models, and what a call
        would cost at most. The generation is `gemini_image_act`.

        `op`:
        - `list_models` — the image models this key can call (`name` is what `model`
          takes, e.g. `gemini-nano-banana-2.1`), with `priced` true for those this
          connector can price — the only ones the SHARED key serves.
        - `estimate` — the most one `create_image` would cost, in US dollars (`usd`),
          from the published price table: `model` (default `gemini-nano-banana-2.1`),
          `image_size` (1K default, 2K, 4K), `prompt` and `image_count` (input images).
          No call to Google. The call itself bills what Google counted, usually less.

        Args:
            op: What to read (see above).
            model: `estimate`: the model code (default gemini-nano-banana-2.1).
            image_size: `estimate`: 1K (default), 2K or 4K.
            prompt: `estimate`: the prompt, whose length counts in the bound.
            image_count: `estimate`: how many input images the call would send (0 to 14).
        """
        if op not in _READ_OPS:
            raise _bad(f"Unknown op {op!r}: expected one of {', '.join(_READ_OPS)}.")
        if op == "list_models":
            _refuse_ignored(op, model=model, image_size=image_size, prompt=prompt,
                            image_count=image_count)
            key, _ = access.resolve_api_key(_NAME)
            client = GeminiClient(api_key=key)
            listed = _run(lambda: client.list_models())
            session_org.note_call_trace(quantity=0, units=0)
            models = []
            for m in listed.get("models") or []:
                name = str(m.get("name") or "").removeprefix("models/")
                if "image" not in name and "banana" not in name:
                    continue
                if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                    continue
                models.append({"name": name, "display_name": m.get("displayName"),
                               "description": m.get("description"),
                               "priced": name in PRICES})
            return {"models": models, "default": DEFAULT_MODEL}
        m = model or DEFAULT_MODEL
        n = image_count or 0
        if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= MAX_INPUT_IMAGES:
            raise _bad(f"`image_count` is a whole number from 0 to {MAX_INPUT_IMAGES}.")
        _check_shape(m, image_size, None, prompt, None)
        session_org.note_call_trace(quantity=0)
        usd = bound_usd(m, image_size, prompt or "", n)
        if usd is None:
            return {"model": m, "usd": None, "priced": False,
                    "basis": f"no price for {m!r}: priced models are {', '.join(sorted(PRICES))}"}
        return {"model": m, "image_size": image_size or "1K", "usd": usd, "priced": True,
                "basis": ("at most: the output image at its size, a thinking allowance, the "
                          "prompt and the input images, at Google's published rates; a call "
                          "bills what Google counted")}

    @mcp.tool(name="gemini_image_act", annotations=ACTE)
    def gemini_image_act(
        op: Literal["create_image"],
        prompt: Optional[str] = None,
        model: Optional[str] = None,
        images: Optional[list[dict]] = None,
        aspect_ratio: Optional[str] = None,
        image_size: Optional[Literal["1K", "2K", "4K"]] = None,
        max_usd: Optional[float] = None,
    ) -> dict:
        """Generate or edit an image with Gemini (Nano Banana). EVERY call spends: on your
        own key your Google billing, on the shared key your organization's credits, at
        what Google counted (`gemini_image` op='estimate' gives the most it can cost).
        Not retried.

        `op`:
        - `create_image` — one image from `prompt`. With `images` (up to 14 file
          references, in order: products, characters, a style to follow, or the image to
          change) the prompt says what to do with them, e.g. "put this product on a
          marble table", "same character, now running". `aspect_ratio` (1:1, 16:9, 9:16,
          4:3, 3:4, 3:2, 2:3, 4:5, 5:4, 21:9, and 1:4, 4:1, 1:8, 8:1), `image_size` 1K
          (default), 2K or 4K. `model` defaults to `gemini-nano-banana-2.1`; the SHARED
          key serves only the models `gemini_image` op='list_models' marks `priced`.
          `max_usd` refuses the call before it is sent when its bound is above it.

        Returns `images` (each a signed `url`, valid `expires_in` seconds, deleted after a
        day: download right away), any `text` the model added, `finish_reason`, and
        `cost` (`usd`, `basis`). No image means the model declined (a
        filtered prompt): `finish_reason` and `text` say why; its input tokens are still
        billed. Every image carries Google's invisible SynthID watermark.

        Args:
            op: What to do (see above).
            prompt: What to draw, or what to do with the input images.
            model: The model code (default gemini-nano-banana-2.1).
            images: Input images, in order, each a file reference, e.g. {"kind": "url", "url": "https://..."}, {"kind": "transit", "ref": "..."} or {"kind": "project_file", "project_id": 1, "file_id": 2}.
            aspect_ratio: e.g. 1:1, 16:9, 9:16 (the model's default when omitted).
            image_size: 1K (default), 2K or 4K — uppercase K.
            max_usd: Optional ceiling in US dollars: refused before it is sent when the call's bound is above it.
        """
        if op not in _ACT_OPS:
            raise _bad(f"Unknown op {op!r}: expected one of {', '.join(_ACT_OPS)}. "
                       "Nothing was done.")
        if not isinstance(prompt, str) or not prompt.strip():
            raise _bad("op='create_image' requires `prompt`. Nothing was done.")
        m = (model or DEFAULT_MODEL).strip()
        _check_shape(m, image_size, aspect_ratio, prompt, images)
        if max_usd is not None and (isinstance(max_usd, bool) or not max_usd > 0):
            raise _bad("`max_usd` must be a positive amount in US dollars. Nothing was done.")
        bound = bound_usd(m, image_size, prompt, len(images or []))
        if max_usd is not None and bound is not None and bound > max_usd:
            raise _bad(f"{ABOVE_MAX_USD}: this call may cost up to ${bound:.4f}, above "
                       f"max_usd ${max_usd:.4f}. Nothing was sent.")
        inputs = _read_images(images)
        client, is_platform = _client(m, units=microdollars(bound) if bound is not None else 1)
        result = _run(lambda: client.create_image(
            prompt, m, images=inputs, aspect_ratio=aspect_ratio, image_size=image_size))
        usage = result.get("usage_metadata")
        try:
            cost = cost_of(m, usage)
        except UsageUnreadable as e:
            logger.error("gemini_image: usage unreadable (%s), platform=%s model=%s "
                         "usage=%r", e, is_platform, m, usage)
            if is_platform:
                raise _bad(f"{USAGE_UNREADABLE}: Gemini answered without saying what it "
                           f"counted by modality ({e}), so the call cannot be billed at "
                           "what it cost: it is refused and its images are not served. "
                           "Retry; if it happens again, report it.") from None
            cost = {"usd": None, "basis": f"unknown: {e}"}
        spent = microdollars(cost["usd"]) if cost["usd"] is not None else 0
        # `units` (the grant's quota axis) on the shared key only; None is not noted.
        session_org.note_call_trace(quantity=spent, units=spent if is_platform else None)
        from ..auth.hooks import current_user_sub_from_token
        sub = current_user_sub_from_token()
        stem = (result.get("response_id") or "image")[:32]
        out_images = [_deposit(img, stem, i, sub) for i, img in enumerate(result["images"])]
        return {"images": out_images, "text": result.get("text") or "",
                "finish_reason": result.get("finish_reason"), "model": m,
                "model_version": result.get("model_version"),
                "cost": {**cost, "usage": result.get("usage_metadata") or {}}}
