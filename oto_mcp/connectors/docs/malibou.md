## prerequisite — malibou api key and organization id

an **organization admin** in Malibou opens Admin → Company → API keys, clicks "Create an API key", ticks the read scopes to grant, and copies the key (shown only once). The organization ID comes from the same page (actions menu → "Copy organization ID"). Both are pasted into oto.
- byo-only: no shared oto key — these are your personnel data, each organization sets its own
- **the scopes decide the fields**: employees, employee details (NIR, address, phone, bank account, manager, team), contract details, absences, absence types (leave code), work locations
- read-only: the connector never writes into Malibou
- the "test connection" button reads one employee: no side effect

## usage — employees, absences, remote work

- "the active employees" → `malibou(op="collaborators", contract_status="active")`
- "this employee's contracts" → `malibou(op="collaborator", collaborator_id="…")`
- "who is off in October" → `malibou(op="absences", start_date="2026-10-01", end_date="2026-10-31", status="approved")`
- "the pending leave requests" → `malibou(op="absences", start_date="…", end_date="…", status="pending")`
- "who works remotely this week" → `malibou(op="work_locations", start_date="…", end_date="…")`

## note — scopes, masking, what is not served

- ⚠️ **NIR, IBAN/BIC and the absence leave code (`absence_type`) are masked by default**: an org admin lifts the rule by naming the fields in the `malibou` field filters
- a 403 means the key lacks the scope of that read, or the organization ID is not the key's
- absences and work locations need both `start_date` and `end_date`; every record that overlaps the window comes back
- **not served**: payslips, payroll variables, and the partner-only routes (company and establishments, meal vouchers, health insurance contracts and affiliations), which Malibou opens only under a partnership agreement
