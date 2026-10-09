## usage — find companies from their bodacc notices (dila open data)

the whole french bulletin of civil and commercial notices, no key. one tool, `bodacc_notice`, the verb as `op`:
- "every receivership opened in the Rhône this week" → `bodacc_notice(famille="collective", departement="69", date_from="2026-10-05", q="ouverture")` — flat lines with `siren`, `commercant`, `jugement_nature`, and `resume` (the notice's own text)
- "how many business sales per month in Paris this year?" → `bodacc_notice(op="count", group_by="mois", famille="vente", departement="75", date_from="2026-01-01")`
- the full notice behind a line → `bodacc_notice(op="get", id="A202601943821")` (officers, establishments, judgment, parsed)
- families: `collective` (insolvency proceedings), `vente` (sales and transfers), `creation`, `immatriculation`, `modification`, `radiation`, `dpc` (accounts filed), `conciliation`, `retablissement_professionnel`, `divers`

## note — ⚠️ what to know before sweeping

- **one known company → `fr_events(siren)`** (`sirene` connector); `bodacc_notice` is for finding companies FROM the notices, or for counting
- **always bound by date**: the bulletin holds tens of millions of notices since 2008; filed accounts (`dpc`) alone are ~70 000 a week
- **count before paging**: `op="count"` sizes a set in one call; pagination stops at offset + limit = 10 000 — past it, split the period, never page further
- **`q` and `commercant`/`ville`/`tribunal` are word searches**, not exact matches: `commercant="ACME"` also returns "ACME FRANCE"
- `siren` is null on a line when the notice carries no RCS number (some traders and older notices)
