## prerequisite — your silae paie api access

silae paie v1 uses oauth2 credentials with **three fields**; each firm/employer enters their own, and their payroll is visible only to them. they come from the silae api portal ([api-portal.silae.fr](https://api-portal.silae.fr)), the dossiers being opened to the api from silae itself (an "api access configuration").
- `client_id` — the api account's identifier
- `client_secret` — the associated secret
- `subscription_key` — the key of the api access configuration: **it decides which dossiers AND which functions are reachable**. a dossier missing from `silae_dossier()` may simply be outside it
enter these three fields in your oto connector keys under `silae`
- **the silae contract decides the functions**: "usage interne paie" (model 1A) or "usage interne rh" (1B) open everything this connector reads; "mise à disposition" (model 2) does not open `silae_dossier(op="organisms")` nor `silae_declaration` (dsn list, dsn content, declaration states)
- avoid calling between 1am and 5am: silae refreshes its platforms then

## usage — from the dossier to the payslip line

every call targets **one dossier** (`numero_dossier`); pay months are written `AAAA-MM` (`2026-05`), a range is `periode_debut` + `periode_fin` (both included).
- `silae_dossier()` lists the reachable dossiers; `op="current_period"` the open month, `op="establishments"`, `op="organisms"` (urssaf, pension, provident… with their affiliation)
- `silae_employee()` lists the employees (`periode="2026-05"` keeps those active that month); `op="get"` reads one employee's record; `op="jobs"` gives each job's `identifiant_emploi`
- **a payslip is addressed by matricule + `identifiant_emploi` + month + `indice_periode`**: an intermittent holds several jobs in a month, and a job can have several payslips (supplementary, profit-sharing) — `silae_payslip(op="indices")` lists them, index 0 being the first one
- `silae_payslip(op="header")` gross, net, deduction totals; `op="lines"` every line with employee/employer base, rate, amount and DUCS code (`line_filters` to keep a zone, a DUCS code, a label with `%`); `op="totals"` cumulative totals over **12 months at most**
- `silae_payslip()` (op `pdf_ids`) only returns the ids of the payslip PDF images, not their content

## usage — reconcile the charges table with the DSN

- `silae_report(report="charges_table", periode="2026-05")` — the charges table; `report="contributions_detail"` the contributions detail (**12 months at most**, `detail_salaries=True` per employee); `report="declaration_summaries"` the month's declaration summaries (pdf). `format="xlsx"` returns the spreadsheet as csv to read; a pdf comes back as its text, the original file as a temporary `raw_url`
- a heavy report: `op="start"` returns a `task_id`, then `op="status"` until `ETAT_TERMINEE`
- `silae_declaration(periode="2026-05")` lists the month's dsn (establishment, body, `type_dsn`, `fraction`); `op="dsn_content"` with those values reads one dsn, whole or only `segments` (`["S21.G00.30"]`); `op="states"` the declarations and their returns (ads, ars…)

## note — personal data and documents

nothing is masked by default: employee records, payslips and the dsn carry names, nir and pay in clear until the org sets a field-filter policy for `silae`. a filter cannot see inside a file, so **reports and dsn contents are refused while that policy masks anything**.
the readers (`silae_dossier`, `silae_employee`, `silae_payslip`, `silae_variables_to_enter`, `silae_report`, `silae_declaration`) **never write** to silae. no payslip control, payslip deletion, pay cycle nor declaration setting goes out of the connector at all.

## usage — entering payroll items (`silae_act`)

`silae_act` is the only tool that writes, and only what the next payslip computation will use. it is served at the **`full` access level only**: `read` is the default, an org admin opens `full` on the connector. it also needs silae's "usage interne rh" contract (1B) and a subscription key that opens these functions (a 403 otherwise).
- `op="add_variable_element"` — `matricule_salarie`, `periode`, `code`, and `montant` OR `valeur_chaine`: a variable pay element for one employee and one month; the code must exist in a bonus profile the computation uses (`silae_variables_to_enter` lists the dossier's codes)
- `op="add_bonus"` — same, plus optional `prime_nette`, `intitule_bulletin` (≤ 45 characters) and `intitule_court` (≤ 10)
- `op="add_hours"` — `code`, `nombre` and `ajouter`: `true` ADDS to the month's value, `false` REPLACES it
- `op="set_entries_confirmed"` — `confirmer_heures` and/or `confirmer_primes`: confirms the month's entries for the whole dossier
- silae refuses an entry on a payslip already computed: delete that payslip in silae first
- `dry_run=true` checks everything and returns the call it would make, without calling silae: show it to the person who validates, then call again without it
