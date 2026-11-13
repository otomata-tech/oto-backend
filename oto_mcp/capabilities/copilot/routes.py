"""The in-app agent's five routes (REST only — the front's panel; an MCP client has its own
agent). Three answer with an event stream (`EventStream`, `text/event-stream`), one —
undo — with JSON. Contract: the front's `src/lib/copilot/protocol.ts` and `transport.ts`.

Who may use it: an org member, in the org they view (`X-Oto-Org`), whose `sub` is in the
instance's allowlist `OTO_COPILOT_SUBS` — empty by default, so nobody until an operator
names someone; `*` admits every account (the other gates below still apply). Never while viewing as someone else. **An interactive session only**: an
`oto_` token — a person's, a delegated one, an org key —, a worker secret or a service
identity is refused (403 `api_token_forbidden`), twice: by the authentication
(`RestBinding.allow_api_token=False`) and here, on the request's source (`geste`). A
token would turn the panel into a model endpoint paid by the instance, driven by a script.
And never past the account's daily spend ceiling (429 `copilot_daily_cap`, `depense.py`).

Every refusal that happens once the stream is open comes as an `error` event followed
by `done`, inside a 200: the panel shows it in the conversation. Only the refusals that
precede the stream are HTTP statuses (403 for the gate and the view-as, never a 404 — a
404 on the turn route reads, to the panel, as "no backend deployed").
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Annotated, Any, Literal, Optional

import anyio
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ... import geste, session_org
from ...db import copilot as store
from .._authz import ORG_MEMBER
from .._types import (AuthzDenied, Capability, DeclaredError, EventStream, ResolvedCtx,
                      RestBinding)
from ..registry import CAPABILITIES
from . import (annulation, boucle, cle as cles, compactage, consigne, depense, dictee, outils,
               palier, pieces)
from .boucle import ev

logger = logging.getLogger(__name__)

#: Conversations a person may have touched in the last `_FENETRE_MIN` minutes.
MAX_CONVERSATIONS = 20
_FENETRE_MIN = 30
#: An idle conversation's run is closed after this long (lazily, on the person's next turn).
INACTIVITE_MIN = 30
#: Beyond this, the conversation is too long to resend: start a new one.
MAX_HISTOIRE_CARACTERES = 4_000_000
_SSE = "text/event-stream"


#: In `OTO_COPILOT_SUBS`: every account (still a console session, a member of the org,
#: never viewing as someone else).
TOUS = "*"


def autorises() -> frozenset:
    """The subs allowed to use the in-app agent (`OTO_COPILOT_SUBS`, comma-separated).
    Read on each call. Empty = nobody; `*` among them = everyone (`admis`)."""
    brut = os.environ.get("OTO_COPILOT_SUBS", "")
    return frozenset(s.strip() for s in brut.split(",") if s.strip())


def admis(sub: str) -> bool:
    """Is `sub` in the allowlist — named, or by `*`?"""
    liste = autorises()
    return TOUS in liste or sub in liste


def _garde(ctx: ResolvedCtx) -> None:
    g = geste.courant()
    if g is None or g.source != geste.CONSOLE:
        # Fail-closed: no request source known is not a session.
        raise AuthzDenied(403, "api_token_forbidden",
                          "The in-app agent answers an interactive session only: an API "
                          "or delegated token cannot use it.")
    if session_org.current_view_as_bound_org() is not None:
        raise AuthzDenied(403, "copilot_view_as",
                          "The in-app agent is off while viewing as someone else.")
    if not admis(ctx.sub):
        raise AuthzDenied(403, "copilot_not_enabled",
                          "The in-app agent is not enabled for this account.")


# --- inputs and the event shape -------------------------------------------------------

class ClientTool(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    description: str = Field(default="", max_length=4000)
    input_schema: dict = Field(default_factory=lambda: {"type": "object"})


class TurnInput(BaseModel):
    thread_id: Optional[str] = Field(default=None, max_length=80)
    text: str = Field(min_length=1, max_length=20_000)
    input: Literal["text", "voice"] = "text"
    mode: Literal["ask", "act"] = "ask"
    page_context: dict = Field(default_factory=dict)
    client_tools: list[ClientTool] = Field(default_factory=list, max_length=40)
    #: Read the final answer aloud: one `audio` event before `done` (`voix.py`), or
    #: none when it cannot be — the panel then uses the browser's voice.
    speak: bool = False
    #: Files attached to this message (`att_…` ids from `POST /api/copilot/attachments`),
    #: the caller's own in this org — else 400 `unknown_attachment` (`pieces.py`).
    attachments: list[Annotated[str, Field(max_length=80)]] = Field(
        default_factory=list, max_length=pieces.MAX_PAR_TOUR)


class ClientResultInput(BaseModel):
    thread_id: str = Field(max_length=80)
    call_id: str = Field(max_length=200)
    ok: bool
    result: Any = None
    error: Optional[str] = Field(default=None, max_length=4000)
    client_tools: Optional[list[ClientTool]] = Field(default=None, max_length=40)
    #: As on the turn: read the answer this leg ends with aloud.
    speak: bool = False


class ApprovalInput(BaseModel):
    thread_id: str = Field(max_length=80)
    approval_id: str = Field(max_length=200)
    decision: Literal["approve", "reject"]
    edits: Optional[dict[str, str]] = None
    remember: Optional[bool] = None
    #: As on the turn: read the answer this leg ends with aloud.
    speak: bool = False


class RevertInput(BaseModel):
    revert_id: str = Field(max_length=80)


class DictationInput(BaseModel):
    #: The clip the panel recorded (`MediaRecorder`), base64. 3 MB of audio at most
    #: (`dictee.MAX_OCTETS`, 4,000,000 base64 characters), checked by `dictee.decoder`
    #: (422 `dictation_invalid`); this field's bound stays under the body bound (4 MiB,
    #: `bornes_processus.CORPS_OCTETS`) so that check is reached rather than a 413.
    audio_base64: str = Field(min_length=1, max_length=4_100_000)
    mime_type: str = Field(max_length=100)
    #: How long the person spoke, as the panel timed it: what the quota is checked
    #: against before ElevenLabs is called.
    duration_ms: int = Field(gt=0, le=dictee.MAX_SECONDES * 1000)
    language: Optional[Literal["en", "fr"]] = None


class DictationOut(BaseModel):
    text: str


class CopilotEvent(BaseModel):
    """ONE event of the stream (`data: <json>`, blank line between two). `type` ∈
    thread, text_delta, progress, tool_call, tool_result, approval_required,
    client_tool_call, suggestions, audio, done, error; the other fields per type, as the
    front's `CopilotEvent` declares them. `thread` comes first on a turn; `done` comes
    last. `audio` (only when the request asked `speak`): the final answer read aloud —
    `url` (signed, temporary), `content_type`, `chars` (characters read)."""
    type: str
    thread_id: Optional[str] = None
    text: Optional[str] = None
    id: Optional[str] = None
    name: Optional[str] = None
    label: Optional[str] = None
    write: Optional[bool] = None
    ok: Optional[bool] = None
    summary: Optional[str] = None
    changed: Optional[dict] = None
    revert_id: Optional[str] = None
    title: Optional[str] = None
    steps: Optional[list[dict]] = None
    destructive: Optional[bool] = None
    editable: Optional[list[dict]] = None
    rememberable: Optional[bool] = None
    args: Optional[dict] = None
    items: Optional[list[str]] = None
    message: Optional[str] = None
    url: Optional[str] = None
    content_type: Optional[str] = None
    chars: Optional[int] = None


class AttachmentTypes(BaseModel):
    #: Media type → largest accepted file, in bytes.
    types: dict[str, int]
    #: Files one message may carry.
    max_per_turn: int


class RevertOut(BaseModel):
    ok: bool
    undone: str
    #: Rows or cells left alone because someone changed them since.
    skipped: int


# --- the stream -------------------------------------------------------------------------

async def _sse(travail):
    """Events as SSE bytes. A failure after the 200 is said IN the stream."""
    try:
        async for e in travail:
            yield f"data: {json.dumps(e, ensure_ascii=False, default=str)}\n\n".encode()
    except cles.SansCle as e:
        yield f"data: {json.dumps(ev('error', message=e.message))}\n\n".encode()
        yield f"data: {json.dumps(ev('done'))}\n\n".encode()
    except Exception:
        logger.exception("copilot : étape en échec")
        yield f"data: {json.dumps(ev('error', message='Something went wrong on our side; try again.'))}\n\n".encode()
        yield f"data: {json.dumps(ev('done'))}\n\n".encode()


def _fil(ligne: dict) -> dict:
    fil = dict(ligne)
    for k in ("messages", "queue", "results", "remembered", "client_tools"):
        fil[k] = list(fil.get(k) or [])
    return fil


def _projet(page_context: dict) -> Optional[int]:
    brut = str(((page_context or {}).get("params") or {}).get("projectId") or "")
    return int(brut) if re.fullmatch(r"\d{1,18}", brut) else None


def _run_ctx(fil: dict) -> ResolvedCtx:
    return ResolvedCtx(sub=fil["sub"], org_id=fil["org_id"])


def _fermer_inactives(sub: str) -> None:
    """Closes the runs of this person's idle conversations (lazily, off the loop)."""
    from ..run_thread import RunClotureInput, _clore
    for ligne in store.inactives_avec_run(sub, INACTIVITE_MIN):
        try:
            _clore(ResolvedCtx(sub=sub, org_id=ligne["org_id"]),
                   RunClotureInput(run_id=ligne["run_id"], outcome="done",
                                   note="In-app agent conversation idle."))
        except AuthzDenied as e:
            logger.warning("copilot : run %s non clos (%s)", ligne["run_id"], e.code)
        store.oublier_run(ligne["id"], ligne["run_id"])


def _ouvrir_run(fil: dict, texte: str) -> None:
    from ..run_thread import RunOuvertureInput, _ouvrir
    out = _ouvrir(_run_ctx(fil), RunOuvertureInput(label=f"In-app agent: {texte[:60]}"))
    fil["run_id"] = out["run_id"]


async def _etape(fil: dict, b, avant) -> Any:
    """One leg under the conversation's lock: `avant(fil)` applies what the request
    brings, then the loop runs. The lock is a row of the base (`store.prendre`)."""
    jeton = uuid.uuid4().hex
    if not await run_in_threadpool(store.prendre, fil["id"], jeton, boucle.VERROU_S):
        yield ev("error", message="Still working on the previous step.")
        yield ev("done")
        return
    fil["_verrou"] = jeton
    relu = False
    try:
        frais = await run_in_threadpool(store.lire, fil["id"], fil["sub"])
        fil.update(_fil(frais or {}))
        relu = frais is not None
        erreur = avant(fil)
        if erreur:
            yield ev("error", message=erreur)
            yield ev("done")
            return
        if len(json.dumps(fil["messages"], default=str)) > MAX_HISTOIRE_CARACTERES:
            yield ev("error", message="This conversation is too long: start a new one.")
            yield ev("done")
            return
        cle = await run_in_threadpool(cles.resoudre, fil["org_id"], fil["sub"])
        if not fil.get("run_id"):
            await run_in_threadpool(_ouvrir_run, fil, "conversation resumed")
        await boucle.sauver(fil)
        async for e in boucle.conduire(fil, b, cle):
            yield e
    finally:
        # A closed tab cancels the leg at a `yield`: what was decided before it (a card
        # now waiting, a result of a call that ran) is written back anyway, or the next
        # answer would find nothing waiting — and a write that ran would be replayed.
        with anyio.CancelScope(shield=True):
            if relu:
                try:
                    await boucle.sauver(fil)
                except Exception:
                    logger.exception("copilot : état de la conversation %s non enregistré",
                                     fil["id"])
            await run_in_threadpool(store.rendre, fil["id"], jeton)


async def _tour(ctx: ResolvedCtx, inp: TurnInput, marques: list[dict], canal: str = "app"):
    """A turn of the person's message. `canal` names where the conversation lives — `app`
    (the panel) or `slack` (`slack_copilot.py`, which drives this same function): it only
    changes the prompt a NEW conversation is born with."""
    sub, org = ctx.sub, ctx.org_id
    await run_in_threadpool(_fermer_inactives, sub)
    ligne = None
    if inp.thread_id:
        ligne = await run_in_threadpool(store.lire, inp.thread_id, sub)
        if ligne is not None and ligne.get("org_id") != org:
            ligne = None    # another org's conversation is not continued here
    b = await outils.boite(sub, org)
    projet = _projet(inp.page_context)
    if ligne is None:
        if await run_in_threadpool(store.ouvertes_recentes, sub, _FENETRE_MIN) \
                >= MAX_CONVERSATIONS:
            yield ev("error", message="Too many open conversations: close a tab, or wait "
                     f"{_FENETRE_MIN} minutes.")
            yield ev("done")
            return
        contexte = ""
        if "oto_context" in b.outils:
            issue = await outils.executer(b, sub, "oto_context", {}, org=org, projet=projet,
                                          run_id=None)
            contexte = issue.texte if issue.ok else ""
        thread_id = f"thr_{uuid.uuid4().hex}"
        await run_in_threadpool(store.creer, thread_id, sub, org, projet,
                                consigne.consigne(contexte, canal), palier.defaut())
        fil = _fil(await run_in_threadpool(store.lire, thread_id, sub))
        await run_in_threadpool(_ouvrir_run, fil, inp.text)
        await run_in_threadpool(store.enregistrer, thread_id, {"run_id": fil["run_id"]})
    else:
        fil = _fil(ligne)
    yield ev("thread", thread_id=fil["id"])
    inactif_s = compactage.inactif_depuis((ligne or {}).get("last_seen"))

    def avant(f: dict) -> Optional[str]:
        # A conversation back after the cache's 5 minutes is re-written whole anyway: the
        # long results of its earlier questions leave the history now (`compactage`).
        if compactage.est_froid(inactif_s):
            libres = compactage.compacter(f["messages"], b.annotations)
            if libres:
                logger.info("copilot : conversation %s reprise à froid, %s caractères de "
                            "résultats anciens retirés", f["id"], libres)
        # Per leg, never written back (`boucle.sauver` keeps a fixed list of fields).
        f["_speak"] = inp.speak
        f["mode"] = inp.mode
        f["client_tools"] = [t.model_dump() for t in inp.client_tools]
        if projet is not None:
            f["project_id"] = projet
        boucle.regler_interrompus(f, boucle.ECHEC_ARRETE)
        f["messages"].append({"role": "user", "content": [
            {"type": "text", "text": "<page_context>" + json.dumps(
                inp.page_context, ensure_ascii=False, default=str)[:60_000]
             + "</page_context>"},
            # The files: a small marker each, resolved per request (`pieces.resoudre`).
            *marques,
            {"type": "text", "text": inp.text}]})
        return None

    async for e in _etape(fil, b, avant):
        yield e


async def _reprise(ctx: ResolvedCtx, thread_id: str, avant):
    ligne = await run_in_threadpool(store.lire, thread_id, ctx.sub)
    if ligne is None or ligne.get("org_id") != ctx.org_id:
        yield ev("error", message="This conversation was not found. Start a new one.")
        yield ev("done")
        return
    b = await outils.boite(ctx.sub, ctx.org_id)
    async for e in _etape(_fil(ligne), b, avant):
        yield e


def _parler(fil: dict, speak: bool, erreur: Optional[str]) -> Optional[str]:
    """A resumed leg: `speak` for this leg only, then what the answer to apply said."""
    fil["_speak"] = speak
    return erreur


async def _plafond(ctx: ResolvedCtx) -> None:
    """Refused before the stream opens when the account has reached today's ceiling
    (the loop checks again before each model call)."""
    if await run_in_threadpool(depense.atteint, ctx.sub):
        raise AuthzDenied(429, "copilot_daily_cap", depense.message_de_refus())


async def _turn(ctx: ResolvedCtx, inp: TurnInput) -> EventStream:
    _garde(ctx)
    await _plafond(ctx)
    marques: list[dict] = []
    if inp.attachments:
        marques = await run_in_threadpool(pieces.marques, inp.attachments, ctx.sub,
                                          ctx.org_id)
        if marques is None:
            raise AuthzDenied(400, "unknown_attachment",
                              "An attached file was not found: attach it again.")
    return EventStream(_sse(_tour(ctx, inp, marques)))


def _attachment_types(ctx: ResolvedCtx, inp: BaseModel) -> dict:
    _garde(ctx)
    return pieces.types_acceptes()


class _Rien(BaseModel):
    pass


async def _client_result(ctx: ResolvedCtx, inp: ClientResultInput) -> EventStream:
    _garde(ctx)
    await _plafond(ctx)
    outils_client = ([t.model_dump() for t in inp.client_tools]
                     if inp.client_tools is not None else None)
    return EventStream(_sse(_reprise(ctx, inp.thread_id, lambda f: _parler(
        f, inp.speak, boucle.repondre_page(f, inp.call_id, inp.ok, inp.result, inp.error,
                                           outils_client)))))


async def _approval(ctx: ResolvedCtx, inp: ApprovalInput) -> EventStream:
    _garde(ctx)
    await _plafond(ctx)
    return EventStream(_sse(_reprise(ctx, inp.thread_id, lambda f: _parler(
        f, inp.speak, boucle.repondre_carte(f, inp.approval_id, inp.decision == "approve",
                                            inp.edits, bool(inp.remember))))))


def revert_pour(sub: str, revert_id: str) -> dict:
    """Undoes one change for `sub`, once: what `POST /api/copilot/actions/{id}/revert` does,
    without the REST gate — the Slack button (`slack_copilot.py`) is another door to the
    same undo, and checks its own caller. Raises `AuthzDenied` 404/409 like the route."""
    ligne = store.consommer(revert_id, sub)
    if ligne is None:
        raise AuthzDenied(404, "nothing_to_undo",
                          "Nothing to undo: already undone, expired, or not yours.")
    jeton = session_org.set_call_org(int(ligne["org_id"])) if ligne.get("org_id") else None
    try:
        out = annulation.defaire(sub, ligne)
    except Exception:
        # The store's own words stay in the log: they can name a table, a column or a
        # rule of another account's data. The client gets the named refusal.
        logger.exception("copilot : annulation %s refusée", revert_id)
        raise AuthzDenied(409, "undo_failed",
                          "Undo failed: the change could not be put back, or only in "
                          "part. Check the table before trying again.") from None
    finally:
        if jeton is not None:
            session_org.reset_call_org(jeton)
    return {"ok": True, "undone": ligne.get("label") or "", "skipped": int(out["skipped"])}


def _revert(ctx: ResolvedCtx, inp: RevertInput) -> dict:
    _garde(ctx)
    return revert_pour(ctx.sub, inp.revert_id)


async def _dictation(ctx: ResolvedCtx, inp: DictationInput) -> dict:
    _garde(ctx)
    try:
        data, mime = dictee.decoder(inp.audio_base64, inp.mime_type)
    except dictee.DicteeRefusee as e:
        raise AuthzDenied(422, "dictation_invalid", str(e)) from None
    out = await dictee.transcrire(ctx.sub, ctx.org_id, data, mime, inp.duration_ms / 1000,
                                  inp.language)
    if not out["ok"]:
        # The call's own words: no key, no grant, quota spent, ElevenLabs down. The
        # panel shows them; nothing falls back to the browser's recognition.
        raise AuthzDenied(409, "dictation_unavailable",
                          out["message"] or "Dictation is not available here.")
    return {"text": out["text"]}


_PORTE = (DeclaredError(403, "api_token_forbidden",
                        "an API or delegated token, not an interactive session"),
          DeclaredError(403, "copilot_not_enabled",
                        "the account is not in the instance's allowlist"),
          DeclaredError(403, "copilot_view_as", "the request views as someone else"))
_PLAFOND = DeclaredError(429, "copilot_daily_cap",
                         "the account has reached today's spend ceiling "
                         "(`OTO_COPILOT_DAILY_CAP_USD`)")
_LIMITE_DU_JOUR = DeclaredError(429, "daily_spend_limit",
                               "on a key the operator re-bills, the person, one of their "
                               "teams or the org reached a daily spend limit an org admin "
                               "set (`access/limite_du_jour`)")

CAPABILITIES += [
    Capability(
        key="copilot.turn", handler=_turn, Input=TurnInput, Output=CopilotEvent,
        authz=ORG_MEMBER, mcp=None,
        errors=(*_PORTE, _PLAFOND, _LIMITE_DU_JOUR,
                DeclaredError(400, "unknown_attachment",
                              "an id of `attachments` is not the caller's in this org, or "
                              "does not exist")),
        rest=RestBinding("POST", "/api/copilot/turns", media=_SSE, provisoire=True,
                         allow_api_token=False),
        description="The in-app agent: one message of the person, answered as an event "
                    "stream (SSE). Opens a conversation when `thread_id` is null or not "
                    "the caller's; the first event names it (`thread`). Pauses on an "
                    "approval card or a page action; the matching route resumes it."),
    Capability(
        key="copilot.client_result", handler=_client_result, Input=ClientResultInput,
        Output=CopilotEvent, authz=ORG_MEMBER, mcp=None, errors=(*_PORTE, _PLAFOND, _LIMITE_DU_JOUR),
        rest=RestBinding("POST", "/api/copilot/threads/{thread_id}/client-results",
                         media=_SSE, provisoire=True,
                         allow_api_token=False),
        description="The page's answer to the in-app agent's page action (`call_id`), "
                    "and the page's actions now (`client_tools`); resumes the stream."),
    Capability(
        key="copilot.approval", handler=_approval, Input=ApprovalInput,
        Output=CopilotEvent, authz=ORG_MEMBER, mcp=None, errors=(*_PORTE, _PLAFOND, _LIMITE_DU_JOUR),
        rest=RestBinding("POST", "/api/copilot/threads/{thread_id}/approvals/{approval_id}",
                         media=_SSE, provisoire=True,
                         allow_api_token=False),
        description="The person's answer to an approval card: approve or reject, the "
                    "fields edited on the card (only those it offered), and whether to stop "
                    "asking for this kind of change in this conversation (plain writes "
                    "only). Resumes the stream."),
    Capability(
        key="copilot.dictation", handler=_dictation, Input=DictationInput,
        Output=DictationOut, authz=ORG_MEMBER, mcp=None,
        errors=(*_PORTE,
                DeclaredError(422, "dictation_invalid",
                              "the clip is empty, not audio, or over the limits"),
                DeclaredError(409, "dictation_unavailable",
                              "ElevenLabs refused or could not be reached: no key, no "
                              "grant, quota spent, or the service down")),
        rest=RestBinding("POST", "/api/copilot/dictations", provisoire=True,
                         allow_api_token=False),
        description="The words of a clip the person recorded in the panel, transcribed "
                    "by ElevenLabs (Scribe) on the org's key or the granted shared key, "
                    "metered like any `elevenlabs_act` call. Never the browser's own "
                    "recognition."),
    Capability(
        key="copilot.attachment_types", handler=_attachment_types, Input=_Rien,
        Output=AttachmentTypes, authz=ORG_MEMBER, mcp=None, errors=_PORTE,
        rest=RestBinding("GET", "/api/copilot/attachments", provisoire=True,
                         allow_api_token=False),
        description="What a file attached to a message of the in-app agent may be: "
                    "media type → largest size in bytes, and how many one message may "
                    "carry. The upload itself is `POST` on the same path (multipart)."),
    Capability(
        key="copilot.revert", handler=_revert, Input=RevertInput, Output=RevertOut,
        authz=ORG_MEMBER, mcp=None,
        errors=(*_PORTE,
                DeclaredError(404, "nothing_to_undo",
                              "already undone, expired, or another account's"),
                DeclaredError(409, "undo_failed", "the store refused to put it back")),
        rest=RestBinding("POST", "/api/copilot/actions/{revert_id}/revert", provisoire=True,
                         allow_api_token=False),
        description="Undoes one change the in-app agent made, once, before it expires: "
                    "only rows and cells that still hold what the agent wrote."),
]
