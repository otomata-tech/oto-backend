## setup — the one-click connection ("connect hubspot")

when the instance publishes its hubspot apps, the card shows **connect hubspot**: you pick the portal, approve, and you are connected — no key to create, no scope to tick. **pick the tab first**: it decides which kind of connection you make.

- **personal tab** — connects **your own** hubspot user. every call runs **as you**, with your hubspot permissions (hubspot's audit log shows you, "via" the app), and the contacts, companies, deals and tickets your agents **create** get **you as owner** unless the call names another owner. any member can do it, if their hubspot permissions cover contacts, companies, deals, owners and properties (read); lists, tickets, custom objects and page views are asked as optional
- **org / team tab** — an org admin (or team lead) connects the portal **once for everyone** there; calls run as the app, owners are whatever the call says. also asks, as optional, to create and edit properties (fields) and custom objects
- a member with a personal connection uses it; the others use the org's or team's
- a personal connection cannot do what hubspot keeps from user-level apps (bulk association operations among them): use the org connection for those
- reconnecting the same level replaces the connection; pasting a key over it replaces it too
- "test the connection" reads the portal, who connected it, the granted scopes and — on a personal connection — the owner records default to, without using up anything
- if the app is uninstalled, the next call says **reconnect hubspot** and the card turns red
- removing a one-click connection also uninstalls the app from the portal — unless another connection here still uses the same app in that portal (hubspot uninstalls for the whole portal, personal app included, and emails its super admins). the connection is removed either way, even if hubspot refuses or is unreachable
- each hubspot app installs on at most 25 portals until it is listed on the hubspot marketplace

no "connect hubspot" button on a tab: the instance has no hubspot app for that kind of connection yet — the service key below works the same.

## prerequisite — your hubspot token (service key)

hubspot authenticates via a **service key**. ⚠️ hubspot stops letting you create *private apps* (on 28/09/2026 for new accounts, 26/10/2026 for existing ones — [changelog](https://developers.hubspot.com/changelog/legacy-private-app-creation-sunset)); a private app made before keeps working, and its token is pasted the same way.

in [hubspot](https://app.hubspot.com), open **development → keys → service keys** (on some accounts: **settings → integrations → service keys**), click **create service key** (super admin or developer tools access needed), name it and tick the scopes (names as hubspot's [scope list](https://developers.hubspot.com/docs/apps/legacy-apps/authentication/scopes) spells them — each comes as `.read` and `.write`):
- `crm.objects.contacts.read` + `.write`, `crm.objects.companies.read` + `.write`, `crm.objects.deals.read` + `.write` — the crm itself (deal pipelines and stages come with `crm.objects.deals.read`)
- `crm.objects.tickets.read` + `.write` — tickets. **and** `tickets` if your scope list still shows it: ticket *pipelines* and ticket *properties* still ask for that older scope
- `crm.schemas.contacts.read`, `crm.schemas.companies.read`, `crm.schemas.deals.read` (+ `crm.schemas.tickets.read` if offered) — to discover an object's properties
- `crm.lists.read` + `crm.lists.write` — for **lists (= segments)**
- `crm.objects.owners.read` — to list owners
- `business-intelligence` — optional, only for the **pages a contact read** (`hubspot_visits`, op `page_views`). the connection test does not ask for it. ⚠️ it may not be enough: see *website visitors* below

there is no `crm.objects.tickets` scope (without `.read`/`.write`): if a guide told you to tick it, tick `crm.objects.tickets.read` instead.

⚠️ if your key (or private app) predates the lists tools, it only has the `crm.objects.*` scopes and `hubspot_list` will answer **403**. that does not mean your key is wrong: reopen it in hubspot and add the scope. adding a scope does not change a private app's token; if you rotate the key in hubspot, its new value replaces the old one: paste it again in oto (connector **hubspot**).

**test the connection** tells you more than "it authenticates": it reads, family by family, which scopes the token holds (`connected, tickets scope missing`). a missing family does not turn the connection red — the rest keeps working. it also names the portal the key reads (`identity`: portal id, region, account type).

⚠️ the expected token is the service key's value, or a private app's **access token** (both of the form `pat-<region>-…`). if you paste something else — typically an oauth *refresh token*, a long base64 blob starting with `Ci…` — hubspot answers **401 `EXPIRED_AUTHENTICATION`** with a misleading message along the lines of "expired 20697 day(s) ago" and an expiry date of january 1st, 1970. nothing expired: a refresh token has no expiry field, so sent as a bearer it is read as expired since the epoch. that message means "this is not an access token", not "your token got old".

- **show** ▸ **copy** the key's value
- paste it into oto on your account (`/account`), connector **hubspot**
- byo only: your key or your org's shared one, no platform key

### troubleshooting — 403 `MISSING_SCOPES` on one object type

contacts and deals answer, but tickets (or lists, or pipelines) answer **403 `MISSING_SCOPES`**, with hubspot's message "the scope needed for this api call isn't available for public use". that message is misleading: the scope exists, it is just not ticked on your key. the refusal oto returns names the scope to add — tick it on the service key (or in the private app's **scopes** tab) and save. nothing to fix in the call.

### troubleshooting — 400 `PROPERTY_DOESNT_EXIST` on a write

a create or update names a property your portal does not have (e.g. `linkedin_url` on a company, where hubspot's own property is `linkedin_company_page`). hubspot refuses the whole call, nothing is written. the refusal oto returns names the unknown properties: read the real internal names with `hubspot_property` (op `list`) and fix the procedure, or create the property (op `create`) if it should exist. in `hubspot_push_rows`, the batch stops at the first row (`stopped: hubspot_property_doesnt_exist`, `unknown_properties`) instead of failing every row the same way.

## usage — what you can do

query and edit your hubspot crm (contacts, companies, deals, tickets) from claude.
- "find the contacts at acme" → `hubspot_object` (op `search`, object_type `contacts`)
- "create a 10k€ deal" → `hubspot_object` (op `create`, object_type `deals`)
- "the deals associated with this contact" → `hubspot_object` (op `associations`)
- "add a note on this contact" → `hubspot_object` (op `add_note`)

### merging duplicates — irreversible

- "merge these two acme companies into this one" → `hubspot_object` (op `merge`, object_type `companies`, `object_id` = the record that survives, `merge_ids` = the duplicates)

⚠️ a hubspot merge **cannot be undone**. always run it first with `dry_run=true`: it reads the survivor and each duplicate (name and domain, or email; how many contacts and deals each one carries) and tells you what would happen, without writing anything.

- the survivor **keeps its own values**; only its empty properties are filled from a duplicate. its associations (contacts, deals…) gather on it
- companies, contacts, deals and tickets only; at most **10 duplicates per call**
- each duplicate gets its own result — `merged`, `already_merged` (its id already leads to another record: hubspot keeps answering a merged id with its survivor), `not_found`, or `error` with hubspot's status and message — and one failure never stops the others
- hubspot can give the survivor a **new id**: read `survivor_id` in the answer, not the id you passed
- a recipe can never merge, like it can never delete

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

### website visitors

`hubspot_visits` reads who came to your site, latest first. three sources, and they do not measure the same thing:
- "which companies visited this week?" → op `companies` — the companies hubspot identified from their network (**buyer intent**): last visit time, visitors and page views over 30 days, recent intent signals
- "which contacts came back to the site?" → op `contacts` — people hubspot already knows (a form, an email click): last session, last page, first page, session count. anonymous visitors are not contacts and are not here
- "what did this contact read?" → op `page_views` with the contact's id — each page with its time

what hubspot needs on its side, per its own documentation (we have not yet read a portal with tracking on):
- **the tracking code** installed on the site
- for op `companies`: companies **tracked in buyer intent**. hubspot only writes the visit on the companies it tracks — in buyer intent, a saved view with **add new companies** and **tracking intent signals** turned on does it for every company entering the view from then on. tracking is paid in **hubspot credits**: 10 per company and per month, against the credits a plan includes (500 a month on starter, 3,000 on professional, 5,000 on enterprise) — hubspot's figures as of october 2026, check its catalog
- for op `page_views`: hubspot's reference asks for the `business-intelligence` scope and a **professional or enterprise** plan. ⚠️ on a portal read in october 2026, a private app holding that scope was still refused — hubspot asked for `event-detail-read`, which is in no published scope list. until a private app is seen passing, treat a contact's page views as unavailable; the first page, last page and last session (op `contacts`) always work

what hubspot does **not** expose: the pages a *company* read. the buyer intent card shows them in hubspot; the api only carries the two 30-day counts. on a company, page paths need a visitor-identification tool that writes them.

an empty op `companies` answer says why: its `diagnosis` counts the tracked companies — zero means tracking was never turned on, not that nobody came.

to run it on a schedule ("new visitors since the last run"), pass `since` = the last run's time minus a few minutes, and deduplicate on the record id: hubspot's search index lags a few moments behind a write. a segment works too — a list filtered on **recent intent signals**, read with `hubspot_list` (op `members`, with `properties`), whose rows carry the date each company entered it.

### searching: order and pages

- `hubspot_object` (op `search`) takes `sorts` — one rule, e.g. `[{"propertyName": "createdate", "direction": "DESCENDING"}]`. a search serves at most 10,000 results: narrow the filters rather than paging further
- `hubspot_list` (op `search`) serves 20 lists a page: pass the answer's `offset` back while it says `hasMore`. `processing_types` keeps only MANUAL, DYNAMIC or SNAPSHOT lists
