## prerequisite — your welcome to the jungle api token

you need an **api token** for the welcome to the jungle ats (ex-welcome kit).
- it cannot be generated in the interface: request it from wttj via [help.welcometothejungle.com](https://help.welcometothejungle.com/), describing your use
- ask for the scopes `me_r`, `organizations_r`, `jobs_r`, `candidates_rw` (or `candidates_r` for read-only), `comments_w`, `moves_r`
- paste it into your [connector keys](https://manage.oto.cx/) (or let your org share theirs)
- vendor docs: [developers.welcomekit.co](https://developers.welcomekit.co)

## usage — what you can do

drive your wttj ats: everything starts from an **organization**, a job offer is a **job**, its **stages** are read on the job, a **candidate** belongs to a job.
- "which organizations can i see?" → `wttj_organization` (gives the `organization_reference`s)
- "which jobs are published?" → `wttj_job(op="list", status="published")`; a job's stages → `wttj_job(op="get")`
- "who is interviewing for this role?" → `wttj_candidate(op="list")` with `job_reference` and `job_stage_id` (empty values are left out of the rows); details → `wttj_candidate(op="get")`
- "candidates in Lyon across all our jobs" → there is no cross-job search in the wttj api: list the published jobs (`wttj_job(op="list", status="published")`), then each job's candidates, and filter yourself
- "send me their cv" → `wttj_candidate(op="get")` (or `op="list"`) gives `resume_link`: the link to hand a person. it lasts 30 days and asks wttj for a fresh copy at each click (nothing is stored by oto). `resume_url` is wttj's own link and expires after 30 minutes: only for downloading right away
- "read / score their cv" → `wttj_candidate(op="resume", candidate_reference=…)`: the TEXT of the resume, read by oto in memory (nothing is stored). a scanned resume has no text (`text_unavailable`): hand `resume_link` to a person instead
- "add this candidate" → `wttj_candidate(op="create")`; "move them to the next stage" → `wttj_candidate(op="update", job_stage_id=…)`; archive → `archived=true`
- "log yesterday's conversation" → `wttj_comment` (write-only: the api does not read comments back)
- "what did we write to this candidate?" → `wttj_emails` (read only, per candidate; `fields=["*"]` for the bodies)
- "what moved on this role?" → `wttj_moves` (per job; requires the `moves_r` scope on the token)
