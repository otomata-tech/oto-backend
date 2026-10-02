---
title: Driving Affinity (affinity_entity, affinity_list, affinity_list_entry, affinity_note, affinity_interactions)
description: which id each Affinity tool returns and the next one needs, how list columns are written (names, dropdown text, clearing), what is irreversible, and the errors that mean a missing permission — read before writing to Affinity
---

# Driving Affinity

**Provenance.** Written from Affinity's published documentation (API v2 OpenAPI 2026-09-17, API v1 reference) on 2026-10-02, before any run on a live key. Where a live run contradicts this guide, the run wins: correct the guide.

## The ids

| id | what it is | where it comes from |
|---|---|---|
| person / company / opportunity id | the record | `affinity_entity(op="search")`, `op="get"` |
| `list_id` | a list (a pipeline, a watchlist…) | `affinity_list()` |
| `entry_id` | ONE ROW of a list | `affinity_list_entry(op="query")`, `affinity_entity(op="entries")` |
| field id (`field-123`, `affinity-data-…`) | a column | `affinity_list(op="fields")` |
| option id | one choice of a dropdown column | `affinity_list(op="options")` |

A company on three lists has three rows, three `entry_id`. Writes on list columns take the `entry_id`; `add` takes the `entity_id`.

## Editing a pipeline

1. `affinity_list()` → the `list_id`.
2. `affinity_list(op="fields", list_id=…)` → column names, `valueType`, `isRequired`.
3. `affinity_list_entry(op="query", list_id=…)` → rows with `fields: {name: value}` and their `id` (= `entry_id`).
4. `affinity_list_entry(op="set_fields", list_id=…, items=[{"entry_id": …, "fields": {"Status": "Due diligence", "Amount": 2000000}}], dry_run=true)` → the diff.
5. The same call without `dry_run`.

Values: columns by name or id; dropdowns by option TEXT (case-insensitive) or id, an unknown text is refused with the list of options; persons/companies by id; dates ISO 8601; `null` clears. Enriched and relationship-intelligence columns are computed by Affinity and refused. Up to 25 rows per call; a receipt `{total, succeeded, failed}` comes back, and a 401/403/429 stops the batch.

## Irreversible or surprising

- Removing a row deletes its list column values. On an **opportunity** list it would delete the deal: the tool refuses.
- `affinity_entity(op="update")`: `emails`, `organization_ids`, `person_ids` REPLACE the set. Read first, send all values.
- Global organizations (Affinity's own company records) cannot be renamed.
- A person cannot be created with an email that belongs to another person: search first.
- Company create returns the existing record when the domain is known (`allow_duplicate: true` overrides).
- Opportunities are created ON their list (`affinity_entity(op="create", kind="opportunity", item={"name", "list_id"})`); `add` cannot put one on a list.

## Errors

| answer | meaning |
|---|---|
| 401 | key invalid or revoked |
| 403 on list rows / list column writes | the key's user lacks "Export data from Lists" |
| 403 on person/company field writes | lacks "Edit Global Field Values" |
| 403 everywhere | the plan has no API access (needs Scale+) |
| 404 | does not exist, or not visible to the key's user |
| 400 on `add` | the list has a required column (v2) or the entity type does not match the list |
| 400 on note update | the note contains @mentions |
| 429 | 900/min per user, or the monthly account quota (shared with every Affinity integration) |

## Reading activity

- `affinity_entity(op="relationships")`: team members ranked by `interactionScore` (≥ 0.7 ≈ regular contact).
- `affinity_interactions`: one external record, one type per call, window ≤ 1 year (default 90 days). Metadata only: email bodies are not in the API.
- `affinity_note(op="list", kind=…, entity_id=…)`: direct notes, notes on its meetings, mentions.
