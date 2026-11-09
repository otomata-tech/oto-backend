## prerequisite — your pennylane api key

each user sets their own pennylane key — your books are visible only to you.
- sign in at [app.pennylane.com](https://app.pennylane.com)
- go to settings, api / integrations section, and create an api key (personal token)
- paste it into your oto connector keys under `pennylane`

## usage — read and reconcile your books

query invoices, transactions and the trial balance, and settle unmatched payments.
- `pennylane_trial_balance` the trial balance over a period, `pennylane_ref(kind="ledger_accounts")` the chart of accounts
- `pennylane_invoice(op="list")` / `pennylane_supplier_invoice(op="list")` the invoices, `pennylane_transactions` the bank movements, `pennylane_bank_accounts` the bank accounts and their current balance
- `pennylane_match` matches a transaction to its invoice (reversible) so a paid invoice isn't left as `late`
- quotes: `pennylane_quote(op="create")` (no draft: it is born `pending`) → `op="pdf"` for the PDF link to attach to an email (the link expires, re-read it just before sending) → `op="set_status"` (`accepted` on signature) → `op="to_invoice"` creates the invoice as a **draft**, which `pennylane_invoice(op="finalize")` then `pennylane_act(op="send_invoice")` issue **after human validation**
- supplier invoices: `pennylane_upload_file` (the PDF) → `pennylane_supplier_invoice(op="import")` (with `import_as_incomplete=true`, it stays in `validation_needed`) → `op="lines"` (the line `id`s and `vat_rate`s) → `op="update"` (label, dates, amounts, and `invoice_lines={"update": [{"id": …, "vat_rate": …}]}`) → `op="validate"` moves it to `complete`: a **binding** accounting entry, only on the user's explicit request, never right after an import
- supplier invoice amounts: the pre-tax total (`currency_amount_before_tax`) is required at invoice level and rejected inside a line; a line carries `currency_amount` (incl. tax) and `currency_tax`
- reverse-charge `vat_rate` (v2 reference): `intracom_21`, `intracom_55`, `intracom_85`, `intracom_100`, `extracom`, `crossborder`, `FR_85_construction`, `FR_100_construction`, `FR_200_construction`; the reference doesn't define them any further and has no `intracom_200` — the right code is decided with the accountant
- analytics: `pennylane_ref(kind="category_groups")` the axes, `kind="categories"` their categories → `op="categories"` reads, `op="categorize"` REPLACES the categories of a customer invoice, a supplier invoice (`invoice_id`) or a ledger entry line (`pennylane_ledger_entry`, `line_id`); weights as strings, summing to 1 per axis; `[]` removes them all
- bank: `pennylane_bank(op="account")` one account and its balance (a decimal string), `op="transaction"` one transaction, `op="create_transaction"` records one by hand for an account without a bank feed (check `pennylane_transactions` first, or it is counted twice), `op="update_transaction"` attaches it to a customer or supplier, `op="categorize_transaction"` its analytics
- payments: `pennylane_invoice(op="payments" | "installments" | "matched_transactions")`, and the same on `pennylane_supplier_invoice`; `op="unmatch_transaction"` undoes a `pennylane_match`; `pennylane_invoice(op="mark_paid")` marks paid WITHOUT a transaction and has no way back — prefer matching when the money is on the account
- reference data written: `pennylane_ref_edit` creates axes, categories, journals and ledger accounts (none of them can be deleted through the API; a `401…` account also creates a supplier, `411…` a customer) and updates products
- exports: `pennylane_export(op="start", kind="fec" | "general_ledger" | "analytical_general_ledger")` → `op="get"` until `ready`; the `file_url` expires 30 minutes after the read
- other records, read-only: `pennylane_records` — billing subscriptions, commercial documents (proformas, orders), purchase requests, SEPA mandates, and `kind="changes"`, what changed on one kind of object over the last four weeks
- several pennylane instances in an org (personal and company): without `_instance`, the personal key answers first — to quote or invoice on behalf of the company, pass `_instance="org:<id>:pennylane"`
- supervised credit-note flow: `pennylane_ref(kind="products")` (resolve the `product_id`, never guess it) → `pennylane_invoice(op="find")` (duplicate check) → `pennylane_invoice(op="credit_note")` (**standalone** draft, lines in positive — the "credit note" negation is applied server-side) → `pennylane_invoice(op="finalize")` then `pennylane_act(op="send_invoice")` **after human validation**

## note — project scope (#605, 2026-08-29)

`pennylane_upload_file` with a `{kind: "url"}` source reads that url server-side: under a project with `excluded_url_prefixes`, a matching url is refused, naming the reason (`file_source` seam). details: `docs/projects.md`.
