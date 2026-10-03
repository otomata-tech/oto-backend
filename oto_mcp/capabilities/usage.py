"""Capacités « signaux d'usage » (ADR 0017, barreau 3) : un seul signal volontaire
`feedback` — retour sur un outil (`signal='tool_feedback'`) OU remontée d'un cas
d'usage non couvert (`signal='gap'`). Même substrat, axe explicite.

Co-déclaré MCP + REST (ADR 0009) → émis par les **agents** (tool `feedback`,
auto-journalisé dans tool_calls + corrélé run_id) ET par des **humains** (dashboard,
POST REST). Le contenu durable atterrit dans `usage_signals` (hors prune). Le `gap`
fait de l'agent un capteur de demande non satisfaite.

Handler SYNC (les adaptateurs n'awaitent pas) : on capte `session_id` (propriété
sync du contexte) ; le `run_id` du face-agent vit déjà dans la row tool_calls jumelle.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, field_validator

from .. import config, db, email as mailer, org_store, outreach_optout
from ._authz import PLATFORM_ADMIN, SUB_ONLY
from ._types import AuthzDenied, cap_limit, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class FeedbackInput(BaseModel):
    signal: Literal["tool_feedback", "gap"]
    # tool_feedback: bug | misleading_doc | wrong_result | praise | other
    # gap:           missing_tool | missing_doctrine | missing_data | other
    kind: str
    target: str           # tool_feedback: nom de l'outil ; gap: ce que tu voulais faire
    text: Optional[str] = None


class SignalRecorded(BaseModel):
    """Accusé d'enregistrement d'un signal d'usage — **pas un accusé de traitement**.
    `ok: true` dit que la remontée est écrite et sera vue ; il ne promet ni réponse,
    ni correction, ni notification en retour. Le suivi vit ailleurs (les projections
    admin `usage.signals`, où le signal se résout).

    Rien n'est dédupliqué : deux appels identiques donnent deux lignes et deux `id`
    distincts. C'est volontaire — la répétition d'un même manque EST le signal (ADR
    0017) —, mais un client qui rejoue sa requête sur timeout double son propre poids.

    L'échec, lui, n'a pas de forme : l'écriture est synchrone, une panne remonte en
    erreur HTTP et non en `ok: false`. Il n'existe donc pas de 200 négative ici."""
    ok: bool                     # toujours `true` — un échec ne prend pas ce chemin
    # Identifiant durable de la ligne `usage_signals`, à citer pour la résoudre côté
    # plateforme. Croît strictement, tous signaux et tous émetteurs confondus.
    id: int


def _correlation() -> tuple[str, Optional[str]]:
    """(source, session_id). Contexte MCP présent → 'agent' + session ; sinon
    (REST humain) → 'human' + None. Best-effort, jamais bloquant."""
    try:
        from fastmcp.server.dependencies import get_context
        ctx = get_context()
        return "agent", ctx.session_id
    # noqa: SILENT — corrélation indéterminée ⇒ « human » par défaut
    except Exception:
        return "human", None


def _active_org(sub: str) -> Optional[int]:
    try:
        return org_store.get_active_org(sub)
    # noqa: SILENT — org active indéterminée ⇒ vue non filtrée
    except Exception:
        return None


def _feedback(ctx: ResolvedCtx, inp: FeedbackInput) -> dict:
    source, session_id = _correlation()
    sid, deja = db.insert_usage_signal(
        sub=ctx.sub, org_id=ctx.org_id or _active_org(ctx.sub),
        signal=inp.signal, kind=inp.kind, target=inp.target, body=inp.text,
        session_id=session_id, source=source,
    )
    out = {"ok": True, "id": sid}
    if deja:
        # #684/#685 : le même retour déposé deux fois à onze secondes d'écart.
        # On rend l'id du premier et on le DIT — se taire ferait croire à deux
        # occurrences, refuser ferait perdre son retour à qui redépose de bonne foi.
        out["deja_signale"] = True
        out["deja_signale_hint"] = (
            "ce retour était déjà enregistré, à l'identique, il y a moins de dix "
            "minutes : voici l'identifiant du PREMIER, aucun second n'a été créé. "
            "Si le défaut s'est vraiment reproduit, dis ce qui a changé (autre "
            "ligne, autre paramètre, autre moment) — deux textes identiques ne se "
            "distinguent pas d'un rejeu.")
    return out


# --- Projections de lecture (barreau 4) — opérateur plateforme -------------

class RunsInput(BaseModel):
    limit: int = 100

    @field_validator("limit")
    @classmethod
    def _cap_limit(cls, v):
        return cap_limit(v, 100)


class RunInput(BaseModel):
    run_id: str


class DaysInput(BaseModel):
    days: int = 30


class SignalsInput(BaseModel):
    signal: Optional[str] = None
    target: Optional[str] = None
    # open | acknowledged | declined | resolved, ou 'pending' (= à arbitrer :
    # open ∪ acknowledged), ou None (tous).
    status: Optional[str] = None
    limit: int = 200

    @field_validator("limit")
    @classmethod
    def _cap_limit(cls, v):
        return cap_limit(v, 200)


class SetSignalStatusInput(BaseModel):
    signal_id: int
    status: Literal["open", "acknowledged", "declined", "resolved"]
    note: Optional[str] = None     # ce qui a été décidé, et pourquoi


class SignalRow(BaseModel):
    """Un signal tel qu'il est SERVI. Le corps est de la prose libre écrite par un
    agent : il n'a pas de forme, et n'en aura pas."""
    id: int
    created_at: Optional[str] = None
    sub: Optional[str] = None
    email: Optional[str] = None      # rapporteur (LEFT JOIN users) — NULL si compte parti
    name: Optional[str] = None
    org_id: Optional[int] = None
    signal: Optional[str] = None     # tool_feedback | gap
    kind: Optional[str] = None
    target: Optional[str] = None     # feedback : l'outil ; gap : l'intention
    body: Optional[str] = None
    session_id: Optional[str] = None
    source: Optional[str] = None     # agent | human
    status: Optional[str] = None     # open | acknowledged | declined | resolved
    # Le dernier ARBITRAGE, pas la seule résolution : posé aussi par `acknowledged`
    # et `declined`, effacé par un retour à `open`. Les noms datent des deux états
    # d'origine — c'est `status` qui dit l'état.
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None
    resolution: Optional[str] = None


class SignalsPage(BaseModel):
    """Une page de signaux + l'état de TOUTE la pile.

    Les deux vont ensemble : une page de 200 lignes ne dit pas si la pile en compte
    203 ou 2 000, et c'est ce chiffre qu'on vient chercher. `counts` porte les quatre
    états — ceux à zéro compris — plus `pending` (open ∪ acknowledged), la seule
    question qu'un opérateur pose vraiment."""
    signals: list[SignalRow]
    counts: dict[str, int]


class SignalArbitrated(BaseModel):
    """Résultat d'un arbitrage. `ok: false` + `error: "not_found"` quand l'id
    n'existe pas — un 404 déguisé en 200, forme historique de cette console."""
    ok: bool
    signal: Optional[SignalRow] = None
    counts: Optional[dict[str, int]] = None
    error: Optional[str] = None
    id: Optional[int] = None       # l'id demandé, quand il est introuvable


def _runs(ctx: ResolvedCtx, inp: RunsInput) -> dict:
    return {"runs": db.list_runs(inp.limit)}


def _run(ctx: ResolvedCtx, inp: RunInput) -> dict:
    # `content_archived` (#665) : même forme que la vue d'org (`OrgRun`).
    return {"run_id": inp.run_id, "calls": db.get_run(inp.run_id),
            "content_archived": db.run_content_archived(inp.run_id)}


def _gaps(ctx: ResolvedCtx, inp: DaysInput) -> dict:
    return {"gaps": db.aggregate_gaps(inp.days)}


def _tool_quality(ctx: ResolvedCtx, inp: DaysInput) -> dict:
    return {"tools": db.aggregate_tool_feedback(inp.days)}


def _signals(ctx: ResolvedCtx, inp: SignalsInput) -> dict:
    _valid = set(db.SIGNAL_STATUSES) | {db.SIGNAL_PENDING}
    if inp.status is not None and inp.status not in _valid:
        raise AuthzDenied(
            400, "unknown_status",
            f"statut inconnu {inp.status!r} — filtre par "
            f"{', '.join(sorted(_valid))}, ou omets-le pour tout voir.")
    # Les COMPTES accompagnent toujours la page : une page de 200 lignes ne dit pas
    # si la pile en compte 203 ou 2 000, et c'est ce chiffre qu'on vient chercher en
    # ouvrant la liste.
    return {"signals": db.list_usage_signals(
                inp.signal, inp.target, inp.limit, status=inp.status),
            "counts": db.count_usage_signals_by_status()}


def _set_signal_status(ctx: ResolvedCtx, inp: SetSignalStatusInput) -> dict:
    # Un refus SANS motif est le défaut qu'on vient fermer sous un autre nom : la
    # pile redeviendrait un endroit où des signaux disparaissent sans qu'on sache
    # pourquoi. `resolved` n'exige rien — le travail livré parle de lui-même, et
    # l'exiger ferait écrire « fait » 200 fois.
    if inp.status == "declined" and not (inp.note or "").strip():
        raise AuthzDenied(
            400, "missing_note",
            "Refuser demande un motif : `note` = pourquoi ce signal ne sera pas "
            "traité. Sans lui, un refus est indistinguable d'un oubli.")
    row = db.set_usage_signal_status(
        inp.signal_id, status=inp.status, by=ctx.sub, note=inp.note)
    if row is None:
        return {"ok": False, "error": "not_found", "id": inp.signal_id}
    return {"ok": True, "signal": row, "counts": db.count_usage_signals_by_status()}


class RerouteSignalInput(BaseModel):
    signal_id: int
    # `None` n'est PAS « ne rien changer » : c'est la plateforme, une destination comme
    # une autre pour un signal qui ne concerne aucun espace client. Le champ est requis
    # pour que l'écriture soit toujours un choix, jamais un défaut hérité.
    org_id: Optional[int]


class SignalRerouted(BaseModel):
    """Un signal déplacé d'un espace à un autre. `previous_org_id` dit d'où il vient —
    de quoi vérifier le geste, et le défaire si c'est la destination qu'on a mal tapée.

    `ok: false` + `error: "not_found"` quand l'id n'existe pas : même forme historique
    que l'arbitrage, un 404 déguisé en 200."""
    ok: bool
    signal: Optional[SignalRow] = None
    previous_org_id: Optional[int] = None
    counts: Optional[dict[str, int]] = None
    error: Optional[str] = None
    id: Optional[int] = None


def _reroute_signal(ctx: ResolvedCtx, inp: RerouteSignalInput) -> dict:
    """Corrige l'ORGANISATION d'un signal mal aiguillé (#471).

    Le cas : un signal écrit au sujet d'un espace, déposé sur un autre parce qu'un
    appel avait omis son jeton d'org. Il y restait à jamais — `feedback` écrit sans
    relire, et l'arbitrage pose un état, pas une adresse.

    **Ré-aiguiller plutôt que supprimer**, et c'est la décision du lot : un signal est
    un FAIT (l'agent a réellement buté sur ce manque), et sa ligne en est l'unique
    copie. Le déplacer le retire de l'espace qui n'aurait pas dû le voir ET le rend à
    celui qui aurait dû — les deux lentilles d'org comptent par `org_id`. Le supprimer
    ferait la première moitié, perdrait la seconde, et rouvrirait sous un autre nom la
    porte que `_set_signal_status` referme en exigeant un motif pour tout refus : une
    pile où des lignes disparaissent sans qu'on sache pourquoi.

    L'org cible est VÉRIFIÉE avant l'écriture. Sans ce contrôle, une faute de frappe
    enterrerait le signal dans un espace inexistant — le défaut qu'on répare, en pire :
    plus personne ne le voit et rien ne le dit.
    """
    if inp.org_id is not None and org_store.get_org(int(inp.org_id)) is None:
        raise AuthzDenied(
            404, "unknown_org",
            f"Aucune organisation #{inp.org_id} — vérifie l'id avant de déplacer le "
            f"signal, sinon il devient invisible partout. `org_id: null` le remonte "
            f"au niveau plateforme.")
    row = db.reroute_usage_signal(inp.signal_id, org_id=inp.org_id)
    if row is None:
        return {"ok": False, "error": "not_found", "id": inp.signal_id}
    precedent = row.pop("previous_org_id", None)
    return {"ok": True, "signal": row, "previous_org_id": precedent,
            "counts": db.count_usage_signals_by_status()}


class NotifyReportersInput(BaseModel):
    op: Literal["preview", "send"] = "preview"
    # Restreint l'envoi à ces destinataires (emails ou subs). Vide = tout le monde.
    # Sert à sortir par paliers plutôt que d'un coup sur des tiers.
    only: Optional[list[str]] = None


class ReporterDigest(BaseModel):
    """Ce qu'UNE personne va recevoir (ou vient de recevoir)."""
    sub: str
    email: Optional[str] = None
    name: Optional[str] = None
    brand: str = "oto"
    count: int
    signal_ids: list[int]
    resolved: int = 0
    declined: int = 0
    sent: Optional[bool] = None      # None en aperçu — rien n'a été envoyé
    reason: Optional[str] = None     # pourquoi rien ne partira (adresse inconnue…)


class NotifyReportersOutput(BaseModel):
    op: str
    recipients: list[ReporterDigest]
    total_signals: int
    sent: int = 0                    # nombre d'envois RÉUSSIS (0 en aperçu)


def _group_notices() -> list[dict]:
    """Les retours en attente, REGROUPÉS par personne — l'unité d'envoi.

    Le regroupement vit ici et pas en SQL parce que c'est lui qui porte la décision
    du lot : une personne = un mail, quel que soit le nombre de signaux. En SQL on
    aurait une agrégation à défaire côté Python pour rendre les lignes."""
    par_sub: dict[str, dict] = {}
    for row in db.pending_signal_notices():
        sub = str(row.get("sub"))
        g = par_sub.setdefault(sub, {
            "sub": sub, "email": row.get("email"), "name": row.get("name"),
            # Propriété du DESTINATAIRE (users.locale), pas du signal — la même
            # pour toutes les lignes d'un sub, prise à la première rencontrée
            # (oto-backend#700). None = pas de préférence ⟹ le gabarit sert FR.
            "locale": row.get("locale"),
            "items": [], "resolved": 0, "declined": 0})
        g["items"].append(row)
        if row.get("status") == "declined":
            g["declined"] += 1
        else:
            g["resolved"] += 1
    return list(par_sub.values())


def _notify_reporters(ctx: ResolvedCtx, inp: NotifyReportersInput) -> dict:
    """Rend leur réponse à ceux qui ont signalé — en aperçu, ou pour de bon.

    **L'envoi est un ACTE, jamais un effet de bord.** Ces mails partent chez des
    tiers (partenaires, contributeurs) sous notre marque : `op=preview` est le
    défaut, et il ne touche à rien. C'est aussi ce qui rend le rattrapage d'une pile
    arbitrée sûr — on regarde qui reçoit quoi avant que ça parte.

    `only` restreint aux destinataires nommés (email ou sub) : sortir par paliers
    plutôt que d'un coup. Un envoi qui échoue ne marque PAS ses signaux — ils
    restent dus au prochain passage, et c'est la seule façon qu'un hoquet du mailer
    ne fasse pas disparaître un retour en silence."""
    cible = {str(x).strip().lower() for x in (inp.only or []) if str(x).strip()}
    groupes, envois = [], 0
    for g in _group_notices():
        if cible and not ({str(g["sub"]).lower(), str(g.get("email") or "").lower()} & cible):
            continue
        _base, marque = config.front_for(g["sub"])
        fiche = {
            "sub": g["sub"], "email": g.get("email"), "name": g.get("name"),
            # La marque sous laquelle CE destinataire nous connaît : écrire « oto »
            # à l'utilisateur d'un partenaire est un faux, même si tout le reste est
            # juste.
            "brand": marque or "oto",
            "count": len(g["items"]),
            "signal_ids": [int(i["id"]) for i in g["items"]],
            "resolved": g["resolved"], "declined": g["declined"],
            # TOUJOURS présent, y compris en aperçu (où il vaut None = « rien n'a été
            # tenté »). Un champ absent obligerait le lecteur à deviner s'il manque
            # parce qu'on n'a pas envoyé ou parce que l'envoi a échoué.
            "sent": None, "reason": None,
        }
        if not g.get("email"):
            # Compte supprimé ou sans adresse : on le MONTRE au lieu de perdre
            # l'envoi en silence. Les signaux restent dus.
            fiche["sent"] = False
            fiche["reason"] = "aucune adresse connue pour ce compte"
        elif inp.op == "send":
            ok = mailer.send_signal_digest_email(
                g["email"], items=g["items"], brand=fiche["brand"],
                locale=g.get("locale"),
                # oto#150 : lien de désinscription DU DIGEST, propre à ce canal —
                # `pending_signal_notices` (donc `_group_notices`) exclut déjà qui
                # s'en est servi, ce n'est pas ce paramètre qui filtre quoi que ce
                # soit ici, seulement ce qui PORTE le refus au prochain envoi.
                unsubscribe_url=outreach_optout.lien_digest(g["sub"]))
            fiche["sent"] = bool(ok)
            if ok:
                db.mark_signals_notified(fiche["signal_ids"])
                envois += 1
            else:
                fiche["reason"] = "le mailer a refusé l'envoi — les signaux restent dus"
        groupes.append(fiche)
    return {"op": inp.op, "recipients": groupes,
            "total_signals": sum(g["count"] for g in groupes), "sent": envois}


CAPABILITIES += [
    Capability(
        key="usage.feedback", handler=_feedback, Input=FeedbackInput, authz=SUB_ONLY,
        Output=SignalRecorded,
        description="Report a usage signal about oto. signal='tool_feedback' = feedback on a "
                    "tool you just used (target = the tool name ; kind = bug | misleading_doc | "
                    "wrong_result | praise | other). signal='gap' = a use case oto could NOT do, "
                    "call it whenever you wanted to act but no oto capability covered it "
                    "(target = what you were trying to accomplish ; kind = missing_tool | "
                    "missing_doctrine | missing_data | other). text = optional detail.",
        mcp="feedback", rest=RestBinding("POST", "/api/me/usage/feedback"),
    ),
    # --- projections de lecture (opérateur plateforme) ---------------------
    Capability(key="usage.runs", handler=_runs, Input=RunsInput, authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/usage/runs")),
    Capability(key="usage.run", handler=_run, Input=RunInput, authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/usage/runs/{run_id}")),
    Capability(key="usage.gaps", handler=_gaps, Input=DaysInput, authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/usage/gaps")),
    Capability(key="usage.tool_quality", handler=_tool_quality, Input=DaysInput, authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/usage/tool-quality")),
    Capability(key="usage.signals", handler=_signals, Input=SignalsInput, authz=PLATFORM_ADMIN,
               Output=SignalsPage,
               description="List usage signals (feedback/gap) reported about oto, most recent "
                           "first, plus `counts` per status over the WHOLE table. Filters: "
                           "signal ('tool_feedback'|'gap'), target, status "
                           "('open'|'acknowledged'|'declined'|'resolved', or 'pending' = "
                           "everything not yet arbitrated). Platform-admin only.",
               rest=RestBinding("GET", "/api/admin/usage/signals")),
    Capability(key="usage.set_signal_status", handler=_set_signal_status,
               Input=SetSignalStatusInput, authz=PLATFORM_ADMIN,
               Output=SignalArbitrated,
               description="Arbitrate a usage signal. signal_id = the signal's id (from "
                           "usage.signals). status = open (back to the pile, clears the "
                           "arbitration) | acknowledged (read, not decided yet) | declined "
                           "(won't do — `note` REQUIRED, say why) | resolved (done). "
                           "note = what was decided, and why.",
               rest=RestBinding("POST", "/api/admin/usage/signals/{signal_id}/status")),
    Capability(key="usage.reroute_signal", handler=_reroute_signal,
               Input=RerouteSignalInput, authz=PLATFORM_ADMIN,
               Output=SignalRerouted,
               description="Move a usage signal to the org it was really about. For a "
                           "signal filed against the WRONG workspace — an agent whose "
                           "call omitted its org token — which otherwise stays there "
                           "forever, counted in the lenses of a workspace that should "
                           "never have seen it. signal_id = the signal's id (from "
                           "usage.signals) ; org_id = where it belongs, or null for the "
                           "platform. Only the address moves: body, status and "
                           "arbitration are untouched, so the measure survives — which "
                           "is why signals are rerouted and never deleted. "
                           "Platform-admin only.",
               # La cible se lit dans le CHEMIN (règle de la maison), et le sous-chemin
               # dit ce qui bouge : `/org`, à côté de `/status`. Deux gestes distincts
               # sur la même ligne, deux adresses — un `PATCH` fourre-tout ferait de
               # l'arbitrage et du ré-aiguillage la même opération.
               rest=RestBinding("POST", "/api/admin/usage/signals/{signal_id}/org")),
    Capability(key="usage.notify_reporters", handler=_notify_reporters,
               Input=NotifyReportersInput, authz=PLATFORM_ADMIN,
               Output=NotifyReportersOutput,
               description="Tell the people whose agents reported a signal what was "
                           "decided. ONE grouped email per person, never one per "
                           "signal. op=preview (default, sends NOTHING — shows who "
                           "would get what) / send (actually sends, then marks them "
                           "notified; a failed send stays owed). `only` = restrict to "
                           "these emails/subs, to roll out in stages.",
               rest=RestBinding("POST", "/api/admin/usage/notify-reporters")),
]


class FirstCallsInput(BaseModel):
    org_ids: list[int]

    @field_validator("org_ids", mode="before")
    @classmethod
    def _virgules(cls, v):
        # Une clé unique de query string arrive en CHAÎNE : `?org_ids=12,34` (#367).
        # Répétée (`?org_ids=12&org_ids=34`), elle arrive déjà en liste.
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v


class FirstCallsOut(BaseModel):
    # org (en chaîne : une clé JSON) → horodatage ISO du premier appel, `null` si
    # l'org n'a jamais appelé.
    first_calls: dict[str, Optional[str]]


def _first_calls(ctx: ResolvedCtx, inp: FirstCallsInput) -> dict:
    if len(inp.org_ids) > 1000:
        raise AuthzDenied(400, "too_many_orgs", "1000 orgs au plus par lecture.")
    return {"first_calls": {str(k): v for k, v in db.premiers_appels(inp.org_ids).items()}}


CAPABILITIES += [
    # Le premier appel de chaque org : l'horloge de l'essai d'un tenant qui facture
    # ses orgs (son service d'usage la fige de son côté, le journal expire à ~90 j).
    Capability(key="platform.usage.first_calls", handler=_first_calls, Input=FirstCallsInput,
               Output=FirstCallsOut, authz=PLATFORM_ADMIN,
               description="[platform admin] First journaled call (MCP or REST) of each "
                           "org in `org_ids` — null for an org that never called.",
               rest=RestBinding("GET", "/api/admin/usage/first-calls")),
]
