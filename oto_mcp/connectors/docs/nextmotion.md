## prerequisite — your nextmotion api key

log in to the [Nextmotion web app](https://app.nextmotion.net), then **Settings → API Keys** → generate a key and copy it: it is not shown again afterwards. paste it into your oto connector keys under `nextmotion`.
- the key acts **on behalf of the user who generated it**, on the clinics where they are employed; it does not expire, and is revoked by "Reroll" or deletion in the same place
- API access is included in the Scale plan, or as a paid option from the Growth plan (public pricing [nextmotion.net/tarifs](https://www.nextmotion.net/tarifs)); who can generate a key depending on role in the clinic is not documented
- BYO only: no shared oto key

## usage — calendar, catalogue, sales, leads, patients, statistics, stock and aggregates

start with `nextmotion_clinic()`: every other tool asks for a `clinic_id`.
- "who works at the clinic?" → `nextmotion_practitioner(op="list", clinic_id=…)`
- "today's calendar" → `nextmotion_appointment(op="list", clinic_id=…, date="YYYY-MM-DD")`
- "which slots are free?" → `nextmotion_availability(clinic_id=…, start_date=…, end_date=…)`
- "move this appointment" → pick ONE slot from `nextmotion_availability`, then `nextmotion_appointment(op="reschedule", appointment_id=…, visit_type_opening_hour_id=<slot id>, time_slot=<slot time_slot>)`
- "what do we offer, at what price?" → `nextmotion_catalog(kind="visit_type"|"treatment_type"|"treatment_pricing"|…, clinic_id=…)`
- "the quotes / invoices" → `nextmotion_quote(op="list", clinic_id=…)`, `nextmotion_invoice(op="list", clinic_id=…)`
- "January's invoices" → `nextmotion_invoice(op="list", clinic_id=…, invoiced_from="2026-01-01", invoiced_to="2026-01-31")`
- "which batches expire soon, what is out of stock?" → `nextmotion_product(op="list", clinic_id=…, expiring_within_days=30)` or `stock_state="low"|"out"`
- "which rooms, which devices, which slots, who is absent?" → `nextmotion_calendar(kind="room"|"device"|"opening_hour"|"absence", clinic_id=…)` (`show_all=True` for the whole clinic, not only the key's user)
- "online appointment requests to handle" → `nextmotion_calendar(kind="appointment_request", clinic_id=…, request_status="new")`
- "where are today's patients at?" → `nextmotion_journey(clinic_id=…, start_date="YYYY-MM-DD", end_date="YYYY-MM-DD")`
- "packages, accounting distributions, catalogue products" → `nextmotion_catalog(kind="treatment_package"|"accounting_distribution"|"global_product", clinic_id=…)`; a package's treatments → `op="items"`, a pricing's or package's per-practitioner distribution → `op="distributions"`
- "who paid what, by which means?" → `nextmotion_payment(op="list", clinic_id=…)` or `invoice_id=…` for one invoice
- "takings of a month, by practitioner?" → `nextmotion_payment(op="list", clinic_id=…, paid_from="YYYY-MM-01", paid_to=…, with_practitioner=True)`: each invoice line names the practitioner of its act; a payment covering several practitioners is not split
- "revenue by month, by treatment type" → `nextmotion_statistics(kind="appointment_income"|"treatment_types"|"treatment_types_income", clinic_id=…, period_type="month")`
- "how much has this patient been invoiced, paid?" → `nextmotion_patient_stats(patient_id=…)` with the id served by an appointment, a quote or an invoice
- "how many sessions of their package are used, how many remain?" → `nextmotion_package_usage(clinic_id=…, patient_id=…)`: the sessions consumed per quote or invoice and per treatment, next to the lines sold (name, quantity). Counters only — no note, zone or free text of a care ever comes out. ⚠️ Nextmotion does not say which line a session decrements: match a group to a line of the same document by its name
- "the prospect pipeline" → `nextmotion_lead(op="list", clinic_id=…)`, and the source or status labels → `nextmotion_setting(kind="object_label", clinic_id=…, label_types=["lead_source"])`
- "where do our patients come from, what age, what gender?" → `nextmotion_patient_demographics(clinic_id=…, by=["department","age_band"])` — headcounts only; a municipality's socio-demographic profile (population, income) is read in open data: `urba_socio(code_insee)`
- "are our machines running?" → `nextmotion_device_usage(clinic_id=…, start_date=…, end_date=…, period_type="month")` — 93 days at most per call
- "Nextmotion subscription, payment means, templates, questionnaire models, webhooks" → `nextmotion_setting(kind="feature"|"payment_medium"|"communication_template"|"document_template"|"survey_form"|"webhook", clinic_id=…)`
- "who is this patient?" → `nextmotion_patient(op="get", patient_id=…)` with the id served by an appointment, a quote, an invoice; "find Mrs X" → `nextmotion_patient(op="list", clinic_id=…, search="X")`
- "create / fix a patient's record" → `nextmotion_patient(op="create", clinic_id=…, data={"email": …, "first_name": …, "last_name": …, "gender": …})` or `op="update", patient_id=…`
- "add a room, a device, an absence, a slot" → `nextmotion_calendar(kind=…, op="create", clinic_id=…, data={…})`; edit / delete → `op="update"|"delete", item_id=…`
- "move or edit this appointment" → `nextmotion_appointment(op="update", appointment_id=…, data={"calendar_event": {"start_time": …, "end_time": …}})`
- "create a treatment type, a package, change a price" → `nextmotion_catalog(kind="treatment_type"|"treatment_package"|…, op="create"|"update", …, data={…})`; a package's lines → `op="add_item"|"set_items"`, per-practitioner distribution → `op="set_distributions"`
- "add this practitioner to all the pricings of a treatment type, keeping the others" → `nextmotion_catalog(kind="treatment_pricing", op="assign_distribution", data={"user": …, "accounting_distribution": …}, clinic_id=…, treatment_type_id=…)`, or `data={"user": …, "items": [{"id": <pricing>, "accounting_distribution": …}, …]}` when the split differs per line; remove with `op="unassign_distribution"`; audit a type first with `op="distributions", clinic_id=…, treatment_type_id=…`
- "validate this quote, collect this invoice, make a credit note" → `nextmotion_quote(op="validate", quote_id=…)`, `nextmotion_invoice(op="pay", invoice_id=…, data={"card": "100.00"})`, `nextmotion_invoice(op="credit_note", clinic_id=…, data={"patient": …, "items": [...]})`
- "add this prospect, convert them into a patient" → `nextmotion_lead(op="create", clinic_id=…, data={…})`, `nextmotion_lead(op="convert", lead_id=…)`
- "log this call, send the quote by email" → `nextmotion_communication(kind="call"|"message", clinic_id=…, data={…})`

## note — invoices by period: a complete, bounded walk

- the Nextmotion API does not filter invoices by date and does not document their order: the tool reads **all** the pages (100 invoices per call) and keeps those whose `invoiced_time` falls within the period, bounds included
- the walk is capped by `max_pages` (20 by default, 100 at most); the response gives `pages_lues`, `factures_parcourues` and `complet`
- `complet: false` = **partial** result: rerun with `offset=<offset_suivant>` and the same period to read the rest
- with a period, `limit` is refused and `offset` is the starting point of the walk
- no period filter on quotes: a quote has no invoicing date, and its issue date can be empty

## note — product stock: no link to invoices

- `nextmotion_product` reads the clinic's stock (one batch per line: batch number, expiry, stock levels, unit price, product and brand); a batch can be created, edited (levels, expiry) and deleted
- the Nextmotion API exposes **no consumable or batch per invoice or per treatment**: an invoice line carries the act and its amounts, never the batches consumed, and nothing links a batch to an invoice or to a patient

## note — patient base and devices: aggregates, never a row

- `nextmotion_patient_demographics` reads the whole patient list to **count**: by postal code, department, city, country, gender or age band; no row, no id, no name comes out, and **a cell of fewer than 10 patients is masked** (only the masked total is returned) — crossing many dimensions masks a lot: start broad
- age comes out as a band (0-17, 18-24, 25-34, 35-44, 45-54, 55-64, 65+), never as a date of birth; the department is deduced from a French postal code, "étranger" if the country is not France
- `nextmotion_device_usage` counts, per device, the appointments held, their minutes and those not held, by reading the calendar day by day: it is the **booked** usage, not the machine's real usage (shots, actual duration), which Nextmotion does not know; no occupancy rate, the API does not give a device's capacity

## note — health data: what is not served

- **no medical content**: history, photos and media, prescriptions and their signature, signed consents, treatments performed, consultations, visits and their notes, questionnaire answers stay outside the connector, like the chat with patients; neither quotes nor invoices are created here (Nextmotion only creates them under a consultation)
- **cannot be deleted**: a patient, an invoice, a payment (an accounting document is corrected by a credit note or an update)
- **everything that goes out passes through an allowlist** written from the spec: a field Nextmotion adds tomorrow does not come out, and `fields=["*"]` returns the default view, never the raw
- **the patient's identity comes out through `nextmotion_patient` alone**: last name, first name, email, phone, date of birth, age, gender, address, postal code, city, country, contact consents, patient number, archived — **never** the practitioner's comments, the photo or the GPS coordinates, and `doctor_comments` is refused on write
- **elsewhere, the patient is served only by id** (appointments, journey, quotes, invoices, payments, calls, credit notes, `dry_run` previews included); when the identity is useful, this id is resolved with `nextmotion_patient(op="get", patient_id=…)`. `nextmotion_patient_stats` returns its financial totals and its visit dates
- **a lead serves its contact identity** (last name, first name, email, phone); its notes and its external reference can be written but never come back out; no search by lead name is offered
- **the person of an online request is not read back**: no name, no contact details, no date of birth; it can be written (creating a request) but does not come back out
- also removed: practitioner comments, titles, notes and reminder texts of calendar events, quote/invoice titles, line details, PDF document, link to the treatment performed, number, notes and transcript of a call, recipient of a message, and any free text, **with no option to get the raw**
- remaining as text: catalogue labels (visit type, name of a line or of a sub-pricing, detail of a pricing), tags (source, status, desired treatment and zone of a lead) and the practitioners' names; if a practitioner typed a patient's name into a line label, it would pass through

## note — sales: full lines, statistics, settings

- a quote or invoice line carries its price, quantity, discount, margin (`markup`), VAT and its sub-pricings (`subpricing`: nature, price, VAT, accounting code, clinic or practitioner share)
- a payment carries the amount per means (card, cash, cheque, transfer, Stripe, credit note, custom means) and its invoice, projected as in `nextmotion_invoice`
- statistics return charts (`title`, `labels`, `datasets`); the API's `meta` block, undescribed, is not served
- communication and document templates, questionnaire models: metadata and merge fields on read (type, name, enabled), full body on write; webhooks: their headers, which usually carry a secret, can be written but are never returned, preview included

## note — writes: a preview first

- **every write has `dry_run=True` by default** (creation, modification, deletion, rescheduling, validation, collection, credit note, conversion, sending): the call validates `data`, re-reads the targeted object and returns what would go out, without writing anything. pass `dry_run=False` to act
- the body goes in `data`, checked against the fields the Nextmotion spec accepts for THIS op: **an unknown field is refused, by name**, even in nested objects — never ignored; a missing required field too
- a "complete" list replaces: a visit type's `sub_visit_types`, a treatment type's `pricings`, a package's `op="set_items"`, `op="set_distributions"` — an omitted element is deleted
- `op="assign_distribution"` / `"unassign_distribution"` change ONE practitioner on up to 500 pricings or packages and rewrite every other practitioner as read: the preview lists each item's change (add, update, remove, unchanged), the write touches only the items that change, and an item that fails is listed in `failed` without stopping the others
- a `null` value is not sent: a field cannot be emptied through this tool
- `nextmotion_communication(kind="message")` **sends** an email, SMS or WhatsApp to the patient, for a quote, an invoice or an administrative document only — never a prescription, a consent or a medical document
- **never an implicit notification**: editing an appointment would notify the patient by default (`send_appointment_modified_email|sms` are `true` in the spec); the tool sends them as `false` unless you pass them as `true`, and the preview says who would be notified (`notifie_le_patient`)
- whether Nextmotion notifies the patient on a reschedule or a deletion is not documented
- the lines of a quote or invoice are not edited here: each requires the id of a clinical treatment; a credit-note line references neither a treatment nor an extracted package
- `pay` and `credit_note` validate the document by default on the Nextmotion side (`do_validate: true`); pass `do_validate: false` to keep a draft
- derived from the public OpenAPI spec, never exercised with a real key
