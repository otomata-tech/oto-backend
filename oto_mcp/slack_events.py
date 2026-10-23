"""The instance's bot in Slack — what an event of its PUBLISHED app becomes.

`POST /api/slack/events` (`api/slack.py`) only adapts: it reads the body capped, then
calls `recevoir` off the loop. Everything that decides lives here, testable without
HTTP — the same split as `runner_hook` and `api/hooks.py`.

## The path of an `@<app>`

1. **Proof**: the request is signed with the app's signing secret (`X-Slack-Signature`,
   HMAC-SHA256 over `v0:{timestamp}:{raw body}`), within five minutes of our clock.
   The secret is the INSTANCE's (`auth/slack.signing_secret`, platform scope of
   `connector_settings`), like the app's `client_id`/`client_secret`.
2. **Which org**: an event names a workspace (`team_id`) and an app (`api_app_id`),
   never an org. The org is the one whose ORG row was written by the one-click
   install of THIS app in THIS workspace (`credentials_store.slack_workspace_installs`).
   Several orgs on one workspace: the oldest install answers — a later install
   cannot divert the requests of a workspace already served.
3. **Which agent**: the org's hosted agent in the `slack` preset
   (`db.trigger_slack_de_l_org`) — at most one per org, chosen by an org admin.
4. **Who speaks** (decision of 23/10/2026): the Slack user's email, read with the
   bot token (`users.info`), must be that of exactly ONE member of the org. The run
   then carries THAT member's identity — their connections, their rights — never the
   agent owner's. Guests, bots, deleted users and users of another workspace (Slack
   Connect) are refused, by a message only they see.
5. **Enqueue**: `runner_hook.enfiler`, so the agent's pause, daily cap, smoothing and
   delivery log apply as to any webhook; the event's `event_id` is the delivery id,
   so a Slack retry (`X-Slack-Retry-Num`) never starts a second run.
6. **Pickup**: 👀 on the message. The agent answers in the thread itself
   (`slack_post_message`, added to its tools for this run).

⚠️ **ACK within 3 s, never a 5xx for a business reason**: Slack retries a slow or
failed delivery three times, then disables the endpoint of an app that keeps failing.
Every refusal planned here answers 200 to Slack — the person is told in Slack, the
owner reads the delivery log.

⚠️ **Thread history is NOT read here.** `conversations.replies` is limited to one call
a minute for an app outside the Slack Marketplace: the request is the message itself.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

from . import credentials_store, db, roles, runner_hook
from .auth import slack as slack_auth

logger = logging.getLogger(__name__)

#: The window, both ways, between the signed timestamp and our clock — Slack's value.
TOLERANCE_S = 300

#: What the run gets on top of the agent's tools: answering in the thread.
OUTILS_DE_REPONSE = ("slack_post_message", "slack_add_reaction")

#: The reaction that says "picked up" on the message.
REACTION_PRISE = "eyes"

#: The request carried into the run, in characters — a Slack message is 40,000 at most.
TEXTE_MAX = 8_000

_MENTION = re.compile(r"<@[A-Z0-9]+>")


def _marque() -> str:
    """The instance's declared brand — the name people know it by (`OTO_BRAND_NAME`)."""
    from .email_brand import nom_instance
    return nom_instance()


class Refus(Exception):
    """A refusal of the REQUEST itself (signature, shape) — the only ones that are not
    a 200: Slack did not send it, or sent it to the wrong place."""

    def __init__(self, statut: int, code: str):
        super().__init__(code)
        self.statut, self.code = statut, code


def verifier(brut: bytes, horodatage: Optional[str], signature: Optional[str],
             secret: Optional[str], maintenant: Optional[float] = None) -> None:
    """Raises `Refus` unless the request is signed by the app, within the window.

    The window is judged AFTER the signature: an unknown caller learns nothing from
    being told its timestamp is off."""
    from oto.tools.slack.client import verify_slack_signature

    if not secret:
        # Our misconfiguration, not the sender's: logged loudly, 503 so that Slack
        # retries once the operator has set it.
        logger.error("slack events: no signing secret set on the instance "
                     "(oto_admin_connector_setting connector=slack key=%s)",
                     slack_auth.SIGNING_SECRET)
        raise Refus(503, "slack_events_not_configured")
    if not horodatage or not signature or not horodatage.isdigit():
        raise Refus(401, "invalid_signature")
    try:
        valide = verify_slack_signature(secret, brut, horodatage, signature)
    except UnicodeDecodeError:
        valide = False
    if not valide:
        raise Refus(401, "invalid_signature")
    maintenant = time.time() if maintenant is None else maintenant
    if abs(maintenant - int(horodatage)) > TOLERANCE_S:
        raise Refus(401, "stale_timestamp")


@dataclass(frozen=True)
class Demande:
    """An `@<app>` or a DM to the bot, as Slack described it."""
    event_id: str
    team_id: str
    app_id: str
    user: str
    channel: str
    ts: str
    thread_ts: str
    texte: str
    en_dm: bool


def demande_de(corps: dict) -> Optional[Demande]:
    """The request an `event_callback` carries, or None when it is not one to answer.

    ⚠️ A DM event is ALSO sent for the bot's own messages and for edits: anything
    with a `bot_id` or a `subtype` is skipped — or the bot would answer itself."""
    ev = corps.get("event") or {}
    t = ev.get("type")
    en_dm = t == "message" and ev.get("channel_type") == "im"
    if t != "app_mention" and not en_dm:
        return None
    if ev.get("bot_id") or ev.get("subtype"):
        return None
    champs = (corps.get("event_id"), corps.get("team_id"), corps.get("api_app_id"),
              ev.get("user"), ev.get("channel"), ev.get("ts"))
    if not all(isinstance(c, str) and c for c in champs):
        return None
    event_id, team_id, app_id, user, channel, ts = champs
    texte = _MENTION.sub("", str(ev.get("text") or "")).strip()[:TEXTE_MAX]
    return Demande(event_id=event_id, team_id=team_id, app_id=app_id, user=user,
                   channel=channel, ts=ts, thread_ts=str(ev.get("thread_ts") or ts),
                   texte=texte, en_dm=en_dm)


# --- the org, its bot, the member -------------------------------------------

@dataclass(frozen=True)
class Installation:
    org_id: int
    account: str
    bot_token: str

    @property
    def instance(self) -> str:
        """The ORG row's ref — the run answers through it, not through a personal
        Slack token the member may also hold (cascade: personal first)."""
        from .instance_refs import ref_for_credential
        return ref_for_credential("org", str(self.org_id), "slack", self.account) or ""


def installation(team_id: str, app_id: str) -> Optional[Installation]:
    """The org that installed this app in this workspace, with its bot token."""
    lignes = credentials_store.slack_workspace_installs(team_id, app_id)
    if not lignes:
        return None
    if len(lignes) > 1:
        logger.info("slack events: workspace %s installed by %d orgs — the oldest "
                    "answers (org %s)", team_id, len(lignes), lignes[0]["entity_id"])
    ligne = lignes[0]
    secret = credentials_store.get_credential("org", ligne["entity_id"], "slack",
                                              ligne["account"])
    champs = credentials_store.unpack_secret("slack", secret or "") if secret else {}
    bot = (champs or {}).get("bot_token")
    if not bot:
        logger.warning("slack events: org %s holds workspace %s without a bot token",
                       ligne["entity_id"], team_id)
        return None
    return Installation(org_id=int(ligne["entity_id"]), account=ligne["account"],
                        bot_token=bot)


@dataclass(frozen=True)
class Membre:
    sub: str
    nom: str
    email: str


def membre(client: Any, org_id: int, d: Demande) -> tuple[Optional[Membre], Optional[str]]:
    """`(member, None)` or `(None, the message to show the person)`."""
    from oto.tools.slack.client import SlackError

    try:
        u = (client.get_user_info(d.user) or {}).get("user") or {}
    except SlackError as e:
        logger.warning("slack events: users.info failed for team %s (%s)",
                       d.team_id, e.error)
        return None, "I couldn't check who you are in Slack. Try again in a minute."
    if u.get("is_bot") or u.get("deleted"):
        return None, None
    if u.get("is_restricted") or u.get("is_ultra_restricted") \
            or (u.get("team_id") and u.get("team_id") != d.team_id):
        return None, ("I only take requests from full members of this workspace, "
                      "not from guests or people from another company.")
    profil = u.get("profile") or {}
    email = (profil.get("email") or "").strip().lower()
    if not email:
        return None, ("I can't see your email address in Slack, so I can't match "
                      f"you to a {_marque()} account.")
    subs = sorted({r["sub"] for r in db.get_users_by_email(email)
                   if roles.is_org_member(r["sub"], org_id)})
    if len(subs) != 1:
        if len(subs) > 1:
            logger.warning("slack events: %d members of org %s share one email — "
                           "refused", len(subs), org_id)
        return None, ("Your Slack email doesn't match exactly one member of this "
                      f"workspace's {_marque()} organization. Ask an admin to invite "
                      "you with the address you use in Slack.")
    nom = profil.get("real_name") or u.get("real_name") or u.get("name") or email
    return Membre(sub=subs[0], nom=str(nom), email=email), None


# --- the run --------------------------------------------------------------------

def entree(t: dict, d: Demande, m: Membre, instance: str) -> str:
    """The run's instruction: the agent's own, then the request, then how to answer.

    The request is NOT fenced as untrusted data: it is the ask of a member the
    signature and the email check identified — as if they had typed it in the app.
    Only Slack's coordinates are ours to state."""
    ou = "a direct message to you" if d.en_dm else f"a mention in channel {d.channel}"
    return (
        f"{t.get('input') or ''}\n\n"
        f"--- REQUEST FROM SLACK ---\n"
        f"From: {m.nom} <{m.email}>, a member of this organization (Slack user "
        f"{d.user}), in {ou}.\n"
        f"Message:\n{d.texte or '(empty — they only mentioned you)'}\n"
        f"--- end of the Slack request ---\n\n"
        f"Answer them in Slack, in the thread of their message: "
        f"`slack_post_message(channel=\"{d.channel}\", thread_ts=\"{d.thread_ts}\", "
        f"author=\"app\", text=…, _instance=\"{instance}\")`. Keep it short and link "
        f"to anything long (a table, a doc). If you can't do what they ask, say so "
        f"in the thread — never leave them without an answer."
    ).lstrip()


def _dire(client: Any, d: Demande, texte: str) -> None:
    """A message only the person sees (in a DM, a plain reply: it is theirs)."""
    from oto.tools.slack.client import SlackError

    try:
        if d.en_dm:
            client.post_message(d.channel, texte, thread_ts=d.thread_ts)
        else:
            client.post_ephemeral(d.channel, d.user, texte)
    except SlackError as e:
        logger.warning("slack events: could not answer in team %s (%s)",
                       d.team_id, e.error)


def traiter(d: Demande) -> dict:
    """An `@<app>` → a run of the org's Slack agent, as the member who asked."""
    from oto.tools.slack.client import SlackClient, SlackError

    inst = installation(d.team_id, d.app_id)
    if inst is None:
        logger.info("slack events: no org installed app %s in workspace %s",
                    d.app_id, d.team_id)
        return {"ok": True, "ignored": "no_install"}
    client = SlackClient(bot_token=inst.bot_token)
    t = db.trigger_slack_de_l_org(inst.org_id)
    if t is None:
        _dire(client, d, "No agent answers in Slack for this workspace yet. An admin "
                         f"of its {_marque()} organization can choose one on the "
                         "agent's page.")
        return {"ok": True, "ignored": "no_agent"}
    m, message = membre(client, inst.org_id, d)
    if m is None:
        if message:
            _dire(client, d, message)
        return {"ok": True, "ignored": "unknown_member"}
    # ⚠️ The run sees the MEMBER's toolbox, not the owner's: a connector they never
    # switched on is masked in their session, and the run could not even answer.
    from .capabilities._outils_manquants import manquants
    manque = manquants(inst.org_id, m.sub,
                       [*(t.get("tools") or ()), *OUTILS_DE_REPONSE])
    if manque:
        _dire(client, d, "I can't run this for you yet: these tools aren't switched on "
                         f"in your {_marque()} connectors: {', '.join(manque)}. Turn "
                         "them on, then ask again.")
        return {"ok": True, "refused": "missing_tools"}
    try:
        res = runner_hook.enfiler(t, d.event_id, "slack", entree=entree(t, d, m, inst.instance),
                                  sub=m.sub, outils_en_plus=OUTILS_DE_REPONSE)
    except runner_hook.HookRefus as e:
        # Recorded on the agent's page by `enfiler`; the person gets the reason.
        _dire(client, d, e.message)
        return {"ok": True, "refused": e.code}
    if not res.get("duplicate"):
        try:
            client.add_reaction(d.channel, d.ts, REACTION_PRISE)
        except SlackError as e:
            logger.info("slack events: no pickup reaction in team %s (%s)",
                        d.team_id, e.error)
    logger.info("slack events: org %s, agent %s, job %s queued for %s",
                inst.org_id, t["id"], res.get("job_id"), m.sub)
    return res


def desinstaller(corps: dict) -> dict:
    """`app_uninstalled`, or `tokens_revoked` naming the bot: the workspace's rows
    written by this app are deleted — a dead token kept is a connector that looks
    connected and fails at every call."""
    ev = corps.get("event") or {}
    team_id, app_id = corps.get("team_id"), corps.get("api_app_id")
    if not (isinstance(team_id, str) and isinstance(app_id, str)):
        return {"ok": True, "ignored": "shape"}
    if ev.get("type") == "tokens_revoked" and not (ev.get("tokens") or {}).get("bot"):
        # Only a person's user token went: the bot still works.
        logger.info("slack events: user token revoked in workspace %s", team_id)
        return {"ok": True, "ignored": "user_token_only"}
    n = 0
    for ligne in credentials_store.slack_workspace_installs(team_id, app_id):
        n += credentials_store.clear_credential("org", ligne["entity_id"], "slack",
                                                account=ligne["account"])
    logger.warning("slack events: app removed from workspace %s — %d org row(s) "
                   "deleted", team_id, n)
    return {"ok": True, "removed": n}


def recevoir(brut: bytes, entetes: Any, corps: Any) -> dict:
    """THE gesture: verify, then route. Synchronous — the route runs it off the loop.
    Returns what the route serialises with a 200; raises `Refus` otherwise."""
    verifier(brut, entetes.get("x-slack-request-timestamp"),
             entetes.get("x-slack-signature"), slack_auth.signing_secret())
    if not isinstance(corps, dict):
        raise Refus(400, "invalid_body")
    if corps.get("type") == "url_verification":
        return {"challenge": str(corps.get("challenge") or "")}
    if corps.get("type") != "event_callback":
        return {"ok": True, "ignored": "type"}
    if (corps.get("event") or {}).get("type") in ("app_uninstalled", "tokens_revoked"):
        return desinstaller(corps)
    d = demande_de(corps)
    if d is None:
        return {"ok": True, "ignored": "event"}
    return traiter(d)
