## prerequisite — your amplitude project's key pair

in Amplitude: **Settings → Organization settings → Projects → (your project) → General**. Copy **both** the API key and the secret key into oto, and pick the project's region.
- ⚠️ **the API key alone reads nothing.** It is the public key from the SDK snippet; every read needs the secret key too. A missing or wrong secret answers `403 Invalid API/Secret Key combination`
- **region**: `us` (default, amplitude.com) or `eu` (analytics.eu.amplitude.com) — two separate deployments. A key used on the wrong one answers `403 Invalid API Key`; the "test connection" button names which of the two is wrong
- **one key pair = one project.** Several projects = several connections
- byo-only: no shared oto key, it is your product data
- the "test connection" button lists the project's event types (`GET /api/2/taxonomy/event`): nothing is written

## usage — events, charts, queries, users, cohorts

five tools, all read only:
- "what do we track?" → `amplitude_schema()` — read it **before** a query; `op="event_properties", event_type="Sign Up"`, `op="user_properties"` (custom ones are `gp:…`), `op="volumes"` for what actually fires
- "our activation funnel, from the dashboard" → `amplitude_chart(chart_id="…")` — the id is the last segment of the chart URL. The number is the team's own
- "sign-ups per week in September, by country" → `amplitude_query(op="segmentation", event="Sign Up", start="2026-09-01", end="2026-09-30", interval="week", group_by="country")`
- "sign-up → purchase conversion within 7 days" → `amplitude_query(op="funnel", events=["Sign Up", "Purchase"], start=…, end=…, conversion_window_days=7)`
- "do new users come back?" → `amplitude_query(op="retention", event="Sign Up", start=…, end=…)` (return event defaults to any active event)
- "weekly active users" → `amplitude_query(op="active_users", start=…, end=…, interval="week")`
- "what did this user do?" → `amplitude_user(user="alice@acme.com")` then `amplitude_user(op="activity", amplitude_id=…)`
- "who is in our power-users cohort?" → `amplitude_cohort()` then `amplitude_cohort(op="request", cohort_id=…)`; if still running, `op="members", request_id=…` a minute later

## note — run the saved chart, don't rebuild it

conversion window, step order, exclusions and attribution do not survive being re-specified by hand: a rebuilt funnel returns a **plausible** number that disagrees with the one your team reads in Amplitude, and nothing signals it. When the chart exists, `amplitude_chart`; `amplitude_query` is for questions nobody has charted yet.

## note — limits

- Amplitude bills queries against a **cost budget per project** (108,000 per hour, cost = days × conditions × query type) and 5 concurrent queries. Windows over 92 days need `long_range=True`, 366 days is the cap, 5 segments at most
- user search and activity share **360 calls per hour**
- cohorts are a Growth / Enterprise add-on, 500 downloads a month; members are returned as a bounded preview (first 100 lines with the total)
- a misspelt event name returns **zeros, not an error** — check `amplitude_schema` first

## note — scope, and the official MCP

read only. Event ingestion, taxonomy edits, cohort upload, dashboards, experiments and feature flags are not served. For chatting with Amplitude directly in an AI client, Amplitude's own MCP server (`mcp.amplitude.com/mcp`, OAuth) covers more of the product; this connector is what procedures, agents and runs can use.
