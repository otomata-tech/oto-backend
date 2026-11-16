# Post weekly Lemlist campaign results to Slack and Notion

**When to use it**: you run several outbound campaigns, often across more than one sending workspace. Reps see replies one at a time, and nobody compares campaigns, lanes and A/B variants on the same numbers or counts a meeting once, whichever tool recorded it. Once a week this reads everything, writes a snapshot and posts what changed. It counts; it never answers, pauses or relaunches anything.

```
              Scheduled routine, every Monday morning
              "Refresh the campaign dashboard for the last 30 days, one workspace only."
                         │
                         ▼
╔═════════════════════════════════════════════════╗
║  0 · Fix the week and check the keys            ║   lemlist_list_campaigns, per key
║  A free campaign list per workspace key, and a  ║   data_rows on this run's own snapshot
║  weekly digest already posted stops here.       ║
╚════════════════════════╤════════════════════════╝
                         ├───────────────▶  ▪ already posted    a replay, the digest is out
                         ▼  not posted yet, or an ad-hoc run
┌─────────────────────────────────────────────────┐
│  1 · Pull the counters and the activities       │   lemlist_campaign, batch_stats twice
│  Two counter batches, lookback and last week,   │   lemlist_get_activities, newest first
│  then each reply, interest and booking.         │   filter on createdAt yourself
└────────────────────────┬────────────────────────┘
                         ├───────────────▶  ▪ nothing sent      no sends last week, no errors
                         ▼  at least one send or one error
┌─────────────────────────────────────────────────┐
│  2 · Read each campaign's leads                 │   lemlist_get_leads, every state
│  Leads per A/B variant, and a campaign built by │
│  hand carries none.                             │
└────────────────────────┬────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────┐
│  3 · Attribute meetings three ways              │   hubspot_object, contacts and meetings
│  CRM meetings after launch, the reply text, the │   one meeting per lead email
│  Lemlist marker, once per email.                │
└────────────────────────┬────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────┐
│  4 · Bucket, rate and flag                      │   notion_get_blocks, naming convention
│  Name patterns into buckets, rates with a       │   no rate under ten sends
│  floor, only the changes worth saying.          │
└────────────────────────┬────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────┐
│  5 · Write the week's rows                      │   data_write, keyed week::campaign
│  One row per campaign per week, before any      │   sheets_spreadsheet when one is named
│  sink, so a failure replays cleanly.            │
└────────────────────────┬────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────┐
│  6 · Append the week to one Notion page         │   notion_search, notion_create_page
│  Weekly runs only: find the page by title,      │   notion_append_blocks
│  create it once, append a dated entry.          │
└────────────────────────┬────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────┐
│  7 · Post the digest, then stamp it             │   slack_post_message, thread_ts
│  A weekly run posts and stamps its rows; an     │   data_write, posted_at
│  ad-hoc run answers in the conversation.        │
└─────────────────────────────────────────────────┘

▪ terminal — the run stops there and nothing is posted
```

## 0. Fix the week and check the keys
- **The week** is the ISO week of the run date (`YYYY-Www`); it names the run. **The weekly window** is the last complete ISO week before the run date, from its Monday to its Sunday, both days included: on the scheduled Monday run, the week that just ended. **The lookback** defaults to **[90 days]**, computed at run time, and **the scope** to every workspace.
- **Two kinds of run.** The scheduled run with those defaults writes the `weekly` snapshot and is the only one that appends to Notion and posts. Any other lookback or a single workspace is an ad-hoc run: it writes `adhoc-<n>d-<workspace>` rows, answers with the same summary in the conversation, and never appends, posts or touches a `weekly` row. So a mid-week refresh never overwrites the week, never feeds next week's deltas and never reaches the channel. Words in the request switch parts off: skip the CRM, skip the reply classification, skip the sheet, skip Notion, skip Slack, or dry run (rows only).
- <tool:lemlist_list_campaigns> — free and without side effects, once per workspace key. If it fails, that key is dead: the run continues on the other workspaces and the digest names the missing one. **Every read carries the key of the workspace it reads** (one connector instance per workspace, passed on every call). If you run campaigns under a client's own key, the reads carry the client's key while table writes and posts carry yours. A stats call on the wrong key reads your own workspace and returns numbers that look plausible.
- A dead key is reported in your internal channel, **never in a channel an outside reader sees**. It is your problem, and a technical error there worries people without telling them anything.
- <tool:data_rows> — **the replay guard reads this run's own key only**: `week` and `snapshot` together. On a weekly run, **if any `weekly` row of this week carries `posted_at`, the digest is already out: stop.** Nothing upstream deduplicates a trigger. A restarted schedule, a replayed trigger or a manual rerun after an incident is a fresh session that knows nothing about the previous one. The row key protects the table; only this read protects the channel. An ad-hoc run posts nothing, so it has nothing to guard and is never stopped by a weekly stamp.
- <tool:data_rows> — last week's `weekly` campaign rows, the baseline for every delta and flag.

## 1. Pull the counters and the activities
- <tool:lemlist_list_campaigns> — all statuses, keeping id, name, status and creation date. `status` filters rather than partitions (a campaign can be paused and in error), so if you list by status, deduplicate on the id. `truncated: true` can appear on short lists: a campaign you expect and don't see is re-read with `max_campaigns` raised, never declared absent. Draft campaigns kept as templates send nothing, so skip them.
- <tool:lemlist_campaign> — `op="batch_stats"` with up to 100 `campaign_ids` per call, **twice**, both dates `YYYY-MM-DD`, both days included (a date-only bound is a whole UTC day):
  - **the lookback**: `start_date` at the lookback start, `end_date` today. These rolling counters feed the rates and the dashboard.
  - **the weekly window**: `start_date` the window's Monday, `end_date` its Sunday. These feed the flags and the nothing-sent exit. Two overlapping rolling totals cannot say what was sent in one given week, so without this call a send drop or a silent campaign is invisible.
  - Compute the dates in your sending timezone: shortly after midnight, the scheduler's UTC date names a different day. Keep `nbLeads`, `messagesSent`, `delivered`, `opened`, `clicked`, `replied`, `messagesBounced`, `nbLeadsUnsubscribed`, `meetingBooked`, the interested counter under the name the payload gives it, and the leads still in queue (`nbLeads` minus the leads already reached). A campaign that lands in the `errors` sibling of the results is listed in the digest, never silently omitted.
- <tool:lemlist_get_activities> — `activity_type="emailsReplied"`, plus your interested and meeting-booked event types (read their exact names from a real payload before relying on them), with `limit=100` and paging on `offset`. **Do not trust the date parameters to narrow the result.** Page newest-first, filter on `createdAt` yourself, and stop only once the oldest event on a page predates the lookback start. `all_pages` with `since` walks by date but ignores the type filter, so filter types yourself if you use it. Keep the campaign id, lead id and email, `createdAt` and, on a reply, the body. An event type you meet and do not count goes in the thread, never dropped silently.
- **Exit, nothing sent**: zero sends in the weekly window across every campaign read and no campaign in error means no row, no page and no post. A week without sending is not news, and a "nothing to report" post every quiet week teaches people to stop opening the channel.

## 2. Read each campaign's leads
- <tool:lemlist_get_leads> — the export, which reads leads in every state. Lemlist's own default filter would return an empty list that reads as "no leads"; this route has no state argument and cannot fall into it, while `lemlist_lead` with `op="list"` can, so if you list that way leave its state at `all`. Count leads per value of the custom variable holding the A/B variant (**[your variant variable, e.g. abVariant]**, set by whatever process created the lead) and the leads without one. A campaign built by hand carries no variable and gets no A/B row.
- For a native A/B test on an email step, <tool:lemlist_campaign> `op="batch_stats"` with `ab_selected="A"`, then `"B"`, reads each side's counters. It returns no lead count per side, so its floor in step 4 is applied to `messagesSent` per side instead.

## 3. Attribute meetings three ways
Three signals per lead email and campaign, each recorded on its own:
- **The CRM**, with <tool:hubspot_object>. First `op="search"` on `contacts` with `filters=[{"propertyName": "email", "operator": "IN", "values": [<up to 100 emails of leads who replied>]}]`, paging on `after`. Then `op="get"` with `associations=["meetings"]`, which returns meeting ids inline instead of costing a separate associations call per contact. Then each meeting with `op="get"` and `properties=["hs_createdate", "hs_timestamp", "hs_meeting_title"]`. Always use internal property names. A meeting counts when `hs_createdate` falls after the campaign's creation date.
- **The reply text**: each reply classified from its body alone as `meeting_booked` (a slot booked or a time confirmed), `meeting_interest` (asks for a slot) or `none`.
- **The Lemlist marker**: the meeting-booked activity for that lead.

`meetings_booked` is the number of distinct lead emails with at least one of the three; `meeting_interest` counts distinct emails with interest and no booking. A lead who booked twice is one meeting. **Keep the three component counts on the row**: a week where the CRM shows several meetings and the sending tool's marker shows one is exactly what this dashboard exists to surface. A skipped signal is marked absent on the row and named in the digest.

## 4. Bucket, rate and flag
- **Buckets**: <tool:notion_get_blocks> on the page where your team keeps its campaign naming convention reads the patterns. Each campaign name is tested against them in written order. First match wins, and the catch-all `.*` stays last. A pattern has the shape `(?i)^<lane prefix>[ _-]` for **[lane name]**. The patterns live on that page, not in the run, so a new lane is a new line there, and a campaign named off-convention lands in the catch-all where everyone can see it.
- **Rates** over sent, from the lookback counters: open, reply, interest and meeting. **No rate on a denominator under ten**: give the raw counts instead. Roll up per bucket and overall: campaigns, sent, replied, meetings booked and the same rates.
- **A/B rows** only where every variant of a campaign has at least 20 leads, or, for a native A/B test, at least 20 sent per side. One thin variant removes the whole campaign from the A/B rows: a comparison with one thin side is noise presented as a result.
- **Flags**, and nothing else. From the weekly-window counters against last week's `weekly` rows: bounces, both absolute and as a share of sends (the one number that damages a sending domain); a drop in sends on a campaign that was running; no sends at all on a campaign that is still running. From the snapshot comparison: a status change to paused or ended; a queue running dry on a campaign still running; a campaign in error. A reply or a meeting in the weekly window is always mentioned, even a single one.
- **State the observation, never a supposed cause.** "No sends on <campaign> last week" is said; "the mailbox must have been suspended" is not. If the cause matters and nobody knows it, ask it as an open question.

## 5. Write the week's rows
- <tool:data_write> — one row per campaign keyed `row_key` = `<week>::<snapshot>::<workspace>::<campaign id>`, so a rerun updates instead of duplicating. It carries the week, snapshot, `row_type` (`campaign`), workspace, campaign id **and name and status** (ids alone mean nothing six months later), bucket, creation date, lookback, the lookback counts and rates, the weekly-window counts the flags came from, `signals_used`, the run id, an empty `posted_at` and `page_url`. Qualifying variants get one row each, with `::<variant>` appended to the key and `row_type` set to `variant`.
- Rows are written **before** the sheet, the page and the post. A failure downstream leaves the week counted, and the replay posts without recounting.
- <tool:sheets_spreadsheet> — only when the operator names a spreadsheet. `op="metadata"` first to confirm the tabs exist, then for each tab (overall, one per bucket, A/B, raw activities) `op="clear"` on its data range and `op="write"` with an explicit A1 range and `append=false`. A write only overwrites the cells it covers, so without the clear a week with fewer rows leaves last run's tail under the new one. Leave filter tabs to whoever keeps the sheet; the table stays the source of truth.

## 6. Append the week to one Notion page
Weekly runs only; an ad-hoc run skips this step.
- The integration must be invited to the parent page first. Otherwise the search finds nothing and the run creates a duplicate.
- <tool:notion_search> — `query="<dashboard page title>"`, `filter_type="page"`. Keep the result whose parent is your reporting parent page.
- <tool:notion_create_page> — `parent_type="page"`, on the first run only, when the search found nothing. **One page, found by title, never a second.**
- <tool:notion_append_blocks> — a heading `Week <YYYY-Www>` and a paragraph: lookback, active campaigns, sent, reply rate, meetings booked with their three components, the two buckets that moved most, flags, and the sheet link when one was written. Append rather than rewrite, so the page reads as a dated log. Write the page URL into `page_url` on the week's rows.

## 7. Post the digest, then stamp it
- Deltas against last week's `weekly` rows cover active campaigns, sent, reply rate and meetings booked, in absolute numbers for counts and points for rates. With no previous rows, say "first snapshot, no deltas".
- <tool:slack_post_message> — weekly runs only: one post under about 900 characters as displayed, with the details as replies under it through `thread_ts`: one line per campaign with its bucket and numbers, the A/B rows, the flags, campaigns in error, event types met but not counted, and any workspace or signal missing this run. The app must be a member of the channel: `not_in_channel` is a failed post, not a quiet week. Very long text is split into threaded parts (`split_into` and `ts_all` in the response) rather than truncated, so never re-post a message you think got cut. A message can be deleted, not edited.
- <tool:data_write> — **immediately after the post**, `posted_at` and the post's `ts` on the week's `weekly` rows. A post whose stamp never gets written is the worst of both worlds: the channel has read it, and the next run believes it has not.
- **An ad-hoc run** gives the same headline, buckets and flags back in the conversation, with no deltas (its lookback is not last week's), and writes no stamp.
- If the channel is read outside your team (a channel shared with a client): no tool jargon or campaign ids, nothing that reads as a reproach (an empty queue is a fact about the tool, not about how they use it), and no business figure that does not come from the sending tool.

```text
*Outbound dashboard · week <ww> · last <n> days · <workspaces>*
<n> active campaigns (±<n>) · <n> sent (±<n>) · reply rate <x>% (±<x> pt) · <n> meetings booked (±<n>: CRM <n>, replies <n>, marker <n>)
Buckets: <bucket> <n> campaigns, <n> meetings · <bucket> <n>, <n> · …
Needs attention: <flags, or nothing>
A/B: <n> campaigns qualify, rows in thread · Notion: <link>
```

## What this never does
- Never writes to the sending tool or the CRM: no pause, no relaunch, no refill of an empty queue, no unsubscribe. It looks and reports; acting stays a human gesture, taken with Monday's view in hand.
- Never answers a reply. That belongs to your reply-handling process and its own review.
- Never posts twice for the same week, never posts for a week with nothing sent, and never posts from an ad-hoc run.

## Output
The run closes with campaigns read per workspace, sent over the lookback and over the weekly window, replies, meetings by signal, A/B rows written, flags raised, which sinks were written or skipped (sheet, page, post), and any workspace key or signal that was missing.