## prerequisite — apify api token

get the API token from [Apify](https://console.apify.com/settings/integrations) (it starts with `apify_api_`), then paste it into oto.
- byo-only: runs are billed to the org's Apify account

## usage — the common actors, one tool each

reach for these first: a name that says what it does, typed arguments, the actor's input built for you, `max_items` and a $5 ceiling (`max_total_charge_usd`) set by default.
- "full LinkedIn profiles of these people" → `apify_linkedin_profiles(urls)` (`with_email=true` adds an email search)
- "who works at this company" → `apify_linkedin_company_employees(companies, job_titles, locations)` — `profile_mode="short"` to pick people, enrich only those you keep
- "the LinkedIn page of this company" / "find its LinkedIn URL from its name" → `apify_linkedin_companies(urls | names)` — check `website` against the domain
- "posts about X" / "what does this person post" → `apify_linkedin_posts(queries | author_urls)`
- "who is hiring" → `apify_linkedin_jobs`, `apify_indeed_jobs`, `apify_welcome_to_the_jungle_jobs`
- "emails, phones, LinkedIn link of these websites" → `apify_website_contacts(urls)`
- "read this whole site" → `apify_website_crawl(urls, max_pages)` (one page: `web_read`)
- "businesses on Google Maps in this area" → `apify_google_maps_places(search_terms, location)`; their reviews → `apify_google_maps_reviews(place_ids | place_urls)`

## usage — any other actor of the Store

apify is a catalog of ready-to-use scrapers — Google Maps, LinkedIn, Instagram, Amazon, Booking, TikTok… — that you launch with a JSON input and whose output you read.
- "scrape the Google Maps reviews of bakeries in Marseille" → `apify_store_search("google maps")` to find the actor, then `apify_run_sync` with its input
- "which actor for this site?" → `apify_store_search` (also returns each actor's price and popularity)
- "what options does this actor have?" → `apify_actor` (default memory and timeout; the INPUT fields are documented on its Store page)
- long run (> 5 min) → `apify_run`, then `apify_run_status` until `SUCCEEDED`, finally `apify_dataset_items(defaultDatasetId)`
- "stop that" → `apify_abort_run` (stops billing)

## note — identifier, input and cost

- an actor is written `username/actor-name` (what the Store shows) or by its id — both work, the conversion to the URL form is done for you
- `run_input` is **specific to each actor**: its fields are not invented, they are read on the Store page (e.g. `{"searchStringsArray": [...], "maxCrawledPlaces": 20}` for the Google Maps scraper)
- a run is billed by usage: setting `max_items` (and if needed `max_total_charge_usd`, `timeout_secs`) at LAUNCH is the only protection — afterwards it is consumed
- `apify_run_sync` / `apify_run` without `max_total_charge_usd`: the ceiling is derived from `max_items` and the actor's event prices, or $5 when it cannot be — `max_items` alone does not stop an actor billed per event; a run that reaches its ceiling ends with a partial output
- every run, whichever tool launches it, has a ceiling of at most your organization's ceiling per run ($25 unless its admins set another): a `max_total_charge_usd` above it is refused (`max_charge_above_cap`) before anything is launched — split the work into smaller runs. On the platform's Apify key that ceiling never exceeds the platform's maximum; on a key another organization lends you, never that organization's ceiling either (the stricter of the two: it pays the run); on any organization's or person's key, never the platform's safety bound ($1,000). The refusal names the bound that applies
- an org admin sets the org's ceiling: `oto_org_settings(op="set", domain="apify_charge_cap", org_id=…, usd=…)` — at most the platform's maximum while the org has no Apify key of its own; `op="get"` (any member) reads it
- `apify_run_sync` waits at most 300 s; a run still going then is not stopped — the error names its run id, continue with `apify_run_status` and `apify_dataset_items` (or start a known-long scrape with `apify_run`)
- an actor's items are often very wide: `fields` / `omit` avoid bringing back huge objects
