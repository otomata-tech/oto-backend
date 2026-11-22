# Update HubSpot and the call queue after each cold call

**When to use it**: a caller works a queue in HubSpot and, after each dial, types a short note in their own words: "no budget this year", "ring back early March", "wrong person, ask for the ops manager". Nobody tags anything, so the queue never moves on its own and callbacks slip. This reads those notes every hour, turns each one into a queue state with a rule table, puts callbacks back in the queue on the day they fall due, and sends the caller a morning list of who to ring back. It never sends an email, books a meeting, creates a deal or deletes anything.

```
              Scheduled routine, hourly in calling hours on weekdays
              "Route the call notes logged in HubSpot since the last run."
                         │
                         ▼
╔═════════════════════════════════════════════════╗
║  0 · Check both watermarks                      ║   data_rows
║  A missing or unreadable watermark stops the    ║   one for notes, one for calls
║  run rather than replaying the whole history.   ║
╚════════════════════════╤════════════════════════╝
                         ▼
┌─────────────────────────────────────────────────┐
│  1 · Read the caller's new notes                │   hubspot_object
│  Notes under the caller's owner id since the    │   notes, then associations
│  watermark, oldest first, with their contact.   │
└────────────────────────┬────────────────────────┘
                         ├───────────────▶  ▪ orphan note        no contact associated
                         ▼
╔═════════════════════════════════════════════════╗
║  2 · Classify by the rule table                 ║   most restrictive signal wins
║  Collect every signal, then apply precedence;   ║
║  only an unmatched note reaches the model.      ║
╚════════════════════════╤════════════════════════╝
                         ├───────────────▶  ▪ to review          no rule matched, state untouched
                         ▼  one outcome, matched by the table
┌─────────────────────────────────────────────────┐
│  3 · Route the outcome                          │──▶  Call queue  out on every routed state
│  Set its state and dates; only a profile URL    │──▶  HubSpot note  the decision and date, never their text
│  in a reroute spends credits on a referral's    │──▶  HubSpot task  callback, reroute, review, their words
│  mobile number.                                 │   apollo_match_person · apollo_reveal_phone
└────────────────────────┬────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────┐
│  4 · Count the missed dials                     │   hubspot_object on calls
│  One attempt per outbound call that did not     │   inbound never counts
│  connect, read from the call records.           │
└────────────────────────┬────────────────────────┘
                         ├───────────────▶  ▪ attempt cap        out of the queue, note written
                         ▼
┌─────────────────────────────────────────────────┐
│  5 · Put due contacts back in the queue         │──▶  Call queue  back in, state in_queue
│  Callbacks due by tonight, overdue included,    │   hubspot_list add_members
│  and email requests whose wait is over.         │
└────────────────────────┬────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────┐
│  6 · Write the watermarks last                  │   data_write
│  Both move only once routing is done, then      │
│  every counter for the run is reported.         │
└────────────────────────┬────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────┐
│  7 · Collect today's callbacks                  │   hubspot_object, read only
│  A separate morning run that only reads: due    │   day bounds as UTC instants
│  and overdue callbacks, and email requests.     │
└────────────────────────┬────────────────────────┘
                         ├───────────────▶  ▪ nothing due        no list is sent
                         ▼
┌─────────────────────────────────────────────────┐
│  8 · Send the list to the caller                │──▶  Slack DM  never a do-not-call, never empty
│  One line per contact, by time, with the        │   slack_open_dm · slack_post_message
│  words of the call note, never a summary.       │
└─────────────────────────────────────────────────┘

▪ terminal: that note, contact or morning run stops there
```

Steps 0 to 6 run every hour within calling hours on weekdays. Steps 7 and 8 are a separate run each weekday morning, before calling starts, and that run only reads. HubSpot holds the state; this process only moves it.

**Two systems already own work this must not repeat.** Your meeting-booking flow owns the calendar event, the confirmation and the lifecycle stage. Your email follow-up owns the send. On a meeting or an email request, the router changes the queue state, writes a note, and stops there.

## What you set up once
- **The call queue**: a MANUAL list, <tool:hubspot_list> `op="create"` with `processing_type="MANUAL"`. Its membership belongs to this router, apart from the one-time add when a newly sourced contact arrives.
- **The caller's owner id**, from <tool:hubspot_owners>. Everything below reads and writes under it.
- **Contact properties**, created with <tool:hubspot_property> `op="create"` under your own prefix. Read them back with `op="list"` before any write: an enumeration only accepts its declared `options[].value`, and a write built from a label fails or writes nothing.

| Property | Type | Values |
|---|---|---|
| `queue_state` | enumeration | `in_queue`, `callback`, `email_requested`, `meeting`, `reroute`, `dnc`, `out` |
| `out_reason` | enumeration | `not_interested`, `left_company`, `bad_number`, `already_contacted`, `do_not_call`, `max_attempts` |
| `callback_at` | datetime | when the prospect asked to be called back |
| `reentry_at` | datetime | when an email-requested contact returns to the queue |
| `call_attempts` | number | outbound calls the prospect did not pick up |
| `last_outcome` | single-line text | the caller's words from the last routed note, prefixed `to review:` when the table could not classify it |
| `route_detail` | single-line text | how the router read that note: the date rule that fired, or the model's suggestion |
| `last_note_id` | single-line text | the id of the last note routed onto this contact |

- **A small state table** with two watermark rows, `watermark_notes` and `watermark_calls`, both ISO datetimes, plus one row per pending phone reveal, keyed on the referral's LinkedIn URL and holding its `request_id` (step 3).

## 0. Check both watermarks
- <tool:data_rows> on the state table. `watermark_notes` is the `hs_timestamp` of the last note routed and drives steps 1 to 3. `watermark_calls` is the `hs_timestamp` of the last call counted and drives step 4. They move independently.
- **A missing row, an empty value or a value that does not parse stops the run** and reports it as blocked. Never fall back to a default: a run with no watermark replays the whole history at once.

## 1. Read the caller's new notes
- <tool:hubspot_object> `op="search"`, `object_type="notes"`, with `filters` on `hs_timestamp` `GT` the notes watermark and on the owner property `EQ` the caller's owner id, `properties=["hs_note_body","hs_timestamp","hubspot_owner_id"]`, `limit=100`. Page on `paging.next.after` until it is absent, then process the notes oldest first, so a contact's later note overwrites an earlier one.
- **The owner filter is load-bearing, not a convenience.** Machine-written notes (call-prep cards, and this router's own notes) carry no owner, and the owner is the only thing that separates a human outcome from a generated card. Notes under another rep's owner id are not read either.
- `op="associations"` with `to_object_type="contacts"` on each note. A note with no contact is counted as an orphan and routed nowhere.
- `op="get"` on each contact for `queue_state`, `call_attempts` and `last_note_id`. **Skip any note whose id already sits in the contact's `last_note_id`.** The watermark alone is not enough: it is written last, so a run that dies halfway reads the same notes again on the next hour.

## 2. Classify by the rule table
**Normalize first.** Strip the HTML, turning `<br>` and `</p>` into newlines, unescape entities, and collapse runs of spaces: that is the clean text. From it, build the matching text: Unicode NFKD, drop the combining marks, lower-case, and replace every run of non-alphanumeric characters (keeping `/`) with a single space. **Match on the matching text, store the clean text** in `last_outcome`, so the caller's accents and capitals survive.

**Collect every signal that matches, not just the first.** One note often carries several intents: "email" and "ring back Tuesday afternoon" on two lines, "no need for it, do not call again", "left the company, ask for the finance director". Write the phrasings in the language your callers actually type, and be generous about spelling: notes are typed between dials, and the misspellings are part of the data.

| Signal | Illustrative phrasings, to adapt to your callers |
|---|---|
| `dnc` | "do not call", "don't ring", **unless** the note also says the person is away or on leave, was already reached, or it is a personal number |
| `bad_number` | "wrong number", "personal number", "personal mobile" |
| `left_company` | "left the company", "no longer there" |
| `reroute` | "ask for the", "contact the", "not the right person", "not the decision maker" |
| `meeting` | "meeting booked", "demo", "video call", **unless** aspirational: "to set up a meeting", "maybe a demo", "to arrange" |
| `callback` | "call back", "callback", "try again", "in a meeting", "away until", "on leave", "driving" |
| `email` | the whole note is "email" or "mail", or starts with it, or "send an email" |
| `not_interested` | "not interested" and its misspellings, "no need", "no budget", "not a priority", "we have a supplier", "out of target" |
| `already_done` | "already contacted", "already called", "already reached" |
| `call_quality` | "bad line", "call dropped", "couldn't hear" |
| `prep_instruction` | "tell him", "tell her", "mention who" |

**The guards are not decoration.** Each one stops a wrong routing that a plain match would make. "On leave until next Monday, do not call" is a person out of office, not a do-not-call. "Already spoke to them, don't ring" means the conversation already happened. "Private mobile, do not call on it" is about that one number. And "call back to set up a video call" is a callback that *hopes* for a meeting: routing it to `meeting` pulls a live contact out of the queue for a meeting nobody booked.

**Not every note is an outcome.** A reminder the caller leaves for their next dial ("mention who referred you") routes nothing. Notes like that are counted and left alone.

**Precedence, most restrictive first.** The first signal present wins the state:

`dnc` → `left_company` → `reroute` → `meeting` → `callback` → `email` → `not_interested` → `already_done` → `bad_number` → `call_quality` → `prep_instruction`

| Winning signal | Queue state | Out reason |
|---|---|---|
| `dnc` | `dnc` | `do_not_call` |
| `left_company` | `out` | `left_company` |
| `reroute` | `reroute` | none |
| `meeting` | `meeting` | none |
| `callback` | `callback` | none |
| `email` | `email_requested` | none |
| `not_interested` | `out` | `not_interested` |
| `already_done` | `out` | `already_contacted` |
| `bad_number` | `out` | `bad_number` |
| `call_quality`, `prep_instruction` | unchanged | none |

**A `reroute` signal always raises a task, even when another signal wins the state.** "Left the company, ask for the finance director" is correctly terminal for that contact and still names work to do.

### When the table misses, the model reads it
Only a note that matched no signal, or an empty note, goes to the model: the note and nothing else, asking for one of the eleven signals or `none`, plus a confidence. **A model verdict never moves a queue state.** Put `to review:` in front of the caller's verbatim text in `last_outcome`, write the suggestion and its confidence into `route_detail` and the review task, and leave the state as it is. When the same phrasing keeps coming back in review tasks, it belongs in the table, which is an edit to this process.

### Resolving a callback date
**Anchor every date on the day the note was written, never on the day of the run.** "Ring back early March" written in late February means this March. Routed by a run that happens after the 1st of March (a replay after an outage, say) and anchored on that run's date, it resolves to the following year.

| Expression | Resolves to |
|---|---|
| a full date, or a bare day and month | that date; a bare day and month rolls to next year only if already past **the note's date** |
| in N days, weeks or months | the note's date plus N |
| early, mid or late in a month | the 1st, the 15th or the 25th |
| the Nth week of a month | day 1 + 7 × (N − 1) |
| a bare month | the 1st |
| a weekday name | the next such weekday after the note's date |
| a time only | the next day at that time |

The default time is **[09:00 in your timezone]**, and an explicit time overrides it. Record which rule fired in `route_detail`, never in `last_outcome`, so a wrong guess is visible without overwriting the caller's words. **A callback with no date at all** ("in a meeting", "driving") takes the callback state and no `callback_at`. HubSpot requires a due date on a task, so its task is due at the note's own `hs_timestamp`, with the subject `Callback, no date: <their text>`, and lands in the caller's task list as already due. Never invent a date. With no `callback_at`, that contact never comes back into the queue on its own: it stays in `callback`, out of the queue and off the morning list, until the caller's next note on it routes it.

## 3. Route the outcome
Every route writes `last_note_id` (the note's id), `last_outcome` and, where there is one, `route_detail` with <tool:hubspot_object> `op="update"`.

### The router's notes carry the decision, never the caller's text
A router note states the routing decision and the date, and nothing else: no quote, no excerpt, no paraphrase of what the caller wrote. Their words already live in two places that lose nothing: their own note, which this process never touches, and `last_outcome`, which is a property and triggers nothing. Write notes with `op="add_note"` on the contact, from these templates, where DD/MM is the note's own date:

| Outcome | Note written on the contact |
|---|---|
| `not_interested` | Router: not interested, note of DD/MM. |
| `dnc` | Router: do not call again, note of DD/MM. |
| `left_company` | Router: left the company, note of DD/MM. |
| `bad_number` | Router: invalid number, note of DD/MM. |
| `already_contacted` | Router: already contacted, note of DD/MM. |
| `callback` | Router: callback set for DD/MM at HH:MM. |
| `callback`, no date | Router: callback asked with no date, note of DD/MM. |
| `email_requested` | Router: written follow-up requested DD/MM, back in queue DD/MM. |
| `meeting` | Router: meeting booked, note of DD/MM. |
| `reroute` | Router: contact to change, task created. |
| `max_attempts` | Router: out of the queue after [N] unanswered calls. |
| re-entry, callback | Router: back in queue, callback asked on DD/MM. |
| re-entry, email | Router: back in queue after written follow-up of DD/MM. |

### The hard guard on every note body
If any automation in your portal fires on a note's text (a workflow that sends a follow-up email when a note contains the word "email", for instance), a router note carrying that word sends a real email to a real prospect with no human in the loop. **Before writing any note**, take the exact body about to be written, apply NFKD, drop the combining marks, lower-case it, and test it against `\be?-?mails?\b`. On a match, write no note, still set the properties the route requires, count it as suppressed, and carry on. Never reword the body to slip past the guard, and never retry it. None of the templates above trips it: the guard is there for the day one of them is edited, or a value is interpolated into one.

**A task is not a note**, and it keeps the caller's words verbatim: `Callback: <their text>`, `Callback, no date: <their text>`, `New contact to find: <their text>`, and the review task. Confirm no workflow of yours reads task text before relying on that.

### Routing each state
- **Terminal, `out` and `dnc`.** <tool:hubspot_list> `op="remove_members"` from the call queue, then set `queue_state` and `out_reason`. On `not_interested`, also set `hs_lead_status` to `UNQUALIFIED` and mark the contact as a soft no if you run reactivation campaigns later. On `dnc`, set `UNQUALIFIED` and **no** reactivation marker: a do-not-call is not a soft no and is never reactivated.
- **`callback`.** Remove from the queue, set `callback_at` (none for a callback with no date), and create a task: <tool:hubspot_object> `op="create"`, `object_type="tasks"`, `properties={"hs_task_subject": "Callback: <their text>", "hs_task_type": "CALL", "hubspot_owner_id": "<the caller's owner id>", "hs_timestamp": "<the callback datetime>"}`. On a task, `hs_timestamp` is the due date, and HubSpot refuses a task without one: for a callback with no date, pass the note's own `hs_timestamp`. **Pass the contact in `associations` on the same create call**, as a HubSpot v3 association object: a task created without it is unattached and never shows on the contact. The reroute and review tasks use the same shape and the same association, with their own subject.
- **`email_requested`.** Remove from the queue and set `reentry_at` to the note's date plus **[7 days]**. Send nothing.
- **`meeting`.** Remove from the queue. Book nothing, create no deal, and leave the lifecycle stage alone.
- **`reroute`.** Remove from the queue, set the state, and create the task `New contact to find: <their text>`. **If, and only if, the note carries a LinkedIn profile URL**, resolve the referral, on your own Apollo key:
  1. <tool:apollo_match_person> with `linkedin_url`. A response carrying `person._stub: true` is a failed match, not data.
  2. <tool:apollo_reveal_phone> with the same `linkedin_url` and `webhook_url` set to **[an HTTPS endpoint you control]**. The numbers are not in this response. **Write the returned `request_id` with <tool:data_write> to a pending-reveal row keyed on the referral's LinkedIn URL, before anything else**: lose it, and the credits are spent with nothing to collect. If a row for that URL already exists, collect that request instead of revealing again.
  3. <tool:apollo_reveal_phone_result> with that `request_id`, passed as a string. While `done` is false, wait `retry_after_seconds` and call again. The numbers sit at `result.webhook_result.people[].phone_numbers[]`. Keep the first one whose `type_cd` is `mobile` and whose `status_cd` is `valid_number`, then read its `dnc_status_cd` before anyone dials it.
  4. <tool:hubspot_object> `op="create"` the referral contact with `queue_state` `in_queue` and `call_attempts` at zero, associate it to the company, and add it to the queue, even if you normally queue only one contact per company.

  **A note that names a person without a URL never triggers an Apollo call.** A first name and a job title are not an identifier, a weak match still costs a credit, and the task is the answer.

## 4. Count the missed dials
This runs independently of the notes.
- <tool:hubspot_object> `op="search"`, `object_type="calls"`, `hs_timestamp` `GT` the calls watermark and the owner `EQ` the caller's owner id, oldest first, each call resolved to its contact.
- **Skip any contact whose `queue_state` is not `in_queue`.** A contact already routed out is not accruing failed attempts.
- **Key the disposition on a field that maps one-to-one to an outcome.** Read your dialer's call records before choosing: a disposition id can cover several different outcomes while the call title maps to exactly one, or the reverse. Build the mapping from your own records, as exact strings.
- **Adds an attempt** (`op="update"`, `call_attempts` plus one): an outbound call that did not reach the prospect (canceled, voicemail, no answer, busy, failed). **Never adds one**: any connected call in either direction, and any inbound call, because a prospect ringing you is not one of your failed attempts. A value outside the mapping is logged as unknown and routed nowhere.
- At **[N]** attempts, the cap you choose: remove from the queue, `queue_state` `out`, `out_reason` `max_attempts`, and the template note.
- A connected call on an `in_queue` contact with no note in the same window is counted as "connected without outcome", so the gap between a real conversation and a recorded outcome stays visible. Nothing changes on the contact.

## 5. Put due contacts back in the queue
This runs every hour, whatever steps 2 and 4 found, because a callback comes due on the clock.
- **Callbacks due by the end of today, overdue included.** <tool:hubspot_object> `op="search"` on contacts with `queue_state` `EQ` `callback` and `callback_at` `LTE` the end of today in **[your timezone]**. **Use `LTE`, not a window on today**: the date rules often land on a weekend (the 1st of a month, "in 10 days", a time-only note written on a Friday), and after any missed run day a window on today leaves those contacts in `callback`, out of the queue and out of the morning list, for good. Once back, the state is `in_queue`, so each contact re-enters exactly once. **Compute the day's end as a UTC instant before filtering**: HubSpot stores datetimes in UTC, and a naive date comparison is off by your offset. Each one goes back to `in_queue` with the re-entry note. Leave `callback_at` in place until a new note routes the contact.
- **Email requests whose wait is over.** `queue_state` `email_requested` and `reentry_at` `LTE` now. Each one goes back to `in_queue` with the re-entry note.
- <tool:hubspot_list> `op="add_members"` with the hour's contacts in one `record_ids` list.
- `dnc`, `reroute`, `meeting` and `out` never re-enter. Only a person puts those back.

## 6. Write the watermarks last
- <tool:data_write> both watermarks **only after the routing is done**: the notes watermark to the `hs_timestamp` of the last note processed, the calls watermark to that of the last call counted. A side that read nothing stays untouched. Omit a field rather than writing a null into it.
- Pace the HubSpot writes: a private app is capped at 190 requests per 10 seconds. A failed call is retried once, then logged and skipped, and one bad contact never stops the run.

## 7. Collect today's callbacks
A separate run each weekday at **[a time before calling starts]**. **It only reads**: no property, no list membership, no task, no state change.
- "Today" is the calendar day in **[your timezone]** at the moment of the run, bounded as UTC instants.
- **Callbacks still waiting.** <tool:hubspot_object> `op="search"` on contacts with `queue_state` `EQ` `callback` and `callback_at` `LTE` the day's end. Like the hourly pass, this deliberately catches anything overdue, so a callback that fell due on a weekend is on Monday's list.
- **Callbacks already back in the queue.** A second search with `queue_state` `EQ` `in_queue` and `callback_at` `BETWEEN` the start of the day after your previous scheduled morning run (Saturday, on a Monday) and the day's end (`value` and `highValue`). The hourly pass leaves `callback_at` in place when it puts a contact back, and starting the window after the previous run keeps yesterday's callbacks off today's list. Merge both results on the contact id.
- **Email re-entries.** `queue_state` `EQ` `email_requested` and `reentry_at` `LTE` the day's end. This deliberately includes anything overdue, so a request not yet re-entered still surfaces.
- Page every search to the end, reading first name, last name, company, mobile, `queue_state`, `callback_at`, `reentry_at` and `last_outcome`.
- **`dnc`, `reroute`, `meeting` and `out` never appear, whatever date they carry.** The state filters already exclude them: never widen those filters. A do-not-call surfaced as someone to ring is the one error here that puts you in the wrong, rather than wasting a call.
- If every search comes back empty, stop. Never send an empty list.

## 8. Send the list to the caller
- **One line per contact**, earliest first: time, name, company, mobile, then on the next line **the words of the call note, verbatim**, from `last_outcome`. Never summarized, translated, corrected or tidied: the point is that the caller rereads what was actually said, not a paraphrase of it. A contact with no mobile still gets a line, with the field blank. A `to review:` prefix stays visible, so the caller sees that one of their notes was not understood. Email re-entries go under their own heading, with no time.
- <tool:slack_find_user_by_email> on **[the caller's work email]**, <tool:slack_open_dm> with the returned user id, then <tool:slack_post_message> to that DM channel. One recipient only, the caller.
- A long list is split into threaded parts, never truncated. Read `split_into` in the response before concluding anything was lost, and never post it again.
- **If the caller cannot be reached in Slack** (no user for that email, or the DM will not open), send nothing and say so. The `CALL` tasks from step 3 already sit in the caller's own HubSpot task list, so every callback still reaches them. Report how many contacts the list would have held, so the coverage stays visible.

## Rules
- Never delete or archive anything in HubSpot: not a contact, a company, a call or a note.
- Never remove a contact from the queue except through a routed state or the attempt cap.
- Never send an email, book a meeting, or create a deal or a calendar event.
- The model never moves a queue state. Only the rule table does.
- No router note copies the caller's words, and none is written without passing the guard.

## Output
Each hourly run reports: notes read and routed per state; unclassified and model-assisted notes; attempts added and contacts removed at the cap; callbacks and email requests put back in the queue; referrals resolved; reroute and review tasks raised; connected calls without an outcome; orphan notes; unknown dispositions; and suppressed notes. The morning run reports which path it took (list sent, nothing due, or caller unreachable) and how many contacts the list held.