## prerequisite — sign in with Google and authorize Search Console

from this card, click **connect**: Google asks you to authorize **Search Console read access only** (scope `webmasters.readonly`) on the account you choose. the Google account itself (address, token) is carried by the **Google account** connector — one account can authorize several services, one service at a time, without re-authorizing the others.
- pick the Google account that is a user of your Search Console properties: the tool sees exactly the properties **that Google user** can open, at the permission level Search Console gives it (Owner, Full, Restricted)
- **read-only by default**: performance, URL inspection and sitemaps are read; nothing is submitted, added or removed. to submit sitemaps, the org sets the `full` access level (below), or uses the **Google Search Console** card (service account)
- no key, no service account to add to a property: the access is your own, and is cut off by removing the app from your Google account (myaccount.google.com > Security > Third-party connections) or by disconnecting the card
- several Google accounts: the tool acts on the one the call names with `_account=<email>` (the tool's `account=<email>` is the same choice — two different addresses are refused), otherwise the one the project pins, otherwise the default; `google_accounts` tells which ones have authorized Search Console

## setup — access level: read by default, full to submit a sitemap

the org chooses what the card may do, with `oto_connector_access_level(op='set', scope='org', org_id=…, name='search_console', level=…)` (org admin; a team can only narrow it):

- **`read`**, the default: the five reads below, nothing is ever submitted. The consent asks Google for `webmasters.readonly` only. An account connected before the levels existed stays here and keeps reading, without reconnecting.
- **`full`**: adds `search_console_act(op="add_sitemap")`, which submits a sitemap so Google crawls it. The consent then asks for `webmasters`: **re-authorize from this card** (click connect again) — a token granted at `read` cannot submit. `full` exists only where the instance allows it (instance setting `GOOGLE_SEARCH_CONSOLE_WRITE`); elsewhere setting it is refused (`access_level_off_on_instance`) and the card keeps asking read-only.

the level bounds, it does not grant: submitting also needs your Google user to be **Owner** or **Full** on the property in Search Console. Three refusals say which gesture is missing:

- `access_level_denied`: the level is `read` (an org admin sets `full`), or `full` is off on this instance
- `search_console_reauthorize_required`: the level is `full` but the account was authorized read-only — connect the card again
- `owner_or_full_required`: your Google user is Restricted on the property — an owner gives it Owner or Full (Settings > Users and permissions)

nothing removes a sitemap or a property.

## usage — read a property's search performance

`search_console(op=…)`: `list_sites`, `query_search_analytics`, `inspect_url`, `list_sitemaps`, `get_sitemap` — the same ops and arguments as the service-account connector `google_search_console`.
- "which properties can I read?" → `search_console(op="list_sites")`: each `siteUrl` is the `site_url` of the other ops
- "top queries of October" → `search_console(op="query_search_analytics", site_url="sc-domain:example.com", start_date="2026-10-01", end_date="2026-10-31", dimensions=["query"], row_limit=100)`
- "is this page indexed?" → `search_console(op="inspect_url", site_url="sc-domain:example.com", inspection_url="https://www.example.com/pricing")`
- "which sitemaps are submitted?" → `search_console(op="list_sitemaps", site_url=…)`
- "submit the new sitemap" (level `full`) → `search_console_act(op="add_sitemap", site_url="sc-domain:example.com", feedpath="https://www.example.com/sitemap.xml")`, then `search_console(op="get_sitemap")` later for its processing

## note — read a Search Console figure without getting it wrong

- a Domain property is named `sc-domain:example.com`, a URL-prefix property by its full URL **with its trailing slash**
- data lags about two days; `data_state="all"` adds the fresh, incomplete days
- `query_search_analytics` returns at most `row_limit` rows (up to 25,000): for more, call again with `start_row` = previous `start_row` + `row_limit`, until fewer rows come back
- URL inspection is bounded to 2,000 a day per property
