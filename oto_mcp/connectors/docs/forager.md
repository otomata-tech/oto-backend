## prerequisite — forager api key

create an API key on [app.forager.ai](https://app.forager.ai) (key management — creation/deletion — is **dashboard only**, not from oto), then paste it into oto.
- your own key, or the shared oto key when it is granted to your org: it serves the lookups this oto instance chose (by default a person's phone numbers), never `forager_account`
- an optional second field `account_id` exists if your key has access to **several** Forager accounts — leave it empty otherwise, it is resolved automatically

## note — ⚠️ pay-per-credit, every search/lookup consumes the account balance

Unlike most oto connectors, **every search or lookup call bills one credit** on the Forager subscription (`forager_account(op="me")` shows the balance). Only `forager_account` and `forager_autocomplete` are free.
- use `op="totals"` (job posts, organizations, role search) to learn a volume **before** paying for the full results page
- `forager_account(op="balance_log"|"balance_totals")` traces consumption

## note — ⚠️ ID filters do not take free text

`locations`, `industries`/`industries_exclude`, `keywords`/`organization_keywords`, `web_technologies`/`organization_web_technologies`, `person_skills` expect **integer identifiers**, never a string. Passing `locations=["Paris"]` matches nothing. Resolve first via `forager_autocomplete`:
- "remote job postings in Paris" → `forager_autocomplete(op="locations", q="Paris")` → get the id → `forager_job_post(op="search", filters={"locations": [<id>], "is_remote": true})`

## usage — job posts, organizations, people, feedback

Forager combines job postings, company data and contact enrichment, in 6 tools:
- "engineer job postings at this company" → `forager_job_post(op="search", filters={"title": "engineer", "organization_ids": [...]})`
- "how many remote postings right now, before paying for the full list" → `forager_job_post(op="totals", filters={"is_remote": true})`
- "SaaS companies with 50 to 200 employees" → `forager_organization(op="search", filters={"industries": [<SaaS id>], "employees_start": 50, "employees_end": 200})`
- "the tech stack of acme.com" → `forager_organization(op="website", domain="acme.com")`
- "the full profile of this person (LinkedIn, experience, education…)" → `forager_person(op="detail", linkedin_public_identifier="janedoe")`
- "their work email / personal email / phone" → `forager_person(op="work_emails"|"personal_emails"|"phone_numbers", person_id=...)` (`personal_emails` only returns consumer addresses — Gmail, Outlook… — never a company domain)
- "who is this email address / phone number" → `forager_person(op="reverse_by_email", email="...")` / `op="reverse_by_phone"`
- "the people holding a given role in a given industry" → `forager_person(op="role_search", filters={"role_title": "...", "organization_industries": [...]})`
- "this email did not match the right person" → `forager_feedback(op="work_email", email="...", contact_status="invalid", is_correct_person=false)` — each call creates a NEW feedback row, never an upsert

## note — no API key management here

`GET/POST /api/api_keys/` and `GET/DELETE /api/api_keys/{prefix}/` exist on the Forager side but are **deliberately** not exposed as tools: creating a key would expose a secret in the agent context, and deleting one could silently break another integration. Management exclusively on [app.forager.ai](https://app.forager.ai).

## note — no result ≠ no result

`forager_person(op="work_emails"|"personal_emails"|"phone_numbers")` returns `[]` when nothing is found (not billed). `op="reverse_by_email"|"reverse_by_phone"` **raises an error** in the same case (Forager answers 404) — the two lookup families do not signal "nothing found" the same way, confirmed live.

## note — live-tested on 2026-08-21

Built from Forager's official OpenAPI spec (unlike Grain/Fireflies, a real machine-readable spec exists), then live-tested on a real Trial key (50 credits, 36 spent — one call per op of the 6 tools). Everything works end to end. Two real discoveries along the way:
- `forager_autocomplete` returns `id`s as **strings** (`"9839"`), to be cast to integers before passing them back into a `filters` — see the note above on ID filters
- `forager_job_post(op="totals")` and `op="search")` returned different `total_search_results` on the same filter (500 vs 949) during the test — no explanation found on the client side, keep it in mind if a number looks surprising

Still unconfirmed (not tested to save balance): the exact behavior on a badly typed filter, and the shape of `test_scores` on `person_detail_lookup`.
