## prerequisite — connect with your Microsoft 365 account

on the "Power BI" card, click **Authorize Power BI** and choose your work account. Nothing else to install or register. The account itself is the "Microsoft 365 account" connector: Power BI borrows it and only adds its own permissions (read workspaces, reports and semantic models).
- the agent acts **with your rights**: the workspaces you belong to, your "My workspace", and the models you can open; each person in the org connects their own account
- ⚠️ **many organizations require a Microsoft 365 administrator to authorize oto a first time**, and an earlier approval only covers the permissions it named: if Microsoft shows "admin approval required", an administrator approves oto once for the whole organization (`microsoft_admin_consent`, see the "Microsoft 365 account" connector)
- an account already linked for another Microsoft service only needs to authorize this one; an account that has not authorized Power BI is refused by the tools, naming this card
- **in Power BI itself**: querying a model in DAX needs **Read and Build** on it, and the tenant setting « Semantic model Execute Queries REST API » turned on (on by default; it may be limited to a security group)

## usage — find, query, read the measures

`powerbi` reads only (`op=workspaces|reports|report_pages|datasets|query|measures`), under the account the call names (`_account=`), otherwise the default one. Without `workspace_id`, the person's "My workspace".
- "which Power BI reports do we have?" → `powerbi()` (the workspaces), then `powerbi(op="reports", workspace_id=…)`; a report's `datasetId` is the model behind it
- "what is in that model?" → `powerbi(op="measures", workspace_id=…, dataset_id=…)`: every measure with its table, folder, description and, when allowed, its DAX formula
- "what does it show for October?" → `powerbi(op="query", workspace_id=…, dataset_id=…, dax="EVALUATE SUMMARIZECOLUMNS('Date'[Month], \"CA\", [CA])")`: aggregate in DAX, at most 2,000 rows come back (`max_rows`)

## note — what is misleading

- ⚠️ **a measure's DAX formula is only given to a person with WRITE permission on the model** (Contributor, Member or Admin of its workspace): with Read only, `op="measures"` returns names, tables, folders and descriptions, `expression_visible=false` and a `note` — no formula. Nothing in oto can go around it
- a query is refused (400) on a DAX error, and cut (`truncated`) beyond Power BI's limits (100,000 rows, 15 MB) or `max_rows`
- models hosted in Azure Analysis Services, or live-connected to one, cannot be queried
- what the agent reads goes back to the conversation and nowhere else: oto stores no row

## note — scope

read only: workspaces, reports and their pages, semantic models, one DAX query at a time, measure definitions. No refresh, no export, no edit, no report rendering.
