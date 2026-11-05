"""Welcome to the Jungle — the recruiters' ATS (ex-Welcome Kit): jobs and their
stages, candidates, comments, emails, history of moves.

Wraps `oto.tools.wttj_ats.WttjAtsClient` (Bearer token). Key resolved per call
via `access.resolve_api_key("wttj")` — byo (user key or the org's shared
credential), no platform key. The token cannot be generated self-service: the
account holder requests it from WTTJ, with OAuth scopes chosen at that time;
a call outside its scopes comes back as 403 `invalid_scope`, and the refusal says so.

Vocabulary: everything starts from an **organization** (`organization_reference`); a
job offer is a **job** (`job_reference`), its pipeline stages are read ON the
job and addressed by their integer `id`; a **candidate** belongs to a job.
No global list of candidates: the API requires the job.

**Consolidated surface (ADR 0047 §Amendment)**: one tool per business OBJECT, the verb
in `op` — `wttj_job` (list/get), `wttj_candidate` (list/get/create/update). Four
tools stand alone, their parameters do not overlap those of a neighbor:
`wttj_organization` (the token's organizations, no parameters), `wttj_comment`
(a write, no read exists upstream), `wttj_emails` (a candidate's emails, read
only) and `wttj_moves` (a job's pipeline history).

⚠️ An organization's detail (`GET /organizations/{ref}`) requires a partner scope
(`su_organizations_r`) that a client account does not get: it is not served, the
token's list of organizations already carries their name and reference.

A candidate's resume comes back as `resume_url`, a CDN URL WTTJ signs for 30 minutes,
plus `resume_link`, an oto address that does not expire with it: on each click the
server asks WTTJ for the candidate again and redirects to a fresh `resume_url`
(`tools/wttj_resume.py`, route `GET /api/wttj/resume/{token}`). The raw `resume_url`
is kept: an agent that downloads the file right away needs it, and it is what WTTJ
returned. `wttj_candidate(op="resume")` returns the resume's TEXT instead, read by the
server in memory (`wttj_resume.resume_text`): an agent whose sandbox cannot reach the
CDN reads it that way. Nothing is stored by oto.

A list drops the EMPTY values of its rows (the dozen `media_*` a profile leaves blank):
nothing is lost, and a job with a hundred candidates fits in the context.

⚠️ This module WRITES into the client's ATS: `wttj_candidate` op="create"/"update"
(update MOVES a candidate by `job_stage_id` or archives them) and `wttj_comment`.
The default `op` is always a read, and a missing required argument
raises an error naming the op and the argument. Sending emails (they go out to
real people) and job publication are not served; reading a candidate's emails is.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, output_projection
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError
from . import wttj_resume

#: Where the account holder gets their token.
OU_OBTENIR_LA_CLE = ("ask WTTJ via help.welcometothejungle.com "
                     "(the token is not generated self-service)")

_JOB_OPS = ("list", "get")
_CANDIDATE_OPS = ("list", "get", "resume", "create", "update")

#: Body columns rendered as `<field>_length` in a list (sorting view).
_JOB_BODIES = ("description", "profile", "company_description", "recruitment_process")
_CANDIDATE_BODIES = ("cover_letter",)
_EMAIL_BODIES = ("body",)


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _ops_error(ops: tuple[str, ...]) -> str:
    quoted = [f"'{o}'" for o in ops]
    return "op must be " + ", ".join(quoted[:-1]) + " or " + quoted[-1]


def _need(value, name: str, op: str):
    """Required argument for THIS op — an empty value counts as missing."""
    if value is None or (isinstance(value, (str, list, dict)) and not value):
        raise _bad(f"op='{op}' requires {name}")
    return value


def _refuse_ignored(op: str, hint: str, **provided) -> None:
    """A provided argument that THIS op does not use is an error of intent: silencing
    it would return a plausible result that misses the request."""
    for name, value in provided.items():
        if value is not None and value != "":
            raise _bad(f"op='{op}' does not use {name} — {hint}")


def _upstream_message(e) -> str:
    status, body = e.status_code, e.body
    code = body.get("error") if isinstance(body, dict) else None
    if status == 401:
        return ("WTTJ rejected the token (HTTP 401) — check the key configured on "
                f"this connector ({OU_OBTENIR_LA_CLE}).")
    if status == 403:
        if code == "invalid_scope":
            return ("WTTJ: the token lacks the scope required for this call (HTTP 403 "
                    f"invalid_scope) — scopes are requested from WTTJ. {body}")
        return f"WTTJ: access to this resource denied (HTTP 403). {body}"
    if status == 404:
        return f"WTTJ: reference not found (404) — check the reference. {body}"
    if status == 429:
        return "WTTJ: too many requests (429) — try again in a moment."
    if status in (500, 502, 503, 504):
        return f"WTTJ is temporarily unavailable (HTTP {status}) — try again later."
    return f"WTTJ refused the request (HTTP {status}): {body}"


def _listed(key: str, rows, *, bodies: tuple[str, ...], always: tuple[str, ...],
            fields: Optional[list[str]], page: Optional[int],
            per_page: Optional[int]) -> dict:
    """A page of a list, projected: the API returns a bare array, with no total or cursor —
    the requested page is returned with it, so the caller knows where they stand."""
    rows, notice = output_projection.summarize(
        rows if isinstance(rows, list) else [], body_fields=bodies, fields=fields,
        always=always)
    out = {key: rows, "page": page or 1, "per_page": per_page, "count": len(rows)}
    if notice:
        out["projection"] = notice
    return out


def _sans_vides(value):
    """`value` without its empty values (None, "", [], {}), at every depth — what a
    list row carries that says nothing."""
    if isinstance(value, dict):
        out = {k: _sans_vides(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_sans_vides(v) for v in value]
    return value


def _with_resume_links(rows: list) -> list:
    """Adds `resume_link` — the durable link to give a human — next to each
    `resume_url`, sealed for the caller's account, org and team (those under which the
    key was just resolved). A row without a resume gets none."""
    if not any(isinstance(r, dict) and r.get("resume_url") for r in rows):
        return rows
    sub = access.current_user_sub_or_raise()
    org, group = access.current_org(sub), access.current_group(sub)
    out = []
    for row in rows:
        ref = row.get("reference") if isinstance(row, dict) else None
        if isinstance(ref, str) and ref and row.get("resume_url"):
            row = {**row, "resume_link": wttj_resume.mint(sub, org, group, ref)}
        out.append(row)
    return out


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """"Test the connection" probe (otomata-tech/oto#69). Covers `auth` ALONE.

    `GET /users/current` (scope `me_r`), a read with no side effect. **Authenticated ≠
    usable**: a valid token may lack the `jobs_r`/`candidates_*` scopes
    that no single-call probe covers."""
    from oto.tools.wttj_ats import WttjAtsClient

    WttjAtsClient(api_key=fields["key"]).get_current_user()


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.wttj_ats import WttjAtsClient

    connector_verify.register("wttj", _verify)

    def _client() -> WttjAtsClient:
        key, _ = access.resolve_api_key("wttj")
        return WttjAtsClient(api_key=key)

    def _run(fn):
        try:
            return fn()
        except ValueError as e:
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))

    # --- Organizations ------------------------------------------------------

    @mcp.tool()
    def wttj_organization() -> dict:
        """The Welcome to the Jungle organizations this token reaches — start here:
        every other wttj tool needs an `organization_reference` or a job. Returns
        the token's user with `organizations: [{reference, name, …}]`."""
        return {"user": _run(lambda: _client().get_current_user(organizations=True))}

    # --- Jobs --------------------------------------------------------------

    @mcp.tool()
    def wttj_job(
        op: Literal["list", "get"] = "list",
        organization_reference: Optional[str] = None,
        job_reference: Optional[str] = None,
        status: Optional[Literal["draft", "published", "archived"]] = None,
        created_after: Optional[str] = None,
        updated_after: Optional[str] = None,
        published_after: Optional[str] = None,
        page: Optional[int] = None,
        per_page: Optional[int] = None,
        fields: Optional[list[str]] = None,
        candidates_count: Optional[bool] = None,
    ) -> dict:
        """A job (offer) in Welcome to the Jungle's ATS — list, or read one with its
        pipeline stages.

        `op`:
        - **"list"** (default): jobs of one organization (`organization_reference`).
          Sorting view: long text columns come back as `<field>_length`;
          `fields=["*"]` returns full rows, `fields=[…]` picks keys.
        - **"get"**: one job (`job_reference`) WITH its pipeline `stages` —
          `[{id, name, reference, visible, candidates_count}]`. A stage is
          addressed by its integer `id` (its `reference` may be null): that id
          feeds `wttj_candidate` (`job_stage_id`).

        Args:
            status: op="list" — draft | published | archived.
            created_after / updated_after / published_after: op="list" — YYYY-MM-DD.
            page / per_page: op="list" — 1-based page; no total is returned, an
                empty or short page means the end.
            candidates_count: op="get" — add the job's total candidate count
                (needs the `candidates_r` scope).
        """
        if op not in _JOB_OPS:
            raise _bad(_ops_error(_JOB_OPS))
        if op == "list":
            _refuse_ignored(op, "exists only on op='get'", job_reference=job_reference,
                            candidates_count=candidates_count)
            org = _need(organization_reference, "organization_reference", op)
            rows = _run(lambda: _client().list_jobs(
                org, status=status, created_after=created_after,
                updated_after=updated_after, published_after=published_after,
                page=page, per_page=per_page))
            return _listed("jobs", rows, bodies=_JOB_BODIES,
                           always=("reference", "name", "status"), fields=fields,
                           page=page, per_page=per_page)
        if op == "get":
            _refuse_ignored(op, "exists only on op='list'",
                            organization_reference=organization_reference,
                            status=status, created_after=created_after,
                            updated_after=updated_after,
                            published_after=published_after, page=page,
                            per_page=per_page, fields=fields)
            ref = _need(job_reference, "job_reference", op)
            return {"job": _run(lambda: _client().get_job(
                ref, stages=True, candidates_count=candidates_count))}
        raise _bad(_ops_error(_JOB_OPS))

    # --- Candidates ----------------------------------------------------------

    @mcp.tool()
    def wttj_candidate(
        op: Literal["list", "get", "resume", "create", "update"] = "list",
        job_reference: Optional[str] = None,
        candidate_reference: Optional[str] = None,
        job_stage_id: Optional[int] = None,
        email: Optional[str] = None,
        archived: Optional[bool] = None,
        created_after: Optional[str] = None,
        updated_after: Optional[str] = None,
        page: Optional[int] = None,
        per_page: Optional[int] = None,
        fields: Optional[list[str]] = None,
        organization_reference: Optional[str] = None,
        firstname: Optional[str] = None,
        lastname: Optional[str] = None,
        candidate: Optional[dict] = None,
        patch: Optional[dict] = None,
    ) -> dict:
        """A candidate on a job in Welcome to the Jungle's ATS — list, read, add,
        move or archive.

        `op`:
        - **"list"** (default): candidates of one job (`job_reference` — there is
          no cross-job listing), optionally at one stage (`job_stage_id`), by
          `email`, `archived`, dates. Sorting view: `cover_letter` comes back as
          `cover_letter_length`, empty values are left out; `fields=["*"]` returns
          full rows.
        - **"get"**: one candidate (`candidate_reference`), with its stage and tags.
          Give a human `resume_link` (durable); `resume_url` expires in 30 min.
        - **"resume"**: the TEXT of a candidate's resume (`candidate_reference`),
          read by the server — to read or score a CV, rather than downloading
          `resume_url`. A scanned resume has no text (`text_unavailable`): open
          `resume_link` instead. Nothing is stored.
        - **"create"** — ⚠️ WRITES: add a candidate to a job at a stage.
          Required: `organization_reference`, `job_reference`, `job_stage_id`
          (from `wttj_job(op="get")`), `email`, `firstname`, `lastname`.
          Optional fields in `candidate`: `phone`, `subtitle`, `tag_list`
          (comma separated), `cover_letter`, `comment`, `referrer`,
          `remote_resume_url` (PDF/DOC/DOCX/ODT, 5 MB), `media_linkedin`, …
        - **"update"** — ⚠️ WRITES: change a candidate (`candidate_reference`):
          `job_stage_id` MOVES it to that stage, `archived=true` archives it;
          other fields go in `patch` (same names as `candidate`). Only what is
          passed changes.

        Args:
            created_after / updated_after: op="list" — YYYY-MM-DD.
            page / per_page: op="list" — 1-based page; an empty or short page
                means the end.
        """
        if op not in _CANDIDATE_OPS:
            raise _bad(_ops_error(_CANDIDATE_OPS))

        if op == "list":
            _refuse_ignored(op, "is used by create/update or get",
                            candidate_reference=candidate_reference,
                            organization_reference=organization_reference,
                            firstname=firstname, lastname=lastname,
                            candidate=candidate, patch=patch)
            job = _need(job_reference, "job_reference", op)
            rows = _run(lambda: _client().list_candidates(
                job, email=email, job_stage_id=job_stage_id, archived=archived,
                created_after=created_after, updated_after=updated_after,
                stage=True, page=page, per_page=per_page))
            if isinstance(rows, list):
                rows = _with_resume_links(rows)
                if fields != ["*"]:
                    rows = [_sans_vides(r) for r in rows]
            return _listed("candidates", rows, bodies=_CANDIDATE_BODIES,
                           always=("reference", "stage_id"), fields=fields,
                           page=page, per_page=per_page)
        if op == "get":
            ref = _need(candidate_reference, "candidate_reference", op)
            found = _run(lambda: _client().get_candidate(ref, stage=True, tags=True))
            if isinstance(found, dict):
                found, = _with_resume_links([found])
            return {"candidate": found}
        if op == "resume":
            _refuse_ignored(op, "op='resume' takes candidate_reference only",
                            job_reference=job_reference, job_stage_id=job_stage_id,
                            email=email, archived=archived, created_after=created_after,
                            updated_after=updated_after, page=page, per_page=per_page,
                            fields=fields, organization_reference=organization_reference,
                            firstname=firstname, lastname=lastname, candidate=candidate,
                            patch=patch)
            ref = _need(candidate_reference, "candidate_reference", op)
            found = _run(lambda: _client().get_candidate(ref))
            url = found.get("resume_url") if isinstance(found, dict) else None
            if not url:
                raise _bad(f"candidate {ref} has no resume on file.")
            lu = _run(lambda: wttj_resume.resume_text(url))
            link, = _with_resume_links([found])
            return {"candidate_reference": ref, "resume": lu,
                    "resume_link": link.get("resume_link")}
        if op == "create":
            _refuse_ignored(op, "use op='update' to modify an existing candidate",
                            candidate_reference=candidate_reference, patch=patch)
            args = (_need(organization_reference, "organization_reference", op),
                    _need(job_reference, "job_reference", op),
                    _need(job_stage_id, "job_stage_id", op),
                    _need(email, "email", op),
                    _need(firstname, "firstname", op),
                    _need(lastname, "lastname", op))
            extra = dict(candidate or {})
            if archived is not None:
                extra["archived"] = archived
            return {"candidate": _run(lambda: _client().create_candidate(*args, **extra))}
        if op == "update":
            _refuse_ignored(op, "is used only by op='create'",
                            organization_reference=organization_reference,
                            candidate=candidate)
            ref = _need(candidate_reference, "candidate_reference", op)
            changes = dict(patch or {})
            for name, value in (("job_stage_id", job_stage_id), ("archived", archived),
                                ("email", email), ("firstname", firstname),
                                ("lastname", lastname)):
                if value is not None:
                    changes[name] = value
            if not changes:
                raise _bad("op='update' requires at least one change: "
                           "job_stage_id, archived, or fields in patch")
            return {"candidate": _run(lambda: _client().update_candidate(ref, **changes))}
        raise _bad(_ops_error(_CANDIDATE_OPS))

    # --- Comments -------------------------------------------------------

    @mcp.tool()
    def wttj_comment(candidate_reference: str, content: str) -> dict:
        """⚠️ WRITES: add a comment on a candidate (text, HTML or markdown). The
        API cannot list comments back — this is write-only."""
        return {"comment": _run(lambda: _client().create_comment(
            candidate_reference, content))}

    # --- Emails ---------------------------------------------------------

    @mcp.tool()
    def wttj_emails(
        candidate_reference: str,
        page: Optional[int] = None,
        per_page: Optional[int] = None,
        fields: Optional[list[str]] = None,
    ) -> dict:
        """The emails exchanged with one candidate (read only — nothing is
        sent): each `{subject, from_email, from_name, to, created_at, origin,
        attachments}`. Sorting view: `body` comes back as `body_length`;
        `fields=["*"]` returns full emails, `fields=[…]` picks keys. Needs the
        `emails_r` scope on the token."""
        rows = _run(lambda: _client().list_emails(
            candidate_reference, page=page, per_page=per_page))
        return _listed("emails", rows, bodies=_EMAIL_BODIES,
                       always=("subject", "created_at"), fields=fields,
                       page=page, per_page=per_page)

    # --- Pipeline history ---------------------------------------------

    @mcp.tool()
    def wttj_moves(
        organization_reference: str,
        job_reference: str,
        page: Optional[int] = None,
        per_page: Optional[int] = None,
        fields: Optional[list[str]] = None,
    ) -> dict:
        """History of stage changes of ONE job (`job_reference` is required by the
        API): each `{candidate: {reference}, from: {stage, job}, to: {stage, job},
        created_at}`. Answers "who moved where, when" on that job.

        Needs the `moves_r` scope on the token: without it the call is refused
        (403 `invalid_scope`) — the scope is added by WTTJ on request."""
        rows = _run(lambda: _client().list_moves(
            organization_reference, job_reference=job_reference, page=page,
            per_page=per_page))
        return _listed("moves", rows, bodies=(), always=("candidate", "created_at"),
                       fields=fields, page=page, per_page=per_page)
