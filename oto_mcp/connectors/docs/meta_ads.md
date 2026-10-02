## prerequisite — a Meta business portfolio and an ad account

oto reads the ad accounts **you choose** when you authorize it on Facebook.

- you need a Facebook account with access to a **business portfolio** (Meta Business Suite) that owns or manages the ad accounts
- during the authorization, Facebook asks which business and which **ad accounts** to share — only those are visible to oto. To add one later, reconnect and tick it
- with the recommended setup (system-user token), the connection does **not expire**: it lasts until you remove it (Meta Business Suite → settings → integrations → connected apps) or lose access to the ad account
- ⚠️ until our Meta app passes Meta's review, it runs with **standard access**: it works, but Meta limits the number of calls heavily. Big reports may hit the limit — wait a few minutes and retry

## setup — the callback URL to declare at Meta

operator only. You need a **Meta app** of type *Business* with the **Marketing API** product and **Facebook Login for Business**. Create a login *configuration* with:

- token type: **system-user access token** (never expires)
- assets: **ad accounts**
- permissions: `ads_read`, `business_management`

and declare this callback URL, byte for byte:

{{callback:/api/meta_ads/oauth/callback}}

then set the three app values at platform scope, once per instance:

- `oto_admin_connector_setting(op="set", connector="meta_ads", key="app_id", value="…")`
- `oto_admin_connector_setting(op="set", connector="meta_ads", key="app_secret", value="…")`
- `oto_admin_connector_setting(op="set", connector="meta_ads", key="config_id", value="…")`

until they are set, the card stays visible and « connect » refuses **naming the missing key**. `app_secret` is a real secret: never returned by an API, never logged. Preprod and prod have different callback URLs — declare both.

## usage — accounts, campaigns, performance

read-only: nothing is created, edited, paused or spent.

- `meta_ads_accounts` first: it lists the ad accounts granted, with currency and timezone — amounts are in the **account currency**
- `meta_ads_objects` browses campaigns, ad sets and ads of an account (filter with `effective_status=["ACTIVE"]`), or reads one object by id
- `meta_ads_insights` returns performance: spend, impressions, reach, clicks, CPC, CTR, conversions (`actions`). Use `level` to get one row per campaign/ad set/ad, `time_increment=1` for daily rows, `breakdowns` for age, gender, country or placement
- for long ranges or ad-level reports over a whole account, use `mode="async"`: if the report is not done in ~25 s, call again with the returned `report_run_id`

## note — what these numbers are

- figures are Meta's, with Meta's attribution: by default 7-day click and 1-day view. Pass `action_attribution_windows` to change it
- `reach` is deduplicated: it cannot be summed across days or campaigns
- recent days can still move for up to 28 days (late conversions)
