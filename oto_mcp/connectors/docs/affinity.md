## prerequisite — your Affinity API key

In Affinity, open **Settings → Manage Apps** and generate an API key, then paste it on the **affinity** connector. Reference: [Affinity API keys](https://support.affinity.co/s/article/Getting-started-with-the-Affinity-API-faqs).
- API access needs an Affinity **Scale, Advanced or Enterprise** plan (not Essentials, not a trial)
- the key **acts as you**: notes, list changes and new records are attributed to you, and you only see what you can see in Affinity. Set at the organization level, every member acts as the person who created the key
- reading list rows and writing list columns need the role permission **"Export data from Lists"**; writing person/company fields needs **"Edit Global Field Values"**
- byo-only: no shared platform key — it is your CRM

## usage — what you can do

- "find Acme in Affinity" → `affinity_entity(term="acme")`, then `affinity_entity(op="get", id=…)` for its fields
- "who on the team knows her best?" → `affinity_entity(op="relationships", kind="person", id=…)`
- "show the deal pipeline" → `affinity_list()` to find the list, then `affinity_list_entry(list_id=…)`
- "move these 10 deals to Due diligence" → `affinity_list_entry(op="set_fields", list_id=…, items=[{entry_id, fields: {"Status": "Due diligence"}}, …], dry_run=true)`, then again without `dry_run`
- "add this company to the Watchlist" → `affinity_list_entry(op="add", list_id=…, entity_id=…)`
- "log a note on this call" → `affinity_note(op="create", content=…, company_ids=[…])`
- "what did we exchange with them this quarter?" → `affinity_interactions(kind="company", id=…, type="email", since="…")`

## note — what trips people up

- ⚠️ a **row id** (`entry_id`) is not the person/company id (`entity_id`): one company on three lists has three rows
- ⚠️ updating a person's emails or a company's people **replaces** the whole set: pass the existing values too
- removing a row from an **opportunity** list would delete the deal: the connector refuses it
- creating a company whose domain already exists returns the existing one instead of a duplicate
- email **bodies** are never available through the Affinity API, only subjects, participants and dates
- just chatting in Claude? Affinity's own MCP server (`https://mcp.affinity.co/mcp`) covers more; this connector is for procedures and hosted agents

## note — usage limits

900 requests per minute per user, and a monthly quota for the whole account (100,000 on Scale and Advanced, none on Enterprise) **shared** with every other Affinity integration, including Affinity's MCP server. Batch writes report the remaining monthly quota.
