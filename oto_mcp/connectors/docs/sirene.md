## prerequisite — insee sirene api key

the identity, search, financial statement and event tools (`fr_search`, `fr_get`, `fr_bilans`, `fr_events`…) run on open data, **without a key**.
a key is only required for **insee sirene** calls (`fr_siret`, `fr_headquarters` — siret/head office from the official source).
- create an account on the [insee api portal](https://api.insee.fr) and subscribe to the sirene api
- get your key, then set it on your oto dashboard (`sirene` connector)
- **sirene stock** queries (`fr_stock_*`, local parquet) do **not** need a key

## usage — french company data

query the identity, finances, officers, legal events and tenders of a french company.
- `fr_search(query=…, naf=…, departement=…)` — multi-criteria search (sector, area, headcount, revenue)
- `fr_get(siren)` — full aggregated profile: identity + 7 ratios from the latest inpi financial statement + bodacc events
- `fr_bilans(siren)` then `fr_bilan(siren, date_cloture)` — filing history and detailed financial statement (revenue, EBITDA, debt…)
- `fr_directors(siren)`, `fr_events(siren)`, `fr_tenders_search(query=…)` — officers, bodacc events, boamp tenders
- `fr_tenders_search(op="awarded", query= | titulaire_siret= | departement=)` — the **awarded** contracts (decp): who won, for how much, notified when. the outcome, where `op="notices"` (the default, boamp) only gives the notice — hence the real competition in a territory. a parameter specific to the other op is refused, not ignored. two regimes read, split at notification: the 2022 order since 2024, the 2019 order before — each contract carries its `arrete`. ⚠️ since 2024 the source publishes no name (buyer, place, holder), and before 2024 never the main holder's: resolve them by their siret with `fr_siret`.
- `fr_accords_search(siren=…)`, `fr_egapro_declaration(siren)`, `fr_avis_sirene(siret)` — company agreements, gender equality index, insee situation notice (pdf)

## usage — public aids (subsidies, loans, calls for projects)

the state's reference database (data.aides-entreprises.fr, ~2,400 active aids, updated daily) filtered for a company or a project.
- `fr_aides_search(insee=…, effectif=…, nature=…, echeance_avant=…)` — deterministic shortlist: territory (municipality → region → national/eu), headcount bracket, type of aid (subsidy, loan, guarantee…), deadline of the calls for projects
- `fr_aides_get(id)` — full record: purpose, conditions, amounts, funders, contacts, official source

## usage — sirene stock (bulk enrichment)

the full sirene parquet (insee, monthly vintage) for one-off lookups and **batch** enrichment of thousands of sirens.
- `fr_stock_enrich(sirens=[…])` — head offices of a **list** of sirens in a single scan (bulk)
- `fr_stock_enrich(sirets=[…])` — a **list** of establishments (up to 1,000) in a single scan, each with its headcount band, its year and its employer status, and an error per siret
- `fr_stock_siege(siren)` / `fr_stock_etablissements(siren)` — head office or all the establishments of a company
- `fr_stock_search(naf=…, enseigne=…, departement=…)` — enumerates all sites (e.g. all the "intermarché" stores of a département)
