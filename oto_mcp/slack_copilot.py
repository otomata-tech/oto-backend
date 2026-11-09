"""@<app> in Slack — the in-app agent (`capabilities/copilot`), with Slack as its screen.

A member writes `@<app> …` in a channel, a private channel or a group DM the app sits in
(or DMs the app when the org has no Slack agent): the SAME loop as the panel answers —
`routes._tour`, with the member's own toolbox, rights and org credits, the same model
routing, the same daily ceiling — and what the panel shows as events, Slack shows as
messages. `slack_events.py` decides WHO the request is from and WHICH door it came by;
this module decides what the agent says and how Slack shows it.

## What a request does

1. **The member**: the Slack user's confirmed email matches exactly one real member of the
   org that installed the app (`slack_events.membre`) — a guest, someone from another
   company or an unknown email is told so (ephemeral in a channel, in the thread of a DM)
   and nothing runs. This door names the account by email, with no REST request: it
   crosses the identity guard itself (`garde_identite.refus`: paused account, disabled
   tenant) and refuses a suspended org (`org_suspension.refus`), like the panel's door
   (`_coupe`). The agent must be open to them (`OTO_COPILOT_SUBS`) and under today's
   spend ceiling.
2. **Once**: a mention reaches us twice (the `app_mention` event and the `message` event of
   the same message) and Slack retries: `(channel, ts)` is claimed in
   `copilot_slack_events`, the first event wins.
3. **The conversation**: `(workspace, channel, thread, member)` → a copilot conversation
   (`copilot_slack_threads`). A mention starts one in the thread under it; the same
   person's later replies in that thread continue it WITHOUT another mention. Somebody
   else mentioning the app in the thread opens a conversation of their own — nothing is
   shared between two people. A DM is one rolling conversation per person, a new one after
   30 idle minutes.
4. **The answer**: 👀 on the request; one "working" message in the thread, edited with the
   agent's progress notes (at most one edit every `PAS_DE_MISE_A_JOUR_S` seconds — Slack
   limits `chat.update`), then replaced by the answer. The conversation lives where the
   panel's does (`copilot_threads`): same history, same prompt, `canal="slack"`.
5. **Changes ask first, in Slack**: the approval card is a message with Approve / Cancel
   buttons carrying the server-written sentence and EVERY argument of the call. Only the
   person who asked may press them (`POST /api/slack/interactions`, signed like the
   events; the click is the conversation's `sub` or nothing). A call too long to be shown
   whole gets no Approve button — nothing runs that the person could not read. A change
   that can be undone gets an Undo button on the answer (`routes.revert_pour`, once, the
   same journal as the panel's). A page action does not exist here: no client tool is
   offered, and the prompt says so.

⚠️ A mention in the thread of one of OUR AGENTS' messages is that agent's (the reply event
restarts it, `slack_events._reponse_a_l_agent`): the copilot ignores it, or the person would
start two runs with one sentence.
⚠️ Never a 5xx for a business reason (Slack retries three times, then switches the endpoint
off): everything planned is told to the person in Slack.
"""
from __future__ import annotations

import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qs

import anyio
from starlette.concurrency import run_in_threadpool

from . import geste, slack_copilot_places as places, slack_events
from .auth import slack as slack_auth
from .capabilities._types import AuthzDenied, ResolvedCtx
from .db import copilot as store

logger = logging.getLogger(__name__)

ACTION_OUI = "copilot_approve"
ACTION_NON = "copilot_reject"
ACTION_ANNULER = "copilot_undo"

#: A Slack `section` holds 3,000 characters; a little less leaves room for a fence.
BLOC_MAX = 2900
#: The most a card shows of a call's arguments. Longer: no Approve button (see above).
DETAIL_MAX = 14_000
#: Blocks one message may carry: 50.
BLOCS_MAX = 48
#: Slack limits `chat.update` (tier 3): never more than one edit this often.
PAS_DE_MISE_A_JOUR_S = 1.5
#: Undo buttons offered on one answer.
ANNULATIONS_MAX = 5
#: The note under the working message: the start of the agent's latest progress note.
NOTE_MAX = 200

ATTENTE = "_Working on it…_"
INCONNU = ("Something went wrong on our side; try again.")
#: What `boucle.repondre_carte` says of an answer to a card that is no longer waiting.
_PLUS_EN_ATTENTE = "That answer is for something no longer waiting."


# --- Slack text ⇄ the agent's text --------------------------------------------------------

_LIEN = re.compile(r"<((?:https?://|mailto:)[^>|]+)(?:\|([^>]*))?>")
_CANAL = re.compile(r"<#[A-Z0-9]+(?:\|([^>]*))?>")
_PERSONNE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")
_SPECIAL = re.compile(r"<!(here|channel|everyone)(?:\|[^>]*)?>")
_GROUPE = re.compile(r"<!subteam\^[A-Z0-9]+(?:\|([^>]*))?>")
_TITRE_MD = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_GRAS_MD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_LIEN_MD = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_PUCE_MD = re.compile(r"^([ \t]*)[*+][ \t]+", re.MULTILINE)


def texte_de_demande(texte: str, bot_user: str = "") -> str:
    """What the person asked, from the message as Slack serialised it: the mention of the
    app (the address, not the request) removed, links and names as words, Slack's escapes
    undone. Pure."""
    t = texte or ""
    if bot_user:
        t = re.sub(rf"<@{re.escape(bot_user)}(?:\|[^>]*)?>", " ", t)
    else:
        t = re.sub(r"^\s*<@[A-Z0-9]+(?:\|[^>]*)?>", " ", t, count=1)
    t = _LIEN.sub(lambda m: (f"{m.group(2)} ({m.group(1)})"
                             if m.group(2) and m.group(2) != m.group(1) else m.group(1)), t)
    t = _CANAL.sub(lambda m: f"#{m.group(1)}" if m.group(1) else "a channel", t)
    t = _SPECIAL.sub(lambda m: f"@{m.group(1)}", t)
    t = _GROUPE.sub(lambda m: f"@{m.group(1)}" if m.group(1) else "a user group", t)
    t = _PERSONNE.sub(lambda m: f"@{m.group(1)}", t)
    t = t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return t.strip()


def vers_mrkdwn(texte: str) -> str:
    """The agent's markdown as Slack's `mrkdwn`: `&`, `<`, `>` escaped FIRST (nothing the
    model — or a row it read — wrote can become a mention, a channel link or `<!channel>`),
    then links, bold, headings and bullets in Slack's spelling. Pure."""
    t = (texte or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = _LIEN_MD.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>", t)
    t = _GRAS_MD.sub(r"*\1*", t)
    t = _TITRE_MD.sub(r"*\1*", t)
    t = _PUCE_MD.sub(r"\1- ", t)
    return t.strip()


def _brut(texte: str, n: int) -> str:
    """The plain start of `texte`, for a message's notification text."""
    return re.sub(r"\s+", " ", texte or "").strip()[:n] or slack_events._marque()


# --- Block Kit ----------------------------------------------------------------------------

def _sections(mrkdwn: str) -> list[dict]:
    """`mrkdwn` as `section` blocks of at most `BLOC_MAX` characters, cut at line ends."""
    reste, morceaux = (mrkdwn or "").strip(), []
    while reste and len(morceaux) < BLOCS_MAX // 2:
        if len(reste) <= BLOC_MAX:
            morceaux.append(reste)
            reste = ""
            break
        coupe = reste.rfind("\n", 0, BLOC_MAX)
        coupe = coupe if coupe >= BLOC_MAX // 2 else BLOC_MAX
        morceaux.append(reste[:coupe])
        reste = reste[coupe:].lstrip("\n")
    if reste:
        morceaux.append("_(the rest is cut — ask for it in smaller parts)_")
    return [{"type": "section", "text": {"type": "mrkdwn", "text": m}} for m in morceaux]


def _contexte(mrkdwn: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": mrkdwn[:2900]}]}


def _bouton(action_id: str, texte: str, valeur: str, *, style: Optional[str] = None,
            confirmer: Optional[str] = None) -> dict:
    b: dict = {"type": "button", "action_id": action_id,
               "text": {"type": "plain_text", "text": texte[:75]}, "value": valeur[:1900]}
    if style:
        b["style"] = style
    if confirmer:
        b["confirm"] = {"title": {"type": "plain_text", "text": "Are you sure?"},
                        "text": {"type": "mrkdwn", "text": confirmer},
                        "confirm": {"type": "plain_text", "text": "Yes, do it"},
                        "deny": {"type": "plain_text", "text": "Cancel"}}
    return b


def _code(texte: str) -> list[dict]:
    """`texte` as fenced blocks, WHOLE: each chunk fits a section, a fence inside it is
    defused. The caller has checked it is at most `DETAIL_MAX`."""
    sain = texte.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") \
        .replace("```", "`​``")
    limite = BLOC_MAX - 8
    return [{"type": "section", "text": {"type": "mrkdwn",
                                         "text": f"```{sain[i:i + limite]}```"}}
            for i in range(0, len(sain), limite)]


def blocs_carte(thread_id: str, e: dict) -> list[dict]:
    """The approval card of an `approval_required` event: the sentence the server wrote,
    then every argument of the call, WHOLE, then the buttons — Approve only when the person
    could read it all."""
    valeur = f"{thread_id}|{e.get('id')}"
    blocs: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text":
                          f":lock: *Approval needed*\n{vers_mrkdwn(e.get('title') or '')}"}}]
    detail, complet = "", True
    for etape in e.get("steps") or []:
        if etape.get("title"):
            blocs.append(_contexte(vers_mrkdwn(str(etape["title"]))))
        detail = str(etape.get("detail") or "")
        if len(detail) > DETAIL_MAX:
            complet = False
        elif detail.strip():
            blocs += _code(detail)
    boutons = []
    if complet:
        destructif = bool(e.get("destructive"))
        boutons.append(_bouton(ACTION_OUI, "Approve", valeur,
                               style="danger" if destructif else "primary",
                               confirmer="This change is destructive." if destructif else None))
    else:
        blocs.append(_contexte(":no_entry: Too long to show here in full, so it can't be "
                               "approved from Slack. Cancel it, or ask for a smaller change."))
    boutons.append(_bouton(ACTION_NON, "Cancel", valeur))
    blocs.append({"type": "actions", "elements": boutons})
    return blocs


def blocs_final(texte: str, annulations: list[tuple[str, str]], carte: Optional[dict],
                erreur: Optional[str], thread_id: Optional[str],
                prefixe: Optional[str] = None) -> list[dict]:
    """The message that replaces the working one: the answer, what failed, an Undo button
    per undoable change, the approval card when the loop paused on one."""
    blocs: list[dict] = []
    if prefixe:
        blocs.append(_contexte(prefixe))
    if texte.strip():
        blocs += _sections(vers_mrkdwn(texte))
    if erreur:
        blocs.append({"type": "section", "text": {"type": "mrkdwn",
                                                  "text": f":warning: {vers_mrkdwn(erreur)}"}})
    for i, (revert_id, resume) in enumerate(annulations[:ANNULATIONS_MAX]):
        blocs.append(_contexte(f":white_check_mark: {vers_mrkdwn(resume)}"))
        blocs.append({"type": "actions", "elements": [
            _bouton(f"{ACTION_ANNULER}:{i}", "Undo", revert_id)]})
    if carte is not None and thread_id:
        blocs += blocs_carte(thread_id, carte)
    if not blocs or all(b["type"] == "context" for b in blocs):
        blocs.append({"type": "section", "text": {"type": "mrkdwn", "text": "Done."}})
    return blocs[:BLOCS_MAX]


# --- showing the stream in Slack ----------------------------------------------------------

class Rendu:
    """One Slack message that shows a turn: opened as "working", edited with the agent's
    notes, replaced by the answer. Every Slack call is off the loop and never raises — a
    message that cannot be edited must not lose the turn's work."""

    def __init__(self, client: Any, channel: str, ts: Optional[str] = None, *,
                 thread_ts: Optional[str] = None, utilisateur: Optional[str] = None,
                 ephemere_si_seule_erreur: bool = False):
        self.client, self.channel, self.ts = client, channel, ts
        self.thread_ts, self.utilisateur = thread_ts, utilisateur
        self.ephemere_si_seule_erreur = ephemere_si_seule_erreur
        self._dernier, self._derniere_note = 0.0, ""

    async def _appel(self, fn, *args, **kw):
        from oto.tools.slack.client import SlackError
        try:
            return await run_in_threadpool(lambda: fn(*args, **kw))
        except SlackError as e:
            logger.warning("slack copilot: Slack refused %s (%s)",
                           getattr(fn, "__name__", "a call"), e.error)
        except Exception:  # noqa: BLE001 — logged whole; the turn's work must survive it
            logger.exception("slack copilot: a Slack call failed")
        return None

    async def ouvrir(self) -> bool:
        """Posts the working message in the thread. False when Slack would not."""
        sortie = await self._appel(
            self.client.post_message, self.channel, ATTENTE, thread_ts=self.thread_ts,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": ATTENTE}}])
        self.ts = (sortie or {}).get("ts")
        return bool(self.ts)

    async def _ecrire(self, blocs: list[dict], texte: str) -> None:
        if self.ts:
            await self._appel(self.client.update_message, self.channel, self.ts,
                              text=texte, blocks=blocs)

    async def note(self, texte: str) -> None:
        """A progress note, at most one edit every `PAS_DE_MISE_A_JOUR_S` seconds."""
        texte = re.sub(r"\s+", " ", texte or "").strip()[:NOTE_MAX]
        if not texte or texte == self._derniere_note \
                or time.monotonic() - self._dernier < PAS_DE_MISE_A_JOUR_S:
            return
        self._dernier, self._derniere_note = time.monotonic(), texte
        await self._ecrire([{"type": "section", "text": {
            "type": "mrkdwn", "text": f"_{vers_mrkdwn(texte)}_"}}], texte)

    async def fin(self, texte: str, annulations: list[tuple[str, str]],
                  carte: Optional[dict], erreur: Optional[str], thread_id: Optional[str],
                  prefixe: Optional[str] = None) -> None:
        """The turn's result in the message. An answer to a button that produced nothing but
        an error is told to the person alone: the message holds a card or an answer that
        another press may already be rewriting."""
        if erreur and not texte.strip() and not annulations and carte is None \
                and self.ephemere_si_seule_erreur and self.utilisateur:
            await self._appel(self.client.post_ephemeral, self.channel, self.utilisateur,
                              _brut(erreur, 500))
            return
        await self._ecrire(blocs_final(texte, annulations, carte, erreur, thread_id, prefixe),
                           _brut(texte or (carte or {}).get("title") or erreur or "", 200))


async def _consommer(rendu: Rendu, flux, thread_id: Optional[str], lier=None,
                     prefixe: Optional[str] = None) -> None:
    """Reads the loop's events and shows them: notes while it works, then ONE final
    message. `lier(thread_id)` is called when the loop names its conversation."""
    from .capabilities.copilot import cle as cles
    texte: list[str] = []
    annulations: list[tuple[str, str]] = []
    carte: Optional[dict] = None
    erreur: Optional[str] = None
    apres_un_outil = False
    try:
        async for e in flux:
            t = e.get("type")
            if t == "thread":
                thread_id = e.get("thread_id") or thread_id
                if lier is not None and thread_id:
                    await run_in_threadpool(lier, thread_id)
            elif t == "text_delta":
                if apres_un_outil and texte:
                    texte.append("\n\n")
                apres_un_outil = False
                texte.append(e.get("text") or "")
            elif t == "progress":
                await rendu.note(e.get("text") or "")
            elif t == "tool_call":
                apres_un_outil = True
                await rendu.note(f"{e.get('label') or e.get('name') or 'Working'}…")
            elif t == "tool_result" and e.get("revert_id"):
                annulations.append((e["revert_id"], e.get("summary") or "Changed"))
            elif t == "approval_required":
                carte = e
            elif t == "error":
                erreur = e.get("message") or INCONNU
    except cles.SansCle as ex:
        erreur = ex.message
    except Exception:  # noqa: BLE001 — logged whole; the person gets a plain sentence
        logger.exception("slack copilot: a turn failed")
        erreur = INCONNU
    await rendu.fin("".join(texte), annulations, carte, erreur, thread_id, prefixe)


# --- a request --------------------------------------------------------------------------

def _executer(fn, *args):
    """Runs the async `fn` ON THE SERVER'S LOOP from the worker thread the ACK's background
    task runs in: the tools need the FastMCP context bound to that loop."""
    return anyio.from_thread.run(fn, *args)


def _dm_sans_fil(d: slack_events.Demande) -> bool:
    return d.dm and not d.reponse


def cle_de_fil(d: slack_events.Demande) -> str:
    """Where the conversation is filed: the thread, or '' for a DM's rolling one."""
    return "" if _dm_sans_fil(d) else d.thread_ts


def _coupe(sub: str, org_id: int) -> Optional[str]:
    """Why this account may no longer act in this org, or None: the identity guard (a
    paused account, a disabled tenant) then a suspended org — what the panel's REST door
    refuses (`api.base`, `org_suspension.garde_capacite`). A failed read RAISES
    (fail-closed)."""
    from . import garde_identite, org_suspension

    if (coupe := garde_identite.refus(sub)):
        return coupe[1]
    return org_suspension.refus(org_id)


def traiter(client: Any, inst: slack_events.Installation, d: slack_events.Demande) -> dict:
    """A mention, or a DM with no Slack agent → a turn of the in-app agent, as the member.
    A mention in a thread of ours carries on its conversation; a reply there that does not
    tag the app never reaches here (`slack_events.demande_de`). Runs in the ACK's worker
    thread."""
    from .capabilities.copilot import depense, routes

    m, refus = slack_events.membre(client, inst.org_id, d)
    if m is None:
        # A mention or a DM is a request: the reason is the answer.
        if refus:
            slack_events._dire(client, d, refus)
        return {"ok": True, "ignored": "unknown_member"}
    cle = cle_de_fil(d)
    existant = store.slack_fil(d.team_id, d.channel, cle, m.sub)
    if (coupe := _coupe(m.sub, inst.org_id)):
        slack_events._dire(client, d, coupe)
        return {"ok": True, "refused": "account_cut"}
    if not store.slack_reclamer(d.channel, d.ts):
        return {"ok": True, "duplicate": True}
    if not routes.admis(m.sub):
        slack_events._dire(client, d, f"The {slack_events._marque()} agent isn't switched "
                                      "on for your account yet.")
        return {"ok": True, "refused": "copilot_not_enabled"}
    if depense.atteint(m.sub):
        slack_events._dire(client, d, depense.message_de_refus())
        return {"ok": True, "refused": "copilot_daily_cap"}
    texte = texte_de_demande(d.texte, d.bot_user)
    if not texte:
        slack_events._dire(client, d, "Tell me what you need after the mention — for "
                                      "example: “what ran last night?”")
        return {"ok": True, "ignored": "empty"}
    thread_id = None
    if existant and not (_dm_sans_fil(d) and existant["inactif_min"] > routes.INACTIVITE_MIN):
        thread_id = existant["thread_id"]
    with contextlib.ExitStack() as pile:
        try:
            pile.enter_context(places.place(m.sub))
        except places.Occupe:
            slack_events._dire(client, d, places.MESSAGE)
            return {"ok": True, "refused": "busy"}
        slack_events._reaction_prise(client, d)
        try:
            _executer(_repondre, inst, client, d, m, texte, thread_id, cle)
        finally:
            _retirer_reaction(client, d)
    return {"ok": True, "answered": True}


def _retirer_reaction(client: Any, d: slack_events.Demande) -> None:
    """Takes the 👀 off: the request is no longer being worked on."""
    from oto.tools.slack.client import SlackError
    try:
        client._request("POST", "reactions.remove", json={
            "channel": d.channel, "timestamp": d.ts, "name": slack_events.REACTION_PRISE})
    except SlackError as e:
        logger.info("slack copilot: no reaction to remove in team %s (%s)", d.team_id, e.error)
    except Exception:  # noqa: BLE001 — a stuck 👀 is cosmetic; the answer is out
        logger.warning("slack copilot: could not remove the reaction", exc_info=True)


async def _repondre(inst: slack_events.Installation, client: Any, d: slack_events.Demande,
                    m: slack_events.Membre, texte: str, thread_id: Optional[str],
                    cle: str) -> None:
    from .capabilities.copilot import routes

    rendu = Rendu(client, d.channel, thread_ts=None if _dm_sans_fil(d) else d.thread_ts)
    if not await rendu.ouvrir():
        return
    genre = "dm" if d.dm else "channel"
    entree = routes.TurnInput(
        thread_id=thread_id, text=texte, mode="ask",
        page_context={"surface": "slack", "kind": genre})
    ctx = ResolvedCtx(sub=m.sub, org_id=inst.org_id)

    def lier(tid: str) -> None:
        if tid != thread_id:
            store.slack_lier(tid, d.team_id, d.channel, cle, m.sub)

    # The panel's REST door runs the handler under the person's console gesture; the same
    # here, so the run and what it writes are stamped like theirs (each tool call then
    # takes the agent's own gesture, `outils.executer`).
    with geste.portee(geste.CONSOLE, m.sub):
        await _consommer(rendu, routes._tour(ctx, entree, [], canal="slack"), thread_id, lier)


# --- the buttons ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Action:
    """A press of one of OUR buttons, as Slack described it."""
    kind: str            # approve | reject | undo
    valeur: str
    action_id: str
    action_ts: str
    user: str
    team_id: str
    app_id: str
    channel: str
    message_ts: str
    blocs: tuple = ()
    #: The Enterprise Grid of the workspace the press came from ('' = not a Grid).
    enterprise_id: str = ""


_GENRES = (("approve", ACTION_OUI), ("reject", ACTION_NON), ("undo", ACTION_ANNULER))


def _grille_de(payload: dict) -> str:
    """The press's Enterprise Grid: `enterprise.id`, else `team.enterprise_id` ('')."""
    for v in ((payload.get("enterprise") or {}).get("id"),
              (payload.get("team") or {}).get("enterprise_id")):
        if isinstance(v, str) and v:
            return v
    return ""


def action_de(payload: dict) -> Optional[Action]:
    """The press a `block_actions` payload carries, or None when it is not one of ours."""
    if payload.get("type") != "block_actions":
        return None
    actions = payload.get("actions") or []
    a = actions[0] if actions and isinstance(actions[0], dict) else {}
    action_id = str(a.get("action_id") or "")
    kind = next((k for k, pre in _GENRES if action_id == pre
                 or action_id.startswith(pre + ":")), None)
    message = payload.get("message") or {}
    champs = (str(a.get("value") or ""), str(a.get("action_ts") or ""),
              str((payload.get("user") or {}).get("id") or ""),
              str((payload.get("team") or {}).get("id") or ""),
              str(payload.get("api_app_id") or ""),
              str((payload.get("channel") or {}).get("id") or ""),
              str(message.get("ts") or (payload.get("container") or {}).get("message_ts")
                  or ""))
    if kind is None or not all(champs):
        return None
    valeur, action_ts, user, team_id, app_id, channel, message_ts = champs
    return Action(kind=kind, valeur=valeur, action_id=action_id, action_ts=action_ts,
                  user=user, team_id=team_id, app_id=app_id, channel=channel,
                  message_ts=message_ts, blocs=tuple(message.get("blocks") or ()),
                  enterprise_id=_grille_de(payload))


def recevoir_interaction(brut: bytes, entetes: Any) -> Optional[Action]:
    """THE gesture for a button press: verify the signature, read the form, return the
    press to handle AFTER the ACK — or None. Synchronous (the route runs it off the loop);
    raises `slack_events.Refus` for a request that is not Slack's."""
    slack_events.verifier(brut, entetes.get("x-slack-request-timestamp"),
                          entetes.get("x-slack-signature"), slack_auth.signing_secret())
    try:
        champs = parse_qs(brut.decode("utf-8"), strict_parsing=True)
        payload = json.loads((champs.get("payload") or [""])[0])
    except (ValueError, UnicodeDecodeError):
        raise slack_events.Refus(400, "invalid_body") from None
    if not isinstance(payload, dict):
        raise slack_events.Refus(400, "invalid_body")
    return action_de(payload)


def _ephemere(client: Any, a: Action, texte: str) -> None:
    from oto.tools.slack.client import SlackError
    try:
        client.post_ephemeral(a.channel, a.user, texte)
    except SlackError as e:
        logger.warning("slack copilot: could not answer a press in team %s (%s)",
                       a.team_id, e.error)


def traiter_action(a: Action) -> dict:
    """A press → the answer to a card, or an undo, as the member who pressed — and only if
    that member is the conversation's own."""
    from oto.tools.slack.client import SlackClient

    if not store.slack_reclamer(a.channel, f"act:{a.message_ts}:{a.action_ts}"):
        return {"ok": True, "duplicate": True}
    inst = slack_events.installation(a.team_id, a.app_id)
    if inst is None:
        logger.info("slack copilot: no org installed app %s in workspace %s",
                    a.app_id, a.team_id)
        return {"ok": True, "ignored": "no_install"}
    client = SlackClient(bot_token=inst.bot_token)
    d = slack_events.Demande(event_id=a.action_ts, team_id=a.team_id, app_id=a.app_id,
                             user=a.user, channel=a.channel, ts=a.message_ts,
                             thread_ts=a.message_ts, texte="", dm=False,
                             enterprise_id=a.enterprise_id)
    m, refus = slack_events.membre(client, inst.org_id, d)
    if m is None:
        _ephemere(client, a, refus or "I can't match you to an account, so I can't act "
                                      "on this.")
        return {"ok": True, "ignored": "unknown_member"}
    if (coupe := _coupe(m.sub, inst.org_id)):
        _ephemere(client, a, coupe)
        return {"ok": True, "refused": "account_cut"}
    if a.kind == "undo":
        return _annuler(client, a, m)
    thread_id, _, approval_id = a.valeur.partition("|")
    ou = store.slack_de(thread_id)
    if not ou or ou["sub"] != m.sub or ou["channel"] != a.channel \
            or ou["team_id"] != a.team_id or not approval_id:
        # Not the asker's, or not a conversation of this channel: no detail given.
        _ephemere(client, a, "Only the person who asked can answer this.")
        return {"ok": True, "refused": "not_the_asker"}
    from .capabilities.copilot import routes
    if not routes.admis(m.sub):
        _ephemere(client, a, f"The {slack_events._marque()} agent isn't switched on for "
                             "your account.")
        return {"ok": True, "refused": "copilot_not_enabled"}
    with contextlib.ExitStack() as pile:
        try:
            pile.enter_context(places.place(m.sub))
        except places.Occupe:
            _ephemere(client, a, places.MESSAGE)
            return {"ok": True, "refused": "busy"}
        _executer(_reprendre, inst, client, a, m, thread_id, approval_id)
    return {"ok": True, "answered": True}


async def _reprendre(inst: slack_events.Installation, client: Any, a: Action,
                     m: slack_events.Membre, thread_id: str, approval_id: str) -> None:
    from .capabilities.copilot import boucle, routes

    approuve = a.kind == "approve"
    rendu = Rendu(client, a.channel, a.message_ts, utilisateur=a.user,
                  ephemere_si_seule_erreur=True)
    ctx = ResolvedCtx(sub=m.sub, org_id=inst.org_id)
    flux = routes._reprise(ctx, thread_id, lambda f: routes._parler(
        f, False, boucle.repondre_carte(f, approval_id, approuve, None, False)))
    mot = "✅ Approved" if approuve else "✖️ Cancelled"
    with geste.portee(geste.CONSOLE, m.sub):
        await _consommer(rendu, flux, thread_id, prefixe=f"{mot} by <@{a.user}>")


def _annuler(client: Any, a: Action, m: slack_events.Membre) -> dict:
    """The Undo button: the panel's undo, once, for the member who made the change."""
    from .capabilities.copilot import routes

    try:
        out = routes.revert_pour(m.sub, a.valeur)
    except AuthzDenied as e:
        _ephemere(client, a, e.message if hasattr(e, "message") else "Nothing to undo.")
        return {"ok": True, "refused": getattr(e, "code", "undo")}
    reste = [b for b in a.blocs
             if not (b.get("type") == "actions" and any(
                 el.get("action_id") == a.action_id for el in b.get("elements") or ()))]
    nuance = f" ({out['skipped']} left alone: changed since)" if out.get("skipped") else ""
    reste.append(_contexte(f":leftwards_arrow_with_hook: Undone by <@{a.user}>: "
                           f"{vers_mrkdwn(out.get('undone') or 'the change')}{nuance}"))
    from oto.tools.slack.client import SlackError
    try:
        client.update_message(a.channel, a.message_ts, text="Undone", blocks=reste[:BLOCS_MAX])
    except SlackError as e:
        logger.warning("slack copilot: could not show an undo in team %s (%s)",
                       a.team_id, e.error)
    return {"ok": True, "undone": True}


def apres_l_accuse_action(a: Action) -> None:
    """`traiter_action`, AFTER the 200 went to Slack (the route's background task). Nobody
    is left to answer an exception: it is logged whole."""
    try:
        traiter_action(a)
    except Exception:  # noqa: BLE001 — logged whole; no caller left to raise to
        logger.exception("slack copilot: a press in team %s failed after the ACK", a.team_id)
