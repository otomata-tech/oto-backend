## prerequisite — your folk api key

folk provides a personal api key. get it from the [api/developer settings of your folk account](https://app.folk.app) (docs: [developer.folk.app](https://developer.folk.app)).
- paste it into oto on your account (`/account`), **folk** connector
- byo only: your key, or your org's shared credential — no platform key
- **groups** (and their custom fields) can be listed/created/updated via the api (`folk_group`) — deleting a group or a custom field remains reserved to the folk app; removing a **member** from a group, however, is done via the api (`folk_group(op="remove_member")`)

## usage — what you can do

manage your folk crm (people/contacts, companies, deals or any other custom object) + notes, interactions and tasks from claude. Everything goes through `folk_record` (parameter `entity`) — there is no separate `folk_company`/`folk_contact`/`folk_deal` tool.
- "find the contact dupont" → `folk_record(op="search")` (entity `person`), then `folk_record(op="get")` for the record
- "add jean dupont, cto at acme" → `folk_record(op="create")` (entity `person`)
- "log a call on this contact" → `folk_record(op="create")` (entity `interaction`, type/title/content)
  - ⚠️ `date_time` is **required** by folk even though it reads as optional; if omitted, it defaults to now. Until 27/08/2026 omitting it returned an opaque 422
  - ⚠️ folk only takes date-times in **UTC with a trailing `Z`** (`2026-11-13T09:00:00Z`) — interaction `date_time`/`dateTime` as well as a task's `completedAt` on `mark_done`; any other ISO 8601 form is a 422 `Invalid datetime`. A value with an offset (`+02:00`) is converted to UTC for you; one with no timezone is refused before the call
- "what did we say to each other with dupont?" → `folk_record(op="search", entity="interaction", entity_id="per_…")`: emails, calendar events, whatsapp messages and logged interactions that folk keeps on this record, then `op="get"` (with the same `entity_id`) for the full body of a single one
  - ⚠️ **correction**: this document long claimed that folk only told you *when* you had talked to someone, never *what* was said. That was false — it was true of the connector, which only exposed interaction creation, not of folk, whose read endpoints already existed (in open beta)
  - an interaction is not addressable on its own: `entity_id` (the person/company that carries it) is **mandatory** for search, read, update and delete
  - search returns `content: {subject, snippet}` — **not the body**. The full body (`body`) only comes from an `op="get"` on ONE interaction: scanning tells you what was discussed, reading what was said costs one `get` per interaction
  - **imported** interactions (email, calendar, whatsapp) are read-only: folk refuses `op="update"` and `op="delete"` on them, they belong to their source
  - `when="past"` by default (what has happened); `"upcoming"` for what is planned, `"all"` for both — so the default HIDES what is upcoming
  - `privacyLevel` hides the **body**, not the subject: on `subjectOnly`/`sensitive`/`internal`, the `get` still returns subject and snippet but `body` is simply absent. the key is byo → you see what ITS owner sees. a missing `body` is a permission result, not an empty interaction
- "what is still open on this contact?" → `folk_record(op="search", entity="task", entity_id="per_…", filters={"completedAt": {"empty": True}})`; to close: `folk_record(op="mark_done", entity="task", ids=[...])` (up to 50 at once — `entity` is required), `op="mark_todo"` to reopen
  - ⚠️ **reminders are deprecated — and they are the SAME records**: folk deprecated `/reminders` on 13/08/2026 (removal announced for February 2027) in favor of **tasks**. Verified live on 27/08/2026: one store, two views — `rmd_<uuid>` and `tsk_<uuid>` designate the same record, and swapping the prefix resolves in both directions (30 reminders = 30 tasks, same uuids). Nothing is orphaned: a reminder set before the switch can be read, filtered and closed like a task today. Write everything new as `entity="task"`, which does strictly more (markdown description, filters by due date/assignee/completion, completion tracking)
  - a task NEVER completes by itself in folk (unlike a reminder, which gets marked "triggered"): `completedAt` only changes on an explicit `mark_done`/`mark_todo` — and it is refused in an `op="update"`
  - ⚠️ no `task.*` webhook event in folk to date — only `reminder.*` exist
- "create a deal in group X" → `folk_record(op="create")` (entity `deal`), and `folk_record(op="search", entity="deal")` to list them — `object_type` is auto-discovered if omitted (see note below); only pass it explicitly if the group has SEVERAL custom objects beyond person/company (auto-discovery then raises an error that lists them)
- "add these 20 contacts" → `folk_record(op="create")` (entity `person`, `items=[...]`) in a single call
- "create a private Leads group" → `folk_group(op="create", name="Leads", visibility="private")`
- "add a Status field (select) on the people of group X" → `folk_group(op="create_custom_field")` (`entity_type="person"` by default, `custom_field={"type": "singleSelect", "name": "Status", "options": [...]}`)
- ⚠️ only `person`/`company` are FIXED entity_types. Any object beyond those (deal or other) is a **custom object that each folk customer names themselves** ("Deals" is just the name chosen by THIS workspace — another could call it "Opportunities", in the singular, etc.). For `folk_group(op="custom_fields"/...)`, discover the name: call with any entity_type — Folk's 404 lists the REAL entity_types of this group (`"Available entity types are: ..."`), then call again with the right name. `folk_record`'s `object_type` does this discovery on its own (see note above) — only `folk_group` still requires doing it by hand
- "add jean as admin of the Leads group" → `folk_group(op="add_member")` (`user_id` from `folk_user(op="list")`, `role="admin"`)
- "who has access to the Leads group?" / "remove jean from the group" → `folk_group(op="members")` / `folk_group(op="remove_member")`
- ⚠️ a **public** group (`visibility="public"`) has IMPLICIT membership: `folk_group(op="members")` lists the WHOLE workspace there with the "admin" role, whether or not anyone was added (verified live) — `add_member`/`remove_member`/`update_member` only have a real effect on a **private** group (explicit membership)
- "notify my endpoint on every new deal in group X" → `folk_webhook(op="create")` (before that: `folk_group(op="list")` for the group id, `folk_group(op="custom_fields")` if the filter is on a custom field)
- "list my webhooks" / "disable this webhook" → `folk_webhook(op="list")` / `folk_webhook(op="update")` (`fields={"status": "inactive"}`)
- ⚠️ a webhook filter set via the api exists ONLY there: editing it from the folk app settings makes it silently disappear
