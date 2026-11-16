## prerequisite — a google service account, user of the Search Console property

access goes through a Google **service account**, not your Google account: it reads Search Console without anyone connecting their account, and is cut off by removing it from the property. an administrator sets it up once for the whole org (org instance); every agent in the org then reads the same properties.

- in a Google Cloud project, **enable** "Google Search Console API" (APIs & Services > Library)
- create a service account, then a **JSON key** (IAM & Admin > Service accounts > the account > Keys > Add key > JSON): the whole file is what you paste into `service_account_key`, exactly as downloaded
- in Search Console, Settings > Users and permissions > Add user: the **service account's email** (`client_email` of the key), permission **Restricted** (enough to read), for each property the agents should see; **Owner** or **Full** for a property where sitemaps are to be submitted
- "test the connection" lists the visible properties: zero properties is a failure, not a green — it is almost always the missing Search Console step

## setup — access level: read by default, full to submit a sitemap

the org chooses what the key may do, with `oto_connector_access_level(op='set', scope='org', org_id=…, name='google_search_console', level=…)` (org admin; a team can only narrow it):

- **`read`**, the default: the five reads below, nothing is ever submitted. The key asks Google for `webmasters.readonly` only. A key installed before the levels existed stays here until an admin chooses otherwise.
- **`full`**: adds `google_search_console_act(op="add_sitemap")`, which submits a sitemap so Google crawls it. The key then asks for `webmasters`, at its next call: nothing to reconnect.

the level bounds, it does not grant: submitting also needs the service account to be **Owner** or **Full** on the property in Search Console. Two refusals say which gesture is missing:

- `access_level_denied`: the org's level is `read` — an org admin sets `full`
- `owner_or_full_required`: the level is `full` but the property shares the key as Restricted — give its `client_email` the Owner or Full permission (Settings > Users and permissions)

nothing removes a sitemap or a property.

## usage — read a property's search performance

- "which properties can we read?" → `google_search_console(op="list_sites")`: each `siteUrl` is the name every other op takes
- "top queries of October" → `google_search_console(op="query_search_analytics", site_url="sc-domain:example.com", start_date="2026-10-01", end_date="2026-10-31", dimensions=["query"], row_limit=100)`
- "is this page indexed?" → `google_search_console(op="inspect_url", site_url="sc-domain:example.com", inspection_url="https://www.example.com/pricing")`
- "which sitemaps are submitted?" → `google_search_console(op="list_sitemaps", site_url=…)`, one sitemap with `op="get_sitemap"` and its `feedpath`
- "submit the new sitemap" (level `full`) → `google_search_console_act(op="add_sitemap", site_url="sc-domain:example.com", feedpath="https://www.example.com/sitemap.xml")`, then `op="get_sitemap"` later for its processing

## note — read a Search Console figure without getting it wrong

- a Domain property is named `sc-domain:example.com`, a URL-prefix property by its full URL **with its trailing slash**
- data lags about two days; `data_state="all"` adds the fresh, incomplete days
- `query_search_analytics` returns at most `row_limit` rows (up to 25,000): for more, call again with `start_row` = previous `start_row` + `row_limit`, until fewer rows come back
- URL inspection is bounded to 2,000 a day per property: inspect the pages that matter, not a whole sitemap
- a "not a user of this property" refusal: add the key's `client_email` to that property
