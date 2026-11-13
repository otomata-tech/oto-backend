"""Jev tools: typed answers instead of a model turn (TypeSafe, via OpenRouter).

Wraps `oto.tools.jev.client.JevClient`. `jev_ask` = one state, whole rubric in one call;
`jev_items` = many states, same rubric, nothing written; `jev_rows` = a table's rows,
read, judged and written back on the server (helpers in `jev_rows.py`).

⚠️ Shared key only (see `providers/jev.py`): the TENANT's key, or a shared key set at the
PLATFORM level of the instance; a closer key is refused, naming who removes it.
Billing: `quantity` = the real upstream cost in micro-dollars (like `serper`), not a call count.
"""
from __future__ import annotations

import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from typing import Any, Optional

import requests
from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, credentials_store, session_org
from ..access import CredentialUnavailable
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError
from . import jev_rows as jr

#: The cascade rungs this tool serves: the shared keys. A closer rung wins the cascade
#: first and is refused. PLATFORM: a shared key set at the instance's platform level
#: (granted to orgs, never a free tier); the primary tenant of an instance has no other.
RANGS_SERVIS = (credentials_store.TENANT, credentials_store.PLATFORM)

#: Who can remove a key that shadows the shared one, per rung.
QUI_RETIRE = {"org": "an org admin",
              "group": "a team admin",
              "user": "its owner, on their account page"}

#: Max states per `jev_items` (measured 28/09/2026: 50 in flight, no 429).
MAX_ITEMS = 200
#: Default in-flight calls, below the measured rate: these are server pool threads.
PARALLELE_DEFAUT = 10
PARALLELE_MAX = 50

#: Max size of ONE state (UTF-8 JSON bytes), ~4k tokens; caps a call's spend (input is billed).
MAX_ETAT_OCTETS = 16_000

#: Read timeout per answer (connect keeps the client's 10 s); answers take ~0.5 s.
LECTURE_S = 15
#: Hard cap on the REST path (`capabilities/tools_me.py`), same as `clay`.
REST_CALL_LIMIT_S = 45.0
#: Batch send window: REST cap − worst in-flight call (10 + 15 s) − margin. Unsent states go to `retry`.
LOT_FENETRE_S = REST_CALL_LIMIT_S - (10 + LECTURE_S) - 5.0


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _upstream_message(e) -> str:
    status = e.status_code
    if status in (401, 403):
        return (f"OpenRouter rejected the key (HTTP {status}): the shared Jev key is "
                "invalid or revoked. Whoever set it (tenant admin, or instance admin for a "
                "platform key) must set it again.")
    if status == 402:
        return ("OpenRouter credits exhausted (402): whoever set the shared Jev key "
                "(tenant admin, or instance admin for a platform key) must top it up.")
    if status == 429:
        return "Jev: too many requests (429). Retry shortly."
    if status == 400:
        # Upstream body verbatim: it names the faulty question or `max_tokens_exceeded`.
        return f"Jev rejected the request (400): {e.body}"
    if status in (500, 502, 503, 504):
        return f"Jev is temporarily unavailable (HTTP {status}). Retry later."
    return f"Jev rejected the request (HTTP {status}): {e.body}"


def _microdollars(cout: Optional[float]) -> int:
    """Call cost in micro-dollars, rounded UP; no declared cost bills 0.

    ⚠️ The `round` before `ceil` matters: a batch cost is a float sum, and
    `3 × 1e-05` = 3.0000000000000004e-05, which bare `ceil` would bill as 31 µ$."""
    return math.ceil(round(float(cout) * 1e6, 6)) if cout else 0


#: OpenRouter's "current key" read: free, works with the inference key Jev holds
#: (`GET /api/v1/key` — the account balance, `/api/v1/credits`, needs a MANAGEMENT key,
#: which this connector never holds). It says what is left of the key's OWN spending
#: limit (`limit_remaining`, US$), `null` when the key has no limit.
_OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"


def _limite_openrouter(key: str) -> Optional[dict]:
    """`{"quota": …}` when the key carries a spending limit OpenRouter states, else
    `None` (no limit set, or the read failed: "not measured", never zero). Never raises:
    the decision call above already proved the key; this only adds a measurement."""
    try:
        r = requests.get(_OPENROUTER_KEY_URL, headers={"Authorization": f"Bearer {key}"},
                         timeout=10)
        data = (r.json() or {}).get("data") if r.ok else None
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    restant = connector_verify.nombre_lu(data.get("limit_remaining"))
    limite = connector_verify.nombre_lu(data.get("limit"))
    if restant is None:
        return None
    quota = {"restant": restant, "unite": "usd"}
    if limite is not None:
        quota["limite"] = limite
    if isinstance(data.get("limit_reset"), str):
        quota["remise"] = data["limit_reset"]
    return {"quota": quota}


def _verify(fields: dict, config: dict | None = None,  # noqa: ARG001
            instance: tuple | None = None) -> Optional[dict]:
    """"Test connection" probe: the smallest possible call (one word, one yes/no), then
    what is left of the OpenRouter key's spending limit, when it has one
    (`_limite_openrouter`) — so a shared key nearing its limit shows a balance in
    `oto_admin_platform_balances` instead of `auth` only.

    ⚠️ A key on any rung but a shared one (TENANT, PLATFORM) fails here without a call:
    it would be refused at use. Without `instance` (check before saving), only the key
    is tested."""
    if instance is not None and instance[0] not in RANGS_SERVIS:
        raise ValueError(
            f"a `jev` key set at the '{instance[0]}' level is not used: Jev only runs "
            "on the TENANT's key or a PLATFORM key, which this one would shadow.")
    from oto.tools.jev.client import JevClient
    JevClient(api_key=fields["key"]).decide(
        {"word": "test"},
        {"ok": {"type": "noul", "instructions": "Is this word 'test'?",
                "criteria": {"true": "the word is test", "false": "another word"}}},
        timeout=20)
    mesure = _limite_openrouter(fields["key"])
    if mesure and mesure["quota"]["restant"] <= 0:
        raise connector_verify.QuotaEpuise(
            "The Jev key authenticates, but its OpenRouter spending limit is used up "
            f"(${mesure['quota'].get('limite', '?')}). Raise the limit or top up at "
            "openrouter.ai/settings/keys — reconnecting would change nothing.")
    return mesure


def client_partage(units: int = 1):
    """Client on the shared key; `units` = answers in this call (quota pre-check).

    ⚠️ The winning rung is CHECKED: an org/user key would bill someone who didn't
    opt in, invisibly to the tenant. The generic "set your own key" refusal is
    replaced by the only valid path. Resolved under the CURRENT identity: the
    background worker sets the job's (`jev_jobs_worker`)."""
    from oto.tools.jev.client import JevClient
    try:
        rc = access.resolve_credential("jev", want="auto", units=units)
    except CredentialUnavailable as e:
        raise CredentialUnavailable(ErrorData(
            code=INVALID_PARAMS,
            message=("No `jev` key for your org: Jev runs on a shared key, either "
                     "your TENANT's (a tenant admin sets it once for all its orgs) "
                     "or one set at the instance's platform level and granted to "
                     "your org. Org, team or personal keys are not used."))) from e
    if rc.mode not in RANGS_SERVIS:
        qui = QUI_RETIRE.get(rc.mode, "whoever set it")
        raise _bad(
            f"A `jev` key set at the '{rc.mode}' level shadows the shared key: "
            "Jev only runs on the TENANT's key or a PLATFORM key, so this one is not used. "
            f"To use the shared key, {qui} must remove it (Jev connector card).")
    return JevClient(api_key=rc.key)


def releve(cout_total: float) -> None:
    """Record the call's REAL spend in micro-dollars (else a 200-item batch bills as one
    call), on the current call's trace — the background worker opens one per slice."""
    u = _microdollars(cout_total)
    if u:
        session_org.note_call_trace(quantity=u)


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError

    connector_verify.register("jev", _verify)

    _client = client_partage

    def _etat_borne(state, ou: str) -> None:
        """Refuse a state over `MAX_ETAT_OCTETS` before any call."""
        taille = len(json.dumps(state, ensure_ascii=False, default=str).encode("utf-8"))
        if taille > MAX_ETAT_OCTETS:
            raise _bad(
                f"{ou}: state is {taille} bytes, max is {MAX_ETAT_OCTETS}. Send only "
                "the fields the judgement needs, not the whole record.")

    @contextmanager
    def _upstream():
        """Turn an upstream refusal into an actionable tool error."""
        try:
            yield
        except ValueError as e:
            # Client rubric guard (unknown type, missing `criteria`); names the question.
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))
        except (requests.ConnectionError, requests.Timeout) as e:
            raise _bad(f"Jev unreachable (network/timeout). Retry later. {e}")

    _releve = releve

    @mcp.tool()
    def jev_ask(state: dict, questions: dict, model: Optional[str] = None) -> dict:
        """Ask Jev typed questions about one state and get answers with probabilities.

        Jev is not a chat model: it returns a typed answer per
        question and nothing else — no prose, no justification, no tool calls. It pays
        off on BATCHES — many rows or profiles to triage with the same rubric (see
        `jev_items`). A single case you can judge yourself does not need Jev.

        ⚠️ The state is sent to a third party (OpenRouter, which passes it to TypeSafe):
        put in it only the fields the judgement needs, never a whole record or
        document.

        Put the WHOLE rubric in one call: the questions of one call are answered in
        parallel, each extra question costs about 48 input tokens, and the state is
        charged once. They cannot see each other's answers, so a question that depends
        on another's answer needs a second call.

        Returns — `{model, answers, usage}`:
            `answers[name]` is `{type: "noul", noul: 0.96}`, or
            `{type: "choice", choice, probabilities, confidence}`, or
            `{type: "score", score, probabilities, legend, confidence}`.
            `model` names the exact snapshot that answered — store it next to the
            answer, it is what makes a threshold reproducible.
            `usage` carries `input_tokens`, `output_tokens` (free) and `cost` in USD.

        A probability near 0.5 means "as likely as not", never "medium": pick two
        thresholds and leave the band between them to a human or to a stronger model.

        Args:
            state: what Jev judges — a flat or nested object (`{"title": …,
                "company": …}`), at most 16 KB of JSON. Send the fields that matter,
                not the whole record: the state is billed per token.
            questions: `{name: {type, instructions, criteria}}`.
                `noul` — criteria `{"true": …, "false": …}`, both described;
                `choice` — one entry per option, each described;
                `score` — an ORDERED list of levels, low to high.
                `criteria` is required.
            model: another model id (default: the pinned dated snapshot).
        """
        _etat_borne(state, "`state`")
        client = _client()
        with _upstream():
            r = client.decide(state, questions, model=model, timeout=LECTURE_S)
        _releve((r.get("usage") or {}).get("cost"))
        return {"model": r.get("model"), "answers": r.get("answers") or {},
                "usage": r.get("usage") or {}}

    @mcp.tool()
    def jev_items(items: list, questions: dict, model: Optional[str] = None,
                  parallel: Optional[int] = None) -> dict:
        """Ask Jev the SAME questions about many states in one call — nothing is written.

        The shape for "decide, then act": you have just read a page of rows or a list
        of profiles to triage with the same rubric, and you want a verdict per item
        before writing them, enriching them, or spending anything on them. Each item
        is judged on its own — one item's answer never depends on another's.

        ⚠️ Each state is sent to a third party (OpenRouter, which passes it to
        TypeSafe): put in it only the fields the judgement needs.

        Returns — `{model, answers, usage, failed, retry}`:
            `answers` — one entry per item, in the ORDER SENT, each
            `{index, answers: {…}}` exactly as `jev_ask` returns them, or
            `{index, error}` for an item without an answer (a state the upstream
            refused, a transient failure, or not sent in time). A failed item never
            becomes a false verdict.
            `usage` — `{decided, failed, input_tokens, cost}` for the whole call.
            `failed` — the number of items without an answer.
            `retry` — the indexes that were NOT SENT because the call ran out of time
            (about 40 s): send exactly those again. `[]` when everything was sent.

        A key problem (invalid key, no credits left) aborts the whole call rather than
        turning into N identical item errors; the answers already given are still
        billed, and the error says how many.

        Args:
            items: the states to judge, at most 200 per call and 16 KB of JSON each.
                Either the raw states, or `{"key": <your id>, "state": {…}}` to get
                your own id back beside each answer (`key` is echoed, never sent).
            questions: the rubric, same shape and same rules as `jev_ask`.
            model: another model id (default: the pinned dated snapshot).
            parallel: how many items in flight (default 10, max 50). Raise it for
                a big batch that must finish inside one call.
        """
        if not isinstance(items, list) or not items:
            raise _bad("`items`: at least one state is required.")
        if len(items) > MAX_ITEMS:
            raise _bad(f"`items`: {len(items)} states, max is {MAX_ITEMS} per call. "
                       "Split into pages.")
        try:
            fil = max(1, min(int(parallel or PARALLELE_DEFAUT), PARALLELE_MAX))
        except (TypeError, ValueError):
            raise _bad(f"`parallel`: expected an integer from 1 to {PARALLELE_MAX}, "
                       f"got {parallel!r}.")

        def _etat(i, x) -> tuple[Optional[str], dict]:
            if isinstance(x, dict) and "state" in x and isinstance(x["state"], dict):
                cle = x.get("key")
                cle, state = (str(cle) if cle is not None else None), x["state"]
            elif isinstance(x, dict):
                cle, state = None, x
            else:
                raise _bad("each `items` entry must be an object (the state) "
                           "or `{key, state}`.")
            _etat_borne(state, f"`items[{i}]`")
            return cle, state

        paires = [_etat(i, x) for i, x in enumerate(items)]
        client = _client(units=len(paires))
        # Check the rubric ONCE, not once per state.
        with _upstream():
            client.check_questions(questions)

        # Nothing starts after the window or a stop; in-flight calls finish and are billed.
        fin_depart = time.monotonic() + LOT_FENETRE_S
        arret = threading.Event()

        def _une(i: int, cle: Optional[str], state: dict) -> dict:
            if arret.is_set() or time.monotonic() >= fin_depart:
                return {"index": i, "key": cle, "_non_parti": True,
                        "error": "not sent (time budget or batch stopped) — send it again"}
            try:
                r = client.decide(state, questions, model=model, timeout=LECTURE_S)
                return {"index": i, "key": cle, "answers": r.get("answers") or {},
                        "_usage": r.get("usage") or {}, "_model": r.get("model")}
            except UpstreamHTTPError as e:
                # ⚠️ Key or balance problems stop the whole batch (not N identical errors).
                if e.status_code in (401, 402, 403):
                    raise
                return {"index": i, "key": cle, "error": _upstream_message(e)}
            except (requests.ConnectionError, requests.Timeout) as e:
                return {"index": i, "key": cle, "error": f"Jev unreachable: {e}"}

        res: list[dict] = []
        panne: Optional[Exception] = None
        try:
            with ThreadPoolExecutor(max_workers=fil) as ex:
                futurs = [ex.submit(_une, i, cle, state)
                          for i, (cle, state) in enumerate(paires)]
                for f in as_completed(futurs):
                    try:
                        res.append(f.result())
                    except Exception as e:  # noqa: SILENT — kept, re-raised below after billing
                        # Stop the batch; answers already given stay billed.
                        if panne is None:
                            panne = e
                            arret.set()
                            for autre in futurs:
                                autre.cancel()
        finally:
            # Whatever reached upstream is billed, even if the batch stops.
            cout = sum((r.get("_usage") or {}).get("cost") or 0 for r in res)
            _releve(cout)

        decides = sum(1 for r in res if "answers" in r)
        if panne is not None:
            deja = (f" {decides} answer(s) already given and billed before the stop."
                    if decides else " No answer had been given yet.")
            if isinstance(panne, UpstreamHTTPError):
                raise _bad(_upstream_message(panne) + deja) from panne
            raise panne

        res.sort(key=lambda r: r["index"])
        jetons = sum((r.get("_usage") or {}).get("input_tokens") or 0 for r in res)
        servi = next((r.get("_model") for r in res if r.get("_model")), None)
        rendus = []
        for r in res:
            ligne = {"index": r["index"]}
            if r.get("key") is not None:
                ligne["key"] = r["key"]
            if "error" in r:
                ligne["error"] = r["error"]
            else:
                ligne["answers"] = r["answers"]
            rendus.append(ligne)
        rates = sum(1 for r in rendus if "error" in r)
        return {"model": servi, "answers": rendus, "failed": rates,
                "retry": [r["index"] for r in res if r.get("_non_parti")],
                "usage": {"decided": len(rendus) - rates, "failed": rates,
                          "input_tokens": jetons, "cost": cout}}

    @mcp.tool()
    def jev_rows(datastore: str, questions: dict, state_fields: list, output: dict,
                 model_column: str, filter: Optional[dict] = None,
                 filters: Optional[list] = None, limit: int = jr.MAX_ROWS,
                 parallel: Optional[int] = None, dry_run: bool = False,
                 overwrite: bool = False, cursor: Optional[str] = None,
                 model: Optional[str] = None, background: bool = False) -> dict:
        """Qualify a table's rows with Jev ON THE SERVER: rows are read, judged and written
        back here, and no row data passes through you. Write the rubric; review the sample.

        Each row's `state_fields` are sent with the same `questions`; each answer is written
        to its `output` column, its confidence to `<column>_p` (choice and score), and the
        model snapshot to `model_column`. A `noul` writes its probability; a score writes
        the expected level (e.g. 3.34). A row Jev can never answer as is (state too large,
        or refused upstream as too long) or whose answer the table refuses to store gets
        `jev_error: <reason>` in `model_column`. A row whose state fields are all empty is
        counted in `empty_state` and left undecided: fill it, then re-run.
        Missing output, `<column>_p` and `model_column` columns are created, typed from
        the questions (choice → enum of its criteria); existing ones are never changed.
        The key and the rubric are checked before anything is created or written.

        Only undecided rows are taken (`model_column` empty) unless `overwrite=true`, so a
        re-run resumes and never pays twice. Rows leased by another run are skipped, and a
        row changed since it was read is never overwritten.

        A whole table: `background=true` (after a dry run). The call returns a `job_id`
        at once and the server judges every matching row itself — no cursor loop, and a
        dropped connection doesn't stop it. Follow it with `jev_job(op='status')`, stop
        it with `jev_job(op='cancel')`. The key, the write right and the quota are
        checked again before every slice: a revoked key or a spent quota stops the job,
        and what was already judged stays written and billed. One active job per table
        and `model_column`. A job keeps `_run_id` and bills its slices under it.

        Without `background`, one call judges what fits in its time window (~100 rows):
        loop on `next_cursor` until `done`, then finish with one pass WITHOUT cursor to
        pick up skipped rows. Nothing reserves rows between two calls: do not run two at
        once on the same rows.

        Any other upstream refusal (key, credits, a malformed request) stops the batch:
        what was already judged stays written and billed, and the error says how much.

        Run `dry_run=true` on known cases before a full run. Jev sees only the state and the
        questions: put client context (ICP, offer) in the criteria or the state. Rows with a
        thin state score low for lack of evidence (`thin_state` counts them). No bucketing
        here: thresholds belong to you.
        Validate fit against known outcomes before trusting it; combine Jev's answers with
        structured fields in plain code.

        ⚠️ Each state goes to a third party (OpenRouter, then TypeSafe). Billed on real cost,
        about 50 µ$ per row for 3 questions and a 500-character description.

        Returns — `{decided, jev_errors, empty_state, skipped_leased, skipped_changed,
        errors, error_count, low_confidence, thin_state, created_columns, next_cursor,
        remaining, done, cost, model, sample}`. `errors` and `sample` name rows by `_id`
        with codes and what was written — never a value read from the table. `dry_run`
        returns `rows` (each judged row's `_id` and answers) and `would_create_columns`,
        and writes nothing. `background` returns `{job_id, status, remaining,
        created_columns}`.

        Args:
            datastore: the table number (ns_id) or `slot:<name>`.
            questions: the rubric, same shape and rules as `jev_ask`; `criteria` required.
            state_fields: columns to send, each a name or `{name: max_chars}`.
            output: `{question: column}`; choice → text/enum, score and noul → number.
                Missing columns are created.
            model_column: text column for the model snapshot or `jev_error`; required
                (created if missing).
            filter: `data_rows` filter grammar.
            filters: `data_rows` multi-column clauses.
            limit: max rows judged in this call (default and max 500; dry run max 20).
                Ignored with `background`: the job takes every matching row.
            parallel: rows in flight (default 25, max 50).
            dry_run: judge and return, write nothing (still billed).
            overwrite: also re-judge rows that already have an answer.
            cursor: `next_cursor` from the previous call (not with `background`).
            model: another model id (default: the pinned dated snapshot).
            background: run the whole table as a server job; returns a `job_id`.
        """
        try:
            limite = int(limit)
            fil = max(1, min(int(parallel or jr.PARALLEL_DEFAULT), jr.PARALLEL_MAX))
        except (TypeError, ValueError):
            raise _bad("`limit` and `parallel` must be integers.")
        if background and dry_run:
            raise _bad("`background` and `dry_run` don't go together: run the dry run "
                       "first, then the job. Nothing sent.")
        if background and cursor:
            raise _bad("`background` takes no `cursor`: the job walks the whole table "
                       "itself. Nothing sent.")
        limite = max(1, min(limite, jr.MAX_DRY_RUN if dry_run else jr.MAX_ROWS))
        plan = preparer(datastore, questions=questions, state_fields=state_fields,
                        output=output, model_column=model_column, filter=filter,
                        filters=filters, overwrite=overwrite, model=model, fil=fil,
                        dry_run=dry_run)
        if background:
            return _deposer(plan)
        out, panne = qualifier(plan, cursor=cursor, limite=limite,
                               fenetre_s=LOT_FENETRE_S, relever=_releve)
        if panne is not None:
            raise _refus_de_panne(panne, out["decided"])
        out.pop("_epuise", None)
        if dry_run:
            # `legend` dropped from each answer here, at the reply (it repeats the criteria).
            out["rows"] = [{"_id": r["_id"], "answers": jr.answers_slim(r["answers"])}
                           for r in out["rows"]]
        return out

    @mcp.tool()
    def jev_job(op: str, job_id: int) -> dict:
        """Follow or stop a `jev_rows(background=true)` job.

        `op='status'` — where the job is: `{job_id, status, decided, jev_errors,
        empty_state, skipped_leased, skipped_changed, low_confidence, thin_state,
        remaining, cost, errors, error_count, error, slices, model, created_at,
        updated_at, finished_at}`. `status` is `pending`, `running`, `done`, `failed`
        (`error` says why; what was judged stays written and billed) or `cancelled`.
        `remaining` = undecided rows matching the job, as of its last slice. Poll every
        minute or so, not faster: a slice takes up to two minutes.
        `op='cancel'` — stop the job. One with no slice in flight stops at once; one
        mid-slice stops when that slice ends (rows it judged stay written and billed).
        Relaunching `jev_rows(background=true)` later resumes on the undecided rows.

        Args:
            op: `status` or `cancel`.
            job_id: the `job_id` returned by `jev_rows(background=true)`.
        """
        from ..db import jev_jobs as db_jev
        if op not in ("status", "cancel"):
            raise _bad("`op`: `status` or `cancel`.")
        job = _job_de_l_appelant(job_id)
        if op == "cancel":
            db_jev.cancel_jev_job(int(job["id"]))
            job = db_jev.get_jev_job(int(job["id"])) or job
        return etat_du_job(job)


# ── jev_rows: the core shared by the call and the background job ───────────────────────


class Plan:
    """A `jev_rows` call checked against its table: what every slice needs."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def preparer(datastore: Any, *, questions: dict, state_fields: list, output: dict,
             model_column: str, filter: Optional[dict], filters: Optional[list],
             overwrite: bool, model: Optional[str], fil: int, dry_run: bool) -> Plan:
    """Check the call against the table, the key, the model and the rubric, then create
    the missing output columns (never in a dry run). Raises the tool's refusals.

    ⚠️ Called again before every background slice: it is what re-checks the write
    right, the key (a revoked grant) and the rubric under the job's identity."""
    from oto.tools.jev.client import DEFAULT_MODEL

    from ..datastore import jetons
    from ..datastore import par_reference as pr
    from ..datastore.core import DatastoreNotFound, DatastoreReadOnly
    try:
        store, adresse = pr.tableau(datastore, ecrire=not dry_run)
        schema = store.get_schema(adresse)
    except jetons.JetonMalPlace as e:
        raise _bad(str(e))
    except DatastoreNotFound:
        raise _bad(f"Table `{datastore}` not found. Nothing sent.")
    except DatastoreReadOnly:
        raise _bad(f"Table `{datastore}` is shared read-only. Nothing sent.")
    fields = jr.fields_of(schema)
    if not fields:
        raise _bad("`jev_rows` needs a declared schema on the table. Nothing sent.")
    # Missing output columns are created (typed from the questions); existing ones
    # are checked, never altered.
    a_creer = jr.missing_columns(questions, output, model_column, fields)
    try:
        cols = jr.check_state_fields(state_fields, fields, jr.hidden_of(schema))
        jr.check_outputs(questions, output, model_column,
                         {**fields, **{f["key"]: f for f in a_creer}},
                         jr.closed_of(schema))
    except jr.Refusal as e:
        raise _bad(str(e))
    # ⚠️ The key, the model and the rubric BEFORE any side effect: a refused call
    # must leave the schema and the rows as they were. Each page re-checks the quota
    # for what it will send.
    client = client_partage(units=1)
    with _vers_refus():
        client.check_model(model or DEFAULT_MODEL)
        client.check_questions(questions)
    if a_creer and not dry_run:
        try:
            store.patch_schema(adresse, fields=a_creer)
        except ValueError as e:
            raise _bad(f"Could not create the output columns: {e}. Nothing sent.")

    clauses = list(filters or [])
    # In a dry run a column still to be created is empty everywhere: no clause.
    if not overwrite and not (dry_run and model_column in {f["key"] for f in a_creer}):
        clauses.append({"field": model_column, "op": "empty", "value": True})
    proj = [c for c, _ in cols] + ["_revision", "_claimed_by", "_claimed_until",
                                    "_claimed_run"]
    return Plan(store=store, adresse=adresse, cols=cols, a_creer=a_creer,
                clauses=clauses, proj=proj, questions=questions, output=output,
                model_column=model_column, filter=filter, filters=filters,
                state_fields=state_fields, overwrite=overwrite, model=model, fil=fil,
                dry_run=dry_run)


@contextmanager
def _vers_refus():
    """`check_model`/`check_questions` refusals → the tool's refusal."""
    try:
        yield
    except ValueError as e:
        raise _bad(str(e))


def _refus_de_panne(panne: Exception, decided: int) -> Exception:
    """A batch stopped upstream → the error to raise, saying what was already billed."""
    from oto.tools.common.errors import UpstreamHTTPError
    deja = f" {decided} row(s) already written and billed."
    if isinstance(panne, UpstreamHTTPError):
        e = _bad(_upstream_message(panne) + " Batch stopped." + deja)
        e.__cause__ = panne
        return e
    return panne


def qualifier(plan: Plan, *, cursor: Optional[str], limite: int, fenetre_s: float,
              relever, passer_les_echecs: bool = False) -> tuple[dict, Optional[Exception]]:
    """Judge and write up to `limite` rows from `cursor`, sending nothing after
    `fenetre_s`. `relever(cost)` records the spend, ALWAYS (even when it raises).

    `passer_les_echecs` (the background job): a row whose call failed upstream (5xx,
    429, unreachable) counts as handled, so the cursor moves past it. Otherwise the
    watermark stops on it and, with `overwrite`, every later row of the page would be
    judged — and billed — again on the next slice. The job's last pass retries it.

    Returns `(result, panne)`: `panne` = the upstream refusal that stopped the batch
    (key, credits, malformed request), what was judged before it stays written."""
    from oto.tools.common.errors import UpstreamHTTPError

    from ..datastore import par_reference as pr
    from ..datastore.errors import InvalidCursor
    from ..datastore.outils import _encode_cursor

    store, adresse, cols = plan.store, plan.adresse, plan.cols
    questions, output, model_column = plan.questions, plan.output, plan.model_column
    dry_run, model, filter, clauses = plan.dry_run, plan.model, plan.filter, plan.clauses
    fin_depart = time.monotonic() + fenetre_s
    arret = threading.Event()
    client = None

    # Per row, in table order: True once handled (written, marked or skipped).
    ordre: list[str] = []
    fait: dict[str, bool] = {}
    n = {"decided": 0, "jev_errors": 0, "empty_state": 0, "skipped_leased": 0,
         "skipped_changed": 0, "low_confidence": 0, "thin_state": 0}
    # `{_id, code}` only: a refusal's text can quote a cell, and this reply never does.
    erreurs: list[dict] = []
    sample: list[dict] = []
    judged: list[dict] = []
    cout = 0.0
    servi = None
    panne: Optional[Exception] = None
    page_cursor, epuise = cursor, False

    def _ecrire(rid: str, rev, patch: dict) -> Optional[str]:
        """Write one row by id on THIS thread (journal stamps). Code or None."""
        return pr.ecrire_ligne(store, adresse, rid, patch, expected_revision=rev)

    def _marquer(rid: str, rev, raison: str) -> None:
        code = _ecrire(rid, rev, {model_column: (jr.ERROR_PREFIX + raison)[:500]})
        if code is None:
            n["jev_errors"] += 1
        _issue(rid, code)

    def _issue(rid: str, code: Optional[str]) -> None:
        if code == "row_locked":
            n["skipped_leased"] += 1
        elif code == "row_changed":
            n["skipped_changed"] += 1
        elif code and code != "row_not_found":
            erreurs.append({"_id": rid, "code": code})
        fait[rid] = True

    def _decide(state: dict) -> dict:
        if arret.is_set() or time.monotonic() >= fin_depart:
            return {"_non_parti": True}
        try:
            return client.decide(state, questions, model=model, timeout=LECTURE_S)
        except UpstreamHTTPError as e:
            refus = jr.state_refusal(e.status_code, getattr(e, "body", ""))
            # ⚠️ Key, balance or request problems stop the whole batch; only a 400
            # about THIS state is the row's own.
            if e.status_code in (401, 402, 403) or (e.status_code == 400
                                                    and refus is None):
                raise
            return {"_code": f"upstream_{e.status_code}", "_refus": refus}
        except (requests.ConnectionError, requests.Timeout):
            return {"_code": "upstream_unreachable", "_refus": None}

    try:
        while len(ordre) < limite and not epuise and not arret.is_set() \
                and time.monotonic() < fin_depart:
            try:
                page = store.cursor_rows(adresse, filter=filter or None,
                                         filters=clauses or None,
                                         limit=min(jr.PAGE, limite - len(ordre)),
                                         cursor=page_cursor, fields=plan.proj)
            except InvalidCursor:
                raise _bad("`cursor` is not a `jev_rows` cursor.")
            except ValueError as e:
                raise _bad(f"`filter`: {e}")
            rows = page.get("rows") or []
            page_cursor = page.get("next_cursor")
            epuise = not page_cursor
            envoi: list[tuple[str, Any, dict]] = []
            for row in rows:
                rid = str(row["_id"])
                ordre.append(rid)
                fait[rid] = False
                if pr.tenue_ailleurs(row):
                    _issue(rid, "row_locked")
                    continue
                state = jr.state_of(row, cols)
                rev = row.get("_revision")
                if not state:
                    # Not the row's verdict: its fields may be filled later.
                    n["empty_state"] += 1
                    fait[rid] = True
                    continue
                taille = len(json.dumps(state, ensure_ascii=False,
                                        default=str).encode("utf-8"))
                if taille > MAX_ETAT_OCTETS:
                    if not dry_run:
                        _marquer(rid, rev, f"state is {taille} bytes, max {MAX_ETAT_OCTETS}")
                    else:
                        fait[rid] = True
                    continue
                if jr.is_thin(state, cols[0][0]):
                    n["thin_state"] += 1
                envoi.append((rid, rev, state))
            if not envoi:
                continue
            # Quota is checked per page, for what this page will send.
            client = client_partage(units=len(envoi))
            # Workers only make the HTTP call; reads and writes stay on this thread.
            with ThreadPoolExecutor(max_workers=plan.fil) as ex:
                futurs = {ex.submit(_decide, st): (rid, rev)
                          for rid, rev, st in envoi}
                for f in as_completed(futurs):
                    rid, rev = futurs[f]
                    try:
                        r = f.result()
                    except Exception as e:  # noqa: SILENT — kept, returned after billing
                        if panne is None:
                            panne = e
                            arret.set()
                            for autre in futurs:
                                autre.cancel()
                        continue
                    if r.get("_non_parti"):
                        continue          # not sent: stays undecided, not handled
                    if "_code" in r:
                        if r["_refus"] and not dry_run:
                            _marquer(rid, rev, f"state refused upstream ({r['_refus']})")
                        else:
                            erreurs.append({"_id": rid, "code": r["_code"]})
                            if passer_les_echecs:
                                fait[rid] = True
                        continue
                    cout += (r.get("usage") or {}).get("cost") or 0
                    servi = servi or r.get("model")
                    answers = r.get("answers") or {}
                    try:
                        patch, conf = jr.patch_of(answers, output, model_column,
                                                  r.get("model") or "")
                    except (KeyError, TypeError):
                        if not dry_run:
                            _marquer(rid, rev, "incomplete answer")
                        continue
                    n["low_confidence"] += sum(1 for c in conf if c < jr.LOW_CONFIDENCE)
                    if dry_run:
                        judged.append({"_id": rid, "answers": answers})
                        fait[rid] = True
                        continue
                    code = _ecrire(rid, rev, patch)
                    if code == "writeback_refused":
                        # Paid once: marked, so no later pass judges it again.
                        erreurs.append({"_id": rid, "code": code})
                        _marquer(rid, rev, "answer refused by the table schema")
                        continue
                    if code is None:
                        n["decided"] += 1
                        if len(sample) < 5:
                            sample.append({"_id": rid, "written": patch})
                    _issue(rid, code)
    finally:
        relever(cout)

    if panne is not None:
        return ({**n, "errors": erreurs[:20], "error_count": len(erreurs), "cost": cout,
                 "model": servi}, panne)

    # Watermark: the last row with every earlier row handled.
    borne = None
    for rid in ordre:
        if not fait.get(rid):
            break
        borne = rid
    overwrite = plan.overwrite
    remaining = (None if overwrite or dry_run
                 else store.count_rows(adresse, filter=filter or None,
                                       filters=clauses or None))
    tout_fait = all(fait.get(r) for r in ordre)
    if remaining is None:
        done = epuise and tout_fait
    else:
        # A full pass (no cursor) that read and handled every undecided row: the
        # only ones left wait for their state, so re-running would change nothing.
        done = remaining == 0 or (cursor is None and epuise and tout_fait
                                  and remaining <= n["empty_state"])
    next_cursor = None if done else (_encode_cursor(borne) if borne else cursor)
    creees = [f["key"] for f in plan.a_creer]
    out = {**n, "errors": erreurs[:20], "error_count": len(erreurs),
           **({"would_create_columns": creees} if dry_run
              else {"created_columns": creees}),
           "next_cursor": next_cursor, "remaining": remaining,
           "done": done, "cost": cout, "model": servi, "dry_run": dry_run,
           # For the background job: the pass read to the end of the table.
           "_epuise": epuise}
    if dry_run:
        out["rows"] = judged
    else:
        out["sample"] = sample
    return out, None


# ── jev_rows(background=true): the job ─────────────────────────────────────────────────

#: What a job keeps of the call: everything `preparer` needs again on every slice.
PARAMS_DU_JOB = ("questions", "state_fields", "output", "model_column", "filter",
                 "filters", "overwrite", "model", "fil")


def _deposer(plan: Plan) -> dict:
    """Queue the job (columns already created by `preparer`) and return at once."""
    from .. import access
    from ..db import jev_jobs as db_jev
    sub = access.current_user_sub_or_raise()
    # The table's NUMBER, whatever the call named it by: by name and by number it is
    # the same table for the one-active-job rule, and a name stops resolving one day.
    ns_id = plan.store._resolve(plan.adresse, write=True)
    remaining = (None if plan.overwrite else
                 plan.store.count_rows(plan.adresse, filter=plan.filter or None,
                                       filters=plan.clauses or None))
    try:
        jid = db_jev.create_jev_job(
            org_id=access.current_org(sub), sub=sub,
            project_id=session_org.current_call_project(),
            run_id=session_org.current_call_or_session_run(), adresse=str(ns_id),
            model_column=plan.model_column, remaining=remaining,
            params={k: getattr(plan, k) for k in PARAMS_DU_JOB})
    except db_jev.JevJobActive as e:
        raise _bad(f"Job {e.job_id} is already running on this table and "
                   f"`{plan.model_column}`: follow it with `jev_job(op='status', "
                   f"job_id={e.job_id})`, or cancel it first. Nothing sent.")
    return {"job_id": jid, "status": "pending", "remaining": remaining,
            "created_columns": [f["key"] for f in plan.a_creer],
            "next": f"jev_job(op='status', job_id={jid})"}


def _job_de_l_appelant(job_id: Any) -> dict:
    """The job, if the caller launched it or belongs to its org. Another org's job and a
    missing one get the same answer: a job id reveals nothing."""
    from .. import access, roles
    from ..db import jev_jobs as db_jev
    try:
        jid = int(job_id)
    except (TypeError, ValueError):
        raise _bad("`job_id` must be an integer.")
    sub = access.current_user_sub_or_raise()
    job = db_jev.get_jev_job(jid)
    if job is not None and (job["sub"] == sub or (
            job["org_id"] is not None and roles.is_org_member(sub, int(job["org_id"])))):
        return job
    raise _bad(f"No `jev_rows` job {jid} in your organizations.")


def etat_du_job(job: dict) -> dict:
    c = job.get("counters") or {}
    compte = {k: int(c.get(k) or 0) for k in (
        "decided", "jev_errors", "empty_state", "skipped_leased", "skipped_changed",
        "low_confidence", "thin_state")}
    quand = {k: (v.isoformat() if hasattr(v, "isoformat") else v)
             for k in ("created_at", "updated_at", "finished_at") for v in [job.get(k)]}
    return {"job_id": int(job["id"]), "status": job["status"], **compte,
            "remaining": job.get("remaining"), "cost": float(job.get("cost") or 0),
            "errors": job.get("errors") or [], "error_count": job.get("error_count") or 0,
            "error": job.get("error"), "slices": job.get("slices") or 0,
            "model": job.get("model"), "cancel_requested": bool(job.get("cancel_requested")),
            **quand}
