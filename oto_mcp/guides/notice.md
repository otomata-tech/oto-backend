---
title: Notice — the oto user manual
description: Full user manual of the platform — the handshake base is only its summary; read it at the start of a session.
---

The base injected at the handshake is a SUMMARY: several clients truncate it (~2,000
characters) or do not deliver it at all. This guide is the full version — every
rule of the summary is detailed here. After reading it, load your context — that of
your org (its readme, your team's, its recent projects) and your own (your profile,
your note): `oto_context` — and reload it after a change of org/team/project.

## Look for a procedure BEFORE acting

Before a substantial task, check whether a relevant procedure exists (`oto_procedure op=list`), including outside the current project. An existing procedure takes precedence over your own method.

## Writing a procedure

A procedure reads as prose: numbered steps, which the agent running it follows in order. No drawing is required. If the body you read carries a `<!-- flowchart: … -->` line, it is the drawing of an existing version set aside: copy it as is to keep it.

## Frame and report back

When you run a procedure — a named guide OR a one-shot sequence worth tracing — open it with `run_start(label, guide?)` (pass `guide`=slug for a named guide, omit it for an ad-hoc run) and close it with `run_finish(run_id, outcome)` (done|partial|failed|blocked — `partial` = finished cleanly but not everything done: the `note` then says what is done and what remains). **Report every usage signal** via `feedback(signal, kind, target, text?)`: `signal='gap'` when oto does NOT cover what you need (missing tool, guide or data — `target`=what you wanted to do) rather than giving up silently; `signal='tool_feedback'` when a tool behaves badly or excellently (`target`=the tool's name). **Trigger it ON YOUR OWN, immediately, without waiting for the user to ask**: as soon as a tool fails (error, timeout), returns a misleading/empty/inconsistent result, or you lack a capability to act — call `feedback` on the spot, then carry on. A missed signal = a bug the platform will never see. This is how the platform learns.

## Work in a project

A project is the home of a task: its context (brief, tables, preconfigured connectors, procedures). When you act FOR a project, pass the `_project=<id>` token on EVERY work call (list/load via `oto_project` op=list/get — no session state): your connectors then take the project's preconfigured identity, the project's org applies, your runs are attached to it, and your output tables must be linked to it (`oto_project(op=link, target_type=tableau)`). A procedure run in a project shares ITS resources (tables, connectors): do not create resources specific to the procedure. **Create a project only for recurring work** (a process that will run again, a campaign, a pipeline someone follows) **or when the user asks for one.** A one-off question or task (a lookup, a quick extraction, a single answer) just runs: do it in an existing project when one already covers the subject (`oto_project op=list`), otherwise with no project at all. Never open a project only to hold a one-off answer: an empty project with a one-line brief is noise in the user's workspace.

## Carry your context IN the call, never in a session state

There is NO server session state: when an action depends on a specific context, pass it AS A PARAMETER of the call. **Context tokens are prefixed with `_`** (they belong to the platform; without the prefix, the name belongs to the tool's business arguments) — `_project=<id>` (the PRIMARY token: the project's org, `slot:<name>` slots, prebuilt connector identities), `_org=<id>` / `_group=<id>` (act in a given org/team), `_account=<label>` (multi-account connector, e.g. "2 Zoho" — `oto_identity(op="list")` lists the labels), `_instance=<ref>` (a SPECIFIC connector instance — an exact credential, refs via `oto_instance(op="list")`), `_run_id=<id>` (attach the call to a `run_start`). **The first five are OPTIONAL: when omitted, each takes its default** — your current org, no project, your default account, normal credential resolution. Only pass them to depart from that default. **`_run_id` is the EXCEPTION: as soon as you have opened a run (`run_start`) or claimed a row (`data_claim_next`), pass it on EVERY call until `run_finish`.** It is inherited only if the server holds a session with an active run — do not count on it: without it, a write on a row you claimed yourself is REFUSED, and you lose the row. Their description in the schema is deliberately one line long; the substance is here. The `oto_use_*` tools no longer set any state: they validate access and remind you of the token to pass.

## What lasts: three homes

What you learn and what must survive the session has a home depending on what it describes:

- **How the organization works** — rules, conventions, vocabulary, who does what, what must never be done: its **guide and its context**. `oto_context` returns them (load it at the start of a session); they are written with `oto_guide` (`scope=org`, reserved to an org admin — if you are not one, propose the text to the person rather than storing it elsewhere). A sequence that gets replayed is a **procedure** (`oto_procedure`).
- **What concerns the person** — their role, their goals, their preferences, their way of working: **their profile** (`oto_profile op=update`, what they told you, never an assumption) and **their note** (`oto_guide scope=user delivery=init`, in their own voice). Both are re-read at every session.
- **The facts of a piece of work** — sources, minutes, reference notes, deliverables: **a doc in the relevant project** (`oto_doc op=create|update`, `kind=source|note`, with the `project_id` of the project the fact belongs to — `oto_project op=list` to choose it, `op=create` to open one). A page cites another one from any project of the org with `[[Exact title]]`.

**Reflex**: before the web, search what the org already knows — its context (`oto_context`), its procedures (`oto_procedure op=list`), its documents (`oto_doc op=search`) — and capture in the right home rather than letting it slip. ⚠️ There is no separate "knowledge base": what a procedure or a page still calls that is an ordinary project of the org, whose pages are read and written with `oto_doc` like those of any project.

## A tool not listed? Call it anyway via `oto_call`

`oto_call(name, arguments)` is the universal bridge: it runs ANY tool of the catalog by its name — a hidden tool, a FOD tool, or a connector you have JUST activated. ⚠️ Activating a connector mid-conversation does NOT mount its tools in the session (the registry is frozen when the session opens, and claude.ai does not apply hot reloading): NEVER conclude "the capability does not exist". Call it right away via `oto_call(name="<connector>_…", arguments={…})` — it also accepts `_org=` to run under a given org — or invite the user to open a NEW conversation to see them mounted. (A sub-agent you launch inherits the same frozen registry → it too goes through `oto_call`.)

⚠️ **A tool missing from your list is NOT a broken connector.** The list of a session — including a scheduled session, on a connector activated long ago — may not show tools that respond perfectly well: it is a **display filter**, not an access failure. The expected path is `oto_call(name="<tool>", arguments={…})`. A failure is observed on the RESPONSE to a call (named error), never on an absence from the list: never report "connector broken" or "connector missing" before having called the tool through `oto_call`.

## Your toolbox = what is installed; the rest gets INSTALLED

Your toolbox, for this org, contains what is installed there for you: by you, by your **organization's kit** (the list its admin installs for all its members — a member can remove what the kit has set up), or by what your keys and open data serve (a connector that your own key, your active team's or your org's key serves, and the keyless open data, install themselves — what you removed or paused stays so). An org without a kit starts with its open data and what its keys serve: that is the normal regime, not a failure. A connector installed but switched off by the org stays hidden until it is reopened. If the toolbox shows (almost) only `oto_*`/`data_*` tools, your role is to GUIDE: understand what the user wants to do, spot the matching capabilities in the namespace catalog injected at the handshake (or `oto_connector(op='list')` for the per-connector status), propose 2-3 relevant ones and install them (`oto_connector(op='select', name=…)`). Do not wait for the remount: run right away via `oto_call`. Open data capabilities (`fr_*`, `foncier_*`, `juris_*`…) and free tier ones (serper, hunter…) work with no configuration at all; those requiring a key or an account are connected on the dashboard — just say so, never simulate a result.

## Slots: the procedure declares, the project binds

A procedure declares its required entities as named slots and its prose references them as `<slot:name>` — never a hard-coded instance name. The project maps name→instance through its links (`oto_project(op=link, …, slot='name')`; for a connector, `instance_ref=<ref>` binds an EXACT credential). You address a slot's table with `datastore='slot:<name>'` on the `data_*` tools, within the project (`_project=<id>` on the call). If a slot does not resolve (no `_project=` on the call, or unbound name), the call is REFUSED with the steps to follow: **materialize the context first** — ask which project (or create one), and for each slot bind an existing resource or create it; NEVER pick a "probable" table in place of a missing binding.
