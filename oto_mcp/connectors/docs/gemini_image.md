## prerequisite — a Gemini API key with billing on

in Google AI Studio ([aistudio.google.com](https://aistudio.google.com)) → **Get API key** → **Create API key**, then paste it into oto.
- ⚠️ **billing must be enabled** on the key's Google project: a free-tier project has no image quota, and Google refuses the first image (`429`, `limit: 0`). The connection test only says the key works, not that the project is billed
- your own key: every image is charged to **your** Google billing; each organization or person sets its own key
- or the **shared key**, when the instance grants it to your organization: no key to set; each image is billed to the organization at what Google counted (input, image and thinking tokens, at Google's published rates), and each day's spend is capped by the grant's quota (in micro-dollars of cost), ONE quota for the whole organization, all its members together. A call whose bound would go over what is left today is refused before it is sent. The shared key serves only the models `gemini_image(op="list_models")` marks `priced` (Nano Banana 2.1)

## usage — a picture, a product shot, an edit

- "which models can I use?" → `gemini_image(op="list_models")`; the default is `gemini-nano-banana-2.1`
- "how much at most?" → `gemini_image(op="estimate", image_size="2K", image_count=1)`: free, from the published price table; about $0.04 (1K), $0.06 (2K), $0.12 (4K) a call, thinking included (measured 09/11)
- "make a picture" → `gemini_image_act(op="create_image", prompt="…", aspect_ratio="16:9", image_size="2K")`
- "put this product in a scene" / "same character, new pose" → add `images=[{"kind": "url", "url": "https://…"}]` (or a `transit` / `project_file` reference), up to 14, in order, and say in the prompt what to do with each
- "change this image" → the image in `images`, the change in `prompt` ("make the background white", "remove the text")
- the answer's `images[].url` are signed links kept one hour: download them right away

## note — what can mislead

- the model **thinks** before drawing, and that cannot be turned off: part of the cost is thinking tokens, billed at the text rate; `cost.usage` shows what Google counted
- no image in the answer means the model declined (a filtered prompt, a refused likeness): `finish_reason` and `text` say why, and the input is still billed
- `image_size` takes an uppercase K (`1K`, `2K`, `4K`); `max_usd` refuses a call whose bound is above it, before it is sent
- every image carries Google's invisible SynthID watermark
