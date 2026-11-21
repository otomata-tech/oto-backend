## usage — healthcare establishments & ESSMS

directory of healthcare and medico-social establishments + has quality evaluations — open data, no key.
- `sante_finess_search(q=…, departement=…, categorie=…)` — finess search by name or code (e.g. categorie "EHPAD")
- `sante_finess(finess)` — record of an establishment by exact code (ET or EJ)
- `sante_essms_dimensions()` then `sante_essms_search(region_libelle=…, secteur=…)` — quality evaluations of essms (has framework)

## usage — medicines, clinical trials, publications, conflicts of interest

read-only, one tool `sante(op=…)`; each answer carries its `source` (producer, licence) to cite.
- `sante(op="search_medicines", q="ibuprofène")` then `sante(op="get_medicine", cis=…)` — french public medicines database: composition, prices and reimbursement, has opinions, and the `securite` block (ansm safety notices, shortages, major therapeutic interest)
- `sante(op="search_clinicaltrials", condition="glioblastoma")` / `sante(op="get_clinicaltrial", nct=…)` — clinicaltrials.gov, sites in france by default
- `sante(op="search_ctis_trials", condition=…)` / `sante(op="get_ctis_trial", eu_ct_number=…)` — eu trials from the ema public portal (ctis), whose api is undocumented
- `sante(op="search_pubmed", query=…)`, `sante(op="search_europepmc", query=…)`, `sante(op="get_europepmc_article", pmid=… | doi=…)` — biomedical literature, abstract and full-text links through europe pmc
- `sante(op="search_interest_declarations", siren=…)` and `sante(op="interest_totals_by_company", rpps=…)` — transparence santé: gifts, agreements and fees from health companies to named professionals. **never add amounts of different types** (a fee can execute an agreement), reuse is restricted by the cc by-nc-sa 4.0 licence (transparency purposes, no strictly commercial use) and must credit "base Transparence - Santé (Direction Générale de la Santé)"
