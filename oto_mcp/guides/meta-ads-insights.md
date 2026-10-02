---
title: Meta Ads insights (meta_ads_insights)
description: levels, metrics, breakdowns, attribution and async reports — read before a non-trivial Meta Ads performance query
---

# Meta Ads insights

The `meta_ads_insights` docstring stays short; this guide holds what makes a query fail or mislead.

## The safe sequence

1. `meta_ads_accounts` → pick the `act_…` id. Note `currency` and `timezone_name`: spend is in the account currency, days are in the account timezone.
2. Optional: `meta_ads_objects(level="campaign", ad_account_id=…, effective_status=["ACTIVE"])` to get campaign names and ids.
3. `meta_ads_insights(object_id="act_…", level="campaign", date_preset="last_30d")` → one row per campaign.

## object_id vs level

- `object_id` is the **scope** (an account, a campaign, an ad set or an ad).
- `level` is the **row grain**. `object_id=act_…, level="ad"` = one row per ad of the whole account.
- Without `level`, you get one row for the object itself.

## Dates

- `date_preset`: `today`, `yesterday`, `last_3d`, `last_7d`, `last_14d`, `last_28d`, `last_30d`, `last_90d`, `this_week_mon_today`, `this_week_sun_today`, `last_week_mon_sun`, `last_week_sun_sat`, `this_month`, `last_month`, `this_quarter`, `last_quarter`, `this_year`, `last_year`, `maximum`, `data_maximum`.
- Or `since` + `until` (`YYYY-MM-DD`, both inclusive). Not both.
- `time_increment=1` → one row per day; an integer up to 90 → rows of that many days; `monthly`; `all_days` (default) → one total.
- Data for the last ~28 days can still change (late conversions).

## Metrics (fields)

Common: `spend`, `impressions`, `reach`, `frequency`, `clicks`, `inline_link_clicks`, `cpc`, `cpm`, `ctr`, `unique_clicks`, `actions`, `action_values`, `cost_per_action_type`, `purchase_roas`, `video_p25_watched_actions`… Add `campaign_name`, `adset_name`, `ad_name` to label rows.

- `actions` is a list of `{action_type, value}` — conversions live here (`purchase`, `lead`, `link_click`, `offsite_conversion.fb_pixel_purchase`, …). Read the `action_type`, do not assume.
- `reach` is **deduplicated**: never sum it across days, ads or breakdowns.

## Breakdowns

Valid groups (mixing outside a group is refused):

- demographics: `age`, `gender`, or `["age", "gender"]`
- geography: `country`, `region`
- placement: `publisher_platform`, `["publisher_platform", "platform_position"]`, `impression_device`, `device_platform`
- time of day: `hourly_stats_aggregated_by_advertiser_time_zone`

`reach` with breakdowns over long ranges is heavily rate-limited — prefer `impressions`.

## Attribution

Default: 7-day click + 1-day view, in both sync and async mode (oto pins it on async reports, where Meta's own default differs). Override with `action_attribution_windows`, e.g. `["1d_click"]` or `["7d_click", "1d_view"]`. Numbers then differ from Ads Manager if Ads Manager uses another setting — say which window you used.

## Big requests → async

Use `mode="async"` when the request is large (ad level over an account, daily rows over months, several breakdowns). The tool waits ~25 s:

- done → rows are returned with `report_run_id`;
- still running → `{report_run_id, status, percent_complete}`: call `meta_ads_insights(report_run_id=…)` again later;
- `report_run_id` expires after 30 days.

A sync request that fails with "too much data" means: narrow it, or switch to async.

## Errors

| Message says | Meaning | Do |
|---|---|---|
| rate limit reached | Marketing API quota (standard access is low) | wait a few minutes; fewer, larger calls |
| too much data for one call | sync response too big | narrow range/breakdowns or `mode="async"` |
| no longer accepts this authorization | token revoked or access removed | the user reconnects the connector |
| code 100 | invalid field/breakdown combination | check the lists above |
