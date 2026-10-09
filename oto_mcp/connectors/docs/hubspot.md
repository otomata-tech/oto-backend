## prerequisite — your hubspot token (private app)

hubspot authenticates via a **private app token**. in your [hubspot account settings](https://app.hubspot.com), go to **integrations → private apps**, create a private app and tick the scopes (names as hubspot's [scope list](https://developers.hubspot.com/docs/apps/legacy-apps/authentication/scopes) spells them — each comes as `.read` and `.write`):
- `crm.objects.contacts.read` + `.write`, `crm.objects.companies.read` + `.write`, `crm.objects.deals.read` + `.write` — the crm itself (deal pipelines and stages come with `crm.objects.deals.read`)
- `crm.objects.tickets.read` + `.write` — tickets. **and** `tickets` if your scope list still shows it: ticket *pipelines* and ticket *properties* still ask for that older scope
- `crm.schemas.contacts.read`, `crm.schemas.companies.read`, `crm.schemas.deals.read` (+ `crm.schemas.tickets.read` if offered) — to discover an object's properties
- `crm.lists.read` + `crm.lists.write` — for **lists (= segments)**
- `crm.objects.owners.read` — to list owners

there is no `crm.objects.tickets` scope (without `.read`/`.write`): if a guide told you to tick it, tick `crm.objects.tickets.read` instead.

⚠️ if your private app predates the lists tools, it only has the `crm.objects.*` scopes and `hubspot_list` will answer **403**. that does not mean your key is wrong: reopen the private app and add the scope, the token does not change.

**test the connection** tells you more than "it authenticates": it reads, family by family, which scopes the token holds (`connected, tickets scope missing`). a missing family does not turn the connection red — the rest keeps working.

⚠️ the expected token is the private app's **access token** (form `pat-<region>-…`). if you paste something else — typically an oauth *refresh token*, a long base64 blob starting with `Ci…` — hubspot answers **401 `EXPIRED_AUTHENTICATION`** with a misleading message along the lines of "expired 20697 day(s) ago" and an expiry date of january 1st, 1970. nothing expired: a refresh token has no expiry field, so sent as a bearer it is read as expired since the epoch. that message means "this is not an access token", not "your token got old".

- copy the generated **access token**
- paste it into oto on your account (`/account`), connector **hubspot**
- byo only: your key or your org's shared one, no platform key

### troubleshooting — 403 `MISSING_SCOPES` on one object type

contacts and deals answer, but tickets (or lists, or pipelines) answer **403 `MISSING_SCOPES`**, with hubspot's message "the scope needed for this api call isn't available for public use". that message is misleading: the scope exists, it is just not ticked on your private app. the refusal oto returns names the scope to add — tick it in the private app's **scopes** tab and save. the token does not change, nothing to update in oto.

## usage — what you can do

query and edit your hubspot crm (contacts, companies, deals, tickets) from claude.
- "find the contacts at acme" → `hubspot_object` (op `search`, object_type `contacts`)
- "create a 10k€ deal" → `hubspot_object` (op `create`, object_type `deals`)
- "the deals associated with this contact" → `hubspot_object` (op `associations`)
- "add a note on this contact" → `hubspot_object` (op `add_note`)

### segments = lists

in hubspot, a "segment" is a **list**: there is no separate segments api. `hubspot_list` covers them.
- "which contact lists do i have?" → `hubspot_list` (op `search`, object_type `contacts`)
- "create a list "ICP France" and put these 40 contacts in it" → op `create` then op `add_members`
- "who is in this list?" → op `members`
- "which lists is this contact in?" → op `record_lists`

three list types, chosen at creation and **not changeable afterwards**:
- **MANUAL** — you decide who is in it (`add_members` / `remove_members`)
- **DYNAMIC** — hubspot recomputes the members from criteria; the membership ops are **refused** on it, you change the criteria (op `update`, `filter_branch`)
- **SNAPSHOT** — filtered once at creation, managed by hand afterwards

emptying a list (`clear_members`) and deleting it (`delete`) accept `dry_run=true`: it tells you what would go without touching anything. a deleted list stays restorable for 90 days (op `restore`).

### properties — read before writing

hubspot internal names are not the interface labels (`dealstage`, not "Deal Stage"), and a dropdown only accepts its declared values. `hubspot_property` (op `list`) gives the real schema of an object type — on a fresh portal, that is already 404 properties on contacts, 45 of them enumerations. this is what makes the `create`/`update` of `hubspot_object` reliable **and** the `filter_branch` criteria of a dynamic list, which reference properties by internal name.

`op list` returns one compact row per property (name, label, type, field type, group, options) and leaves out hubspot's hidden internal ones — `include_hidden=true` keeps them, `verbose=true` gives the full cards. a deal's stages belong to a *pipeline*, not to the `dealstage` property: their options are filled from the pipelines, grouped by pipeline.
- "which custom deal properties do we have?" → `hubspot_property` (op `list`, object_type `deals`, `custom_only=true`)

### pipelines and stages

- "what are the stages of our sales pipeline?" → `hubspot_pipeline` (op `list`, object_type `deals`) — stage ids, labels, order, win probability, closed or not; archived stages are kept and flagged
- "our open deals, with their stage names" → `hubspot_object` (op `search`, object_type `deals`, `resolve_labels=true`) — adds `dealstage_label` and `pipeline_label` next to the raw ids, which stay (a stage is written by its id)
- tickets work the same way (`hs_pipeline_stage`, `ticketState` open or closed)
