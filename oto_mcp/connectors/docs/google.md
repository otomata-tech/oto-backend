## prerequisite — the Google account the services borrow

this connector is the **account**: the Google address, its token, its default account. since the 2026-09-26 split, each service — Gmail, Drive, Sheets, Calendar, Tasks, Chat, BigQuery, Google Ads, YouTube — is a connector in its own right, with **its own** consent (its scopes only): authorise them from their card, one by one, on the same account.
- you can connect **several** Google accounts; each service tool acts on the one the call names with `_account=<email>` (the tools' `account=<email>` is the same choice), otherwise the one the project pins — on the service's card, otherwise on this one —, otherwise the default. an unknown address is refused, never replaced by another; the response names the account that served (`_account`)
- "linking an account" here requests the six services under the platform's app, and only the identity under a partner's app — its services then add theirs

## note — an account shared by the organization or team

an organization admin (**Org** tab) or a team lead (**Team** tab) can connect ONE Google account on behalf of everyone — a shared mailbox, a team calendar. tools take your own account first, then your active team's, then the organization's; `_account=<email>` targets a specific account wherever it lives. nobody reaches another person's personal account: only what an admin has set up as shared is shared.

## usage — which accounts, with which rights

`google_accounts` — the connected accounts and, for each, the services it has authorised. this is the question to ask when a service tool refuses: the account exists but has not yet authorised THIS service.
- "which Google accounts have I connected, and which have Drive?"

## note — the oto app is not published at Google (decision of 2026-09-05)

the platform's OAuth consent screen stays in **Testing** mode, and that is a choice: moving to *published* with the Gmail, Drive and Chat scopes (RESTRICTED at Google) requires a paid, annual **CASA Tier 2** audit. two consequences, to know before relying on it:

- **at most one hundred Google accounts** can authorise oto. beyond that, the connection is refused by Google, not by us.
- **the refresh token expires after seven days.** a connected account that does not come back within the week will have to reconnect — this is not a connector outage.

a partner who sets up **their own** Google app (tenant, `/admin` › OAuth apps) is not affected: their users consent under THEIR project, whose services they choose to have verified — that is what the split is for.
