## prerequisite — a google service account, user of the Search Console property

access goes through a Google **service account**, not your Google account: an administrator sets it up once for the whole org (org instance), and it is cut off by removing it from the property.

- in a Google Cloud project, **enable** the "Google Search Console API"
- create a service account, then Keys → Add key → JSON, and paste the whole file into oto
- in Search Console, Settings → Users and permissions → Add user: the **service account's email** (`…@….iam.gserviceaccount.com`). **Restricted** is enough to read; adding a sitemap needs **Full**
- "test the connection" lists the visible properties: zero properties is a failure, not a green — it is almost always the missing Search Console step

## usage — how a site does on Google

- "which sites can we read?" → `search_console()`
- "top queries of the last 28 days" → `search_console(op="performance", site="sc-domain:example.com", dimensions=["query"])`
- "how does this page rank, and on what?" → `search_console(op="performance", site=…, dimensions=["query"], filters={"page": "https://example.com/pricing"})`
- "pages close to page one" → `search_console(op="performance", site=…, dimensions=["page"], row_limit=1000)`, then keep positions 8 to 15
- "is this page indexed?" → `search_console(op="inspect", site=…, url="https://example.com/new-page")`
- "which of these new pages are not indexed yet?" → `search_console(op="inspect", site=…, urls=[…])`, 20 at most per call
- "was the sitemap read?" → `search_console(op="sitemaps", site=…)`
- "resubmit the sitemap" → `search_console_act(op="add_sitemap", site=…, sitemap="https://example.com/sitemap.xml")`

## note — read a Search Console figure without getting it wrong

- a property is `sc-domain:example.com` (a whole domain) or a URL prefix with its trailing slash (`https://example.com/`): take it from `search_console()`, it must match
- without dates, the window is the 28 days ending 2 days ago: the last days are incomplete
- `verdict` PASS means the URL is on Google; `coverage` is the reason Search Console shows, in its own words
- `inspect` only reports. **Nothing here asks Google to index a page**: the "Request indexing" button has no API, and Google's Indexing API only takes job and livestream pages
- inspection quota: 2,000 URLs a day per property; `failed` in a batch receipt lists what was not read
