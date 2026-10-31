## prerequisite — apify api token

get the API token from [Apify](https://console.apify.com/settings/integrations) (it starts with `apify_api_`), then paste it into oto.
- byo-only: runs are billed to the org's Apify account

## usage — the common actors, one tool each

reach for these first: a name that says what it does, typed arguments, the actor's input built for you, `max_items` and a $5 ceiling (`max_total_charge_usd`, raise it as needed) set by default.
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
- `apify_run_sync` waits at most 300 s; a run still going then is not stopped — the error names its run id, continue with `apify_run_status` and `apify_dataset_items` (or start a known-long scrape with `apify_run`)
- an actor's items are often very wide: `fields` / `omit` avoid bringing back huge objects
