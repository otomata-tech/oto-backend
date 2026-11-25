## prerequisite — github token

create a token in GitHub (Settings → Developer settings → Personal access tokens), then paste it into oto.
- two families of tokens, and they are not configured the same way:
  - **classic** — tick *scopes*: `repo` (private repositories, issues, PRs), `read:org` (organizations and teams), `workflow` (GitHub Actions)
  - **fine-grained** — tick *permissions* (Contents, Issues, Pull requests, Actions, Members…) **and the list of repositories** the token applies to. A repository missing from this list is invisible, even with the right permission
- byo-only: no shared oto key — a GitHub token carries its holder's identity, and every commit, comment or merge is **attributed** to them
- **API URL** field to leave empty for github.com. For a self-hosted **GitHub Enterprise Server**: `https://<your-host>/api/v3`
- the "test connection" button checks that the token is alive and says which account it belongs to. It cannot verify more: a classic token's scopes are only readable in a response header, and a fine-grained token does not expose its repository list

## usage — read a repository, follow tickets, watch CI

- "what is this repository about?" → `github_repos(op="get")` then `github_files(op="readme")`
- "read me this file" → `github_files(op="read", path="src/x.py")` — the content comes back decoded
- "what changed between these two versions?" → `github_repos(op="compare", base="v1.2.0", head="main")`
- "the open tickets" → `github_issues(op="search", state="open")`
- "open a ticket" → `github_issues(op="create", fields={"title": "…", "body": "…"})`
- "open a PR on a fix" → `github_act(op="create_branch", branch="fix/x", ref="main")`, then `github_files(op="write", branch="fix/x", …)` — `branch` required, otherwise the commit lands on the default branch — then `github_pulls(op="create")`; an existing branch is refused (422), never moved
- "the PRs awaiting review" → `github_pulls(op="search", state="open")` then `op="reviews"` on a number
- "what does this PR change?" → `github_pulls(op="files", number=…)`
- "why did CI fail?" → `github_actions(op="runs", status="failure")` → `op="jobs"` → `op="logs"`
- "who has access to this repository?" → `github_orgs(op="collaborators")`, and `op="permission"` for a person's EFFECTIVE level (team inheritance included)
- "find where this symbol is used" → `github_search(op="code", q="myFunction repo:org/repo")`
- diagnose a refusal before blaming a name → `github_orgs(op="rate_limit")` (does not consume quota) and `op="me"`

## note — the 404 trap

⚠️ **on a private resource the token is not allowed to see, GitHub answers 404, not 403** — on purpose, so as not to disclose its existence.

"repository not found" therefore means, in order of likelihood: the token does not have the `repo` scope; or (fine-grained) this repository is not in the token's list; or the organization enforces an SSO authorization that the token has not yet received. Checking the name comes last.

## note — a pull request IS an issue

⚠️ at GitHub, the two share the same numbering and the same list endpoint. Consequences:
- `github_issues(op="search")` **drops PRs by default**, without which "how many open tickets?" gives a wrong number, often by a lot. `include_pull_requests=true` returns the API's raw response
- this sorting happens after pagination: a page of 30 of which 12 are PRs returns 18 — this is normal
- conversely, and this is handy: a PR's thread comments, labels, assignments and milestones go through `github_issues` with its number
- to count cleanly on both sides: `github_search(op="issues", q="repo:org/repo is:issue is:open")`

## note — what writes, and what costs

- ⚠️ **`github_actions(op="dispatch")` triggers a real run** — thus potentially a build, a publication or a **deployment**. It is **dry-run by default**: `dry_run=false` to trigger. The workflow must declare `workflow_dispatch`, otherwise 404 ("not triggerable", not "does not exist"), and the response does not return the created run: find it by listing the runs right afterwards
- ⚠️ **`github_pulls(op="merge")` writes to the target branch**, with no one-click undo. `merge` adds a merge commit, `squash` squashes the branch into a single commit, `rebase` rewrites the commits — three different effects on history. Passing `sha` protects against the race: if the head moved since the read, GitHub refuses instead of merging something else
- ⚠️ **a review without `event` stays pending** (`PENDING`): nothing is published, nobody is notified, and it is only visible to its author. `APPROVE` can unblock a protected merge — it is an act of governance
- **an image (png, jpeg, webp) → `github_files(op="write", source_url=…)`**: the server downloads it (https, public hosts only, redirects re-checked, 10 s, 2 MB), checks its first bytes really are an image (svg and html refused, extension must match), commits the bytes, and returns `sha`, `commit_sha`, `type`, `width`, `height`; `require_square=true` for a logo. `content_base64` for bytes already in hand. Same `sha` rule to overwrite
- ⚠️ **`github_files(op="write")` on an existing file requires its `sha`** (read with `op="list"`): without it, 409. This is GitHub's concurrency control, which guarantees we replace the version read. Each write is a **real commit**, attributed to the token holder
- ⚠️ **`github_issues(op="assign")` silently ignores** an account without write access to the repository: the response comes back as a success without having assigned it. Compare the returned list with the requested one

## note — three memberships that get confused

⚠️ member of an **organization**, member of a **team**, collaborator on a **repository**: removing someone from one does not remove them from the others.
- `github_orgs(op="remove_member")` leaves the organization **and all its teams**
- `op="remove_team_member"` only touches the team
- `op="remove_collaborator"` only touches one repository, **and does not remove access inherited from a team** — then check with `op="permission"`

⚠️ `op="members"` only shows what the token is allowed to see: without `read:org`, only **public** members come out — a shorter list, with no error. It is not a census. And `op="set_membership"` / `op="add_collaborator"` **send an invitation**: access is only effective once accepted.

## note — the limits that silently truncate

- ⚠️ **`per_page` caps at 100 and GitHub trims without error** beyond that. The connector refuses locally, naming the limit, rather than returning 100 rows where you believed 500 — to go further, paginate with `page`
- ⚠️ **search stops at 1,000 results**, whatever `total_count` announces (which is an estimate of the corpus, not a number of retrievable rows). `github_search` therefore surfaces a `troncature` block, which is also true when GitHub gave up the search midway
- ⚠️ **code search only indexes the default branch**, ignores files larger than 384 KB, and requires a real term — `repo:x` alone is not enough. It does not return the content: read it afterwards with `github_files(op="read")`
- `github_pulls(op="files")` is capped at 3,000 files (and omits big `patch`es), `op="commits"` at 250; `github_repos(op="commit")` at 300 files. A massive PR or commit is returned incomplete, without error
