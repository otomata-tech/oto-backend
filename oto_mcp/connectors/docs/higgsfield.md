## prerequisite — a Higgsfield API key

in the Higgsfield console ([console.higgsfield.ai](https://console.higgsfield.ai)), create an API key: it comes as a **key id** and a **secret**. Paste them into oto as ONE value joined by a colon: `KEY_ID:KEY_SECRET`. Reference: [Higgsfield authentication](https://docs.higgsfield.ai/docs/authentication.md).
- ⚠️ the secret is shown only once, when the key is created
- the API spends a **prepaid balance in dollars**, added in the console (it expires one year after it is added); a Higgsfield web plan's credits are a different thing and are not used here
- your own key: every generation spends **your** balance; each organization or person sets its own key
- or the **shared key**, when the instance grants it to your organization: no key to set; a job is billed to the organization only once it **completes** (failed, nsfw and canceled jobs cost nothing), at the price checked before it was sent, and each day's spend is capped by the grant's quota (in micro-dollars of job price). A job that would go over what is left today is refused before it is sent. On the shared key, reading, canceling or deleting a job works only for a job your organization submitted

## usage — a still, a clip

- "which models can I use?" → `higgsfield(op="list_models", output_type="video")`; each model's parameters are on its page at [docs.higgsfield.ai](https://docs.higgsfield.ai)
- "how much would it cost?" → `higgsfield(op="estimate", model=…, params={…})`: free, and a wrong parameter is named there before anything is spent
- "make a still" → `higgsfield_act(op="create_generation", model="higgsfield-ai/soul/v2/standard", params={"prompt": "…", "aspect_ratio": "16:9", "resolution": "1080p"}, max_usd=0.05)`
- "animate this still" → `model="kling-video/v3.0/pro/image-to-video"` or `"bytedance/seedance-2.5/image-to-video"` with `image_url`, `prompt`, `duration`; a first and a last frame → `kling-video/o3/first-last-frame`
- then `higgsfield(op="get_request", request_id=…)` until `status` is `completed`: `outputs` lists the files
- a local image as input → `higgsfield_act(op="create_upload", content_type="image/png")`, PUT the file to `upload_url` with every header of `upload_headers`, then pass `public_url` as `image_url`

## note — what can mislead

- `create_generation` **refuses** a job above `max_usd`, or whose price cannot be read: say the price to the person and get their OK before raising it
- `max_usd` itself has a ceiling set by the instance (`OTO_HIGGSFIELD_MAX_USD_PAR_APPEL`, 5 dollars by default): above it the job is refused (`max_usd_above_cap`) before Higgsfield is called
- some models are priced by a sentence of per-second rates rather than an amount: the price read from it is marked `approximate` (16:9 output, before discount); a job with a **video as input** on such a model cannot be priced and is refused
- GPT Image 2.5 (`marketing-studio/image/flare`, `/sunburst`, same price) is priced by the token, and Higgsfield never says what a job cost: the price is the **charge measured per `quality` and `resolution`** (no margin), marked `approximate`, and it is what the shared key bills. Square and 4k are the dearest; each image in `image_urls` adds $0.013. About $0.19 for the default (`quality` high, 4k), $0.03 at low, $0.72 at max
- Kling and Seedance make **sound by default**: `sound: "off"` / `generate_audio: false` unless sound is wanted, it costs more
- ⚠️ output files (images, videos) are the provider's **public links**: anyone holding one can read the file for seven days. Download them right away, never paste a link anywhere public
- a job takes seconds (a still) to minutes (a clip); `failed` and `nsfw` jobs are not charged, and a job still `queued` can be canceled and refunded
- the connection test says the key works, not that money is left: Higgsfield has no balance endpoint, and an empty balance shows only when a job is refused
