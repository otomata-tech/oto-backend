## usage — collective agreements (kali)

sector-level law in full text (complete kali/dila stock, ~290k articles): minima, leave, bonuses, classifications. native idcc filter.
- `ccn_conventions(idcc=… | query=…)` — resolve an agreement ("which one is 3090?", "conventions du spectacle")
- `ccn_article(op="search")(query=…, idcc=…)` — full-text search across the articles of a sector (or all)
- `ccn_article(op="get")(kali_id)` — full consolidated text of an article + verifiable légifrance link
- company-side complement (sirene connector): `fr_accords_search(idcc=…)` — the sector's **company** agreements (who negotiated what, when), then `fr_accords_text(acco_id)` to read the agreement

## usage — mutual termination check (rupture conventionnelle)

check a rupture conventionnelle before it is filed: reference salary (12 or 3 months, bonuses prorated, leave months neutralised), length of service, legal and agreement severance floors, withdrawal period, filing and earliest termination dates. deterministic, nothing stored.
- `ccn_rupture_conventionnelle(date_entree, date_signature, date_rupture_envisagee, indemnite_convenue, mois=[…], idcc=…)` — `verdict` + one line per check with its article
- read the gross pay months and the idcc with the payroll tools; mark a month `neutralise` without giving its reason
- `non_couvert` on the agreement floor = not coded yet: check it by hand with `ccn_article(op="search")(query="indemnité de licenciement", idcc=…)`

## usage — consolidated codes (legi)

the 22 french codes with historical versions: cite the exact law, at the right date, with a légifrance link.
- `loi_article(code="CT", num="L1242-2", date=…)` — the text in force at the requested date (default today)
- `loi_article(op="versions")(code, num)` — the timeline of an article's wordings
- `loi_article(op="search")(query=…, code=…)` — find the article when you know the concept, not the number
- `loi_codes()` — the aliases covered (CT, CC, CP, CSS, CGI…)

## usage — case law (6 dila collections + cedh/cjue)

how judges rule: cassation (published + unpublished), courts of appeal, CE/CAA/TA, conseil constitutionnel, cnil, cedh, cjue. ranked by relevance × authority.
- `juris_decision(op="search")(query=…, fond=…, juridiction=…, date_min=…)` — unified full-text search
- `juris_decision(op="get")(decision_id)` — full text of a decision + légifrance link
- typical workflow: `juris_decision(op="search")` → spot the leading ruling → `juris_decision(op="get")` → cite with `loi_article` (the texts it refers to, at the decision's date)
