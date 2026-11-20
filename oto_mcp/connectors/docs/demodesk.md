## prerequisite — a demodesk api key

in Demodesk, open **Settings → Integrations → Other** and generate an **API key**, then paste it into oto. Reference: [Demodesk Public API v2](https://api-docs.demodesk.com/api/docs/v2/generated.yaml).
- ⚠️ **the key sees what its creator sees**: a rep's key reads that rep's meetings and what their groups may see. To read the whole company, create the key as an **admin who is a member of every restricted group**
- a recording set to **only me** stays visible to its host alone, whatever the key
- your own key only: no shared oto key — these are your company's recorded calls; set it for yourself or for the whole org
- the "test connection" button reads who the key belongs to (`/me`): no meeting data is touched

## usage — meetings, recordings, transcripts, AI notes

one tool, `demodesk`, read only; each call names its function in `op`.

- "whose key is this, what can it see" → `demodesk(op="get_current_user")`
- "my calls this month that are processed" → `demodesk(op="list_recordings", hosts=["me"], postprocessing_statuses=["done"], meeting_from="2026-11-01T00:00:00Z")`
- "our calls with this company" → `demodesk(op="list_recordings", attendee_domains=["example.com", "example.de"])` — filter by **every email domain** the company uses, not by the meeting title
- "what was decided on that call" → `demodesk(op="list_summaries", token="…")`: the AI notes, one per summary template — read them before the transcript
- "what was said, word for word" → `demodesk(op="get_transcript", token="…")`, `lang="fr"` to translate it
- "the transcripts of these calls" → `demodesk(op="get_transcripts", tokens=["…", "…"], format="plaintext")`, up to 25 at once, each with its own status
- "how did this call score" → `demodesk(op="list_scorecards", token="…")`
- "my meetings next week, recorded or not" → `demodesk(op="list_meetings", hosts=["me"], starts_from="…", starts_before="…")`; each meeting's `recordingTokens` open its recordings
- "find a teammate" → `demodesk(op="list_users", search="jane")`; `hosts` and `attendees` also take an email directly

## note — what is misleading

- ⚠️ **`list_meetings` is ordered oldest first**: without `starts_from` the first page is the oldest meetings. Bound it by date
- **a 404 does not always mean "does not exist"**: a host-only or only-me recording, or one of a restricted group the key's user is not in, answers 404 too
- **a transcript that is still being processed** comes back as an `error` object instead of `data`: read it again later; an **empty answer** means the transcript exists but is empty or too short. `postprocessing_statuses=["done"]` lists only the calls whose transcript and summaries are ready
- meetings held in Zoom, Teams or Meet stay `scheduled` while they run; `ended` does not mean the attendees came
- **transcripts are heavy**: an hour of call is tens of thousands of characters. Prefer `list_summaries`, and `format="plaintext"` on `get_transcripts`
- the direct link to a recording's video never comes back; `recordingWebUrl` opens the call in Demodesk
- recordings, transcripts and attendee lists carry **personal data**: read only what the task needs
- pages: lists return `next_cursor`, passed back as `cursor`; up to 100 per page (`limit`)
- rate limit: **120 requests per minute per key**, 60 for the meetings and recordings lists (429 above)
- not served: deleting a recording, settings and keyword lists, groups and their members — done in the Demodesk app
