"""BRANCHER une source sur un agent webhook — `oto_trigger op=connect_source`.

Une source qui ne sait pas poser d'en-tête (Lemlist) reçoit le porteur `otoh_` de
l'agent comme `secret` de son webhook (préréglage `body_secret`). Sans ce geste,
il fallait que le porteur passe par quelqu'un : rendu à l'écran, copié, collé chez
la source — ou, pire, passé en argument d'outil par un modèle, ce que la règle du
dépôt interdit (un secret brut ne passe jamais par un outil).

Ici le SERVEUR fait tout : il fabrique le porteur, l'inscrit chez la source avec la
clé de la source que l'APPELANT résout dans l'org, et ne rend que les identifiants
des webhooks inscrits. Le porteur va de la mémoire du serveur à la source — jamais
dans une réponse, un journal ou un transcript.

## L'ordre, et pourquoi

1. Inscrire chez la source avec le porteur NEUF (et, pour un agent qui n'en a pas,
   l'adresse privée NEUVE), encore en mémoire. Une inscription qui échoue retire
   celles déjà faites : la base n'a pas bougé, l'agent garde sa porte, intacte.
2. Écrire en base, en UNE transaction : adresse, mode `body_secret`, haché du
   porteur, inscriptions (`db.brancher_la_source`) — à condition que les
   inscriptions lues au départ soient toujours celles de la ligne (un branchement
   concurrent, ou l'agent supprimé entre-temps, ne s'écrase pas en silence).
3. Si cette écriture échoue (panne, ou ligne changée), COMPENSER : retirer chez la
   source ce qu'on vient d'y inscrire — sinon un webhook resterait chez le tiers,
   porteur d'un secret qu'aucune ligne ne connaît, et qu'oto ne saurait plus
   retirer. Un retrait qui échoue à son tour est une erreur NOMMÉE
   (`source_cleanup_failed`), journalisée avec les identifiants distants à retirer
   à la main — jamais avalée.
4. Retirer chez la source les inscriptions d'AVANT (un rebranchement) : elles portent
   l'ancien porteur, qui n'ouvre plus rien. Un retrait qui échoue ne défait rien —
   il est RENDU (`remote_cleanup_failed`) et journalisé.

Rebrancher une source déjà branchée est donc la ROTATION du porteur d'un agent
branché : `rotate_secret`, `rotate_address` et le changement de mode sont refusés
sur un agent branché (`source_connected`), parce qu'ils couperaient la source sans
qu'oto puisse lui donner la nouvelle porte.

## Allo : la source SIGNE

Allo ne renvoie pas de secret qu'on lui donne : il SIGNE chaque livraison avec un
secret `whsec_…` qu'IL fabrique (Standard Webhooks, en-têtes `webhook-*`) et ne rend
qu'à la création de l'endpoint. Le même geste, autre porte : UN endpoint pour tous
les événements (Allo prend une liste de `topics`), dont la réponse porte le secret ;
il est chiffré pour SA ligne et posé avec le mode `standard_webhooks`, dans la même
transaction que le reste. Le secret va de la réponse d'Allo à la base, chiffré —
jamais dans une réponse, un journal ou un transcript. Le client d'oto-core le retire
de ses réponses (lu dans Allo) : l'inscription passe donc par son transport, ici seul.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

from .. import db, runner_hook
from ._types import AuthzDenied, ResolvedCtx

logger = logging.getLogger(__name__)

#: Les sources qu'oto sait brancher. Une source de plus = un client qui sait
#: inscrire et retirer un webhook, et le préréglage par lequel elle prouve.
SOURCES = ("lemlist", "allo")

#: Les sources qui SIGNENT (préréglage `standard_webhooks`) : leur secret est le
#: leur, rendu à la création de l'endpoint. Les autres reçoivent notre porteur.
SOURCES_QUI_SIGNENT = ("allo",)

#: Combien d'événements un branchement inscrit, au plus : un webhook par
#: événement chez Lemlist, et chaque inscription est un appel au tiers.
EVENEMENTS_MAX = 10

SOURCE_CONNECTED = (
    "A sender is wired to this agent (`hook_sources`): changing its door would cut "
    "the sender off, with no way for oto to hand it the new one. Re-run "
    "`op=connect_source` to renew the token AND the registration, or "
    "`op=disconnect_source` first to change the door otherwise.")


def exiger_aucune_source(t: Optional[dict]) -> None:
    """Refuse un geste de porte sur un agent branché (voir `SOURCE_CONNECTED`)."""
    if t and t.get("hook_sources"):
        raise AuthzDenied(409, "source_connected", SOURCE_CONNECTED)


def _client(ctx: ResolvedCtx, source: str, account: Optional[str] = None) -> Any:
    """Le client de la source, sur la clé que l'APPELANT résout dans l'org de
    l'agent — la même cascade qu'un outil `lemlist_*` dans cette org.

    `want="byo"` : la clé d'un compte de l'appelant (perso, équipe, org) — jamais un
    rang partagé, qui inscrirait le webhook sur le compte d'un autre.
    `check_usage=False` : configurer une connexion ne consomme pas de quota.
    `account` : le compte NOMMÉ quand l'org en tient plusieurs (deux espaces Lemlist),
    épinglé comme le ferait `_account=` sur un outil `lemlist_*` — sans lui la
    résolution refuse plutôt que de choisir."""
    from .. import access, session_org
    from ..auth import hooks as auth_hooks
    from ..mcp_errors import McpError

    try:
        with auth_hooks.sub_override(ctx.sub):
            jeton = session_org.set_call_org(int(ctx.org_id))
            compte = session_org.set_call_account(account) if account else None
            try:
                rc = access.resolve_credential(source, want="byo", sub=ctx.sub,
                                               check_usage=False)
            finally:
                if compte is not None:
                    session_org.reset_call_account(compte)
                session_org.reset_call_org(jeton)
    except McpError as e:
        # `from e` : la face MCP rend l'erreur d'origine, déjà actionnable. Elle dit
        # `_account=` — le nom de l'axe sur les outils de connecteur ; ici c'est
        # `account` (oto_trigger est un outil du socle, sans axe compte).
        message = e.error.message
        if not account and "_account" in message:
            message += " On `oto_trigger`, name it with `account`."
        raise AuthzDenied(400, "credential_unavailable", message) from e
    if source == "allo":
        from oto.tools.allo import AlloClient
        return AlloClient(api_key=rc.key)
    from oto.tools.lemlist import LemlistClient
    return LemlistClient(api_key=rc.key)


def _url_publique(slug: str) -> str:
    """L'URL que la source appellera. ⚠️ Sans `OTO_MCP_PUBLIC_URL`, l'URL servie à
    un écran est RELATIVE (`_url_du_hook`) — inscrite chez un tiers, elle ne
    répondrait jamais : refusé par son nom plutôt qu'inscrit faux."""
    base = (os.environ.get("OTO_MCP_PUBLIC_URL") or "").rstrip("/")
    if not base:
        raise AuthzDenied(
            503, "public_url_unset",
            "This server does not know its public address (`OTO_MCP_PUBLIC_URL`): "
            "the sender could not reach the agent. Nothing was registered.")
    return f"{base}/api/hooks/{slug}"


def _motif(e: Exception) -> str:
    """Ce qu'on dit d'un refus du tiers. ⚠️ Jamais `str(e)` : le message d'une
    erreur amont peut recopier la requête, donc le porteur qu'elle portait."""
    statut = getattr(e, "status_code", None)
    return f"{type(e).__name__}{f' {statut}' if statut else ''}"


def _retirer(client: Any, inscrites: list) -> list:
    """Retire chez la source chaque inscription ; rend celles qui ont RÉSISTÉ."""
    restees = []
    for ins in inscrites:
        try:
            client.delete_webhook(ins["remote_id"])
        except Exception as e:  # noqa: SILENT — journalisé, et rendu à l'appelant
            logger.warning("webhook %s chez %s : retrait impossible (%s)",
                           ins.get("remote_id"), ins.get("source"), _motif(e))
            restees.append({**ins, "error": _motif(e)})
    return restees


def _retirer_partout(ctx: ResolvedCtx, t: dict, inscrites: list,
                     clients: Optional[dict] = None) -> list:
    """Retire chaque inscription avec la clé qui l'a POSÉE — sa source et son compte
    (`account`, absent avant qu'on le retienne : la clé par défaut, comme alors).
    `clients` : ceux déjà ouverts, par (source, compte). Rend ce qui a RÉSISTÉ ; une
    source inconnue ou sans clé n'empêche pas de retirer le reste."""
    clients = dict(clients or {})
    lots: dict = {}
    for ins in inscrites:
        lots.setdefault((ins.get("source"), ins.get("account")), []).append(ins)
    restees = []
    for (source, compte), lot in lots.items():
        if source not in SOURCES:
            logger.warning("webhook %s : source inconnue `%s`, %s non retiré(s)",
                           t["id"], source, _ids(lot))
            restees += [{**i, "error": "unknown_source"} for i in lot]
            continue
        client = clients.get((source, compte))
        if client is None:
            try:
                client = _client(ctx, source, compte)
            except AuthzDenied as e:
                # Sans clé, rien ne se retire — mais `delete` ne doit pas en être
                # bloqué : ce qui reste est rendu, avec la raison, et journalisé.
                logger.warning("webhook %s : aucune clé %s%s pour %s (%s), %s non "
                               "retiré(s)", t["id"], source,
                               f" ({compte})" if compte else "", ctx.sub, e.code,
                               _ids(lot))
                restees += [{**i, "error": e.code} for i in lot]
                continue
        restees += _retirer(client, lot)
    return restees


def _ids(inscrites: list) -> str:
    return ", ".join(str(i.get("remote_id")) for i in inscrites)


def _compenser(ctx: ResolvedCtx, t: dict, client: Any, source: str, inscrites: list,
               cause: str) -> None:
    """Retire chez la source les inscriptions qu'aucune ligne ne retiendra. Si l'une
    résiste, LÈVE `source_cleanup_failed` en la nommant — un webhook orphelin chez
    le tiers porte un secret qu'oto ne connaît plus : il ne doit pas passer inaperçu."""
    restees = _retirer(client, inscrites)
    if not restees:
        return
    logger.error("webhook %s (org %s) : %s — %d webhook(s) ORPHELIN(S) chez %s, à "
                 "retirer à la main : %s", t["id"], ctx.org_id, cause, len(restees),
                 source, _ids(restees))
    raise AuthzDenied(
        502, "source_cleanup_failed",
        f"Wiring failed ({cause}) and {source} refused to remove {len(restees)} "
        f"webhook(s) oto had just registered: {_ids(restees)}. They hold a token "
        "that opens nothing; remove them in the sender's settings.")


def _evenements(source: str, events: Optional[list]) -> list:
    vus = []
    for ev in events or []:
        ev = (ev or "").strip() if isinstance(ev, str) else ""
        if ev and ev not in vus:
            vus.append(ev)
    if not vus:
        raise AuthzDenied(
            400, "events_required",
            "`events`: at least one of the sender's events (Lemlist: "
            "`emailsReplied`, `linkedinReplied`…). No default: subscribing to "
            "EVERYTHING would start the agent on every email open.")
    if len(vus) > EVENEMENTS_MAX:
        raise AuthzDenied(400, "too_many_events",
                          f"`events`: at most {EVENEMENTS_MAX} per wiring.")
    if source == "allo":
        # Refusé ICI, par son nom, plutôt que par Allo après un aller-retour.
        from oto.tools.allo.const import WEBHOOK_TOPICS
        inconnus = [ev for ev in vus if ev not in WEBHOOK_TOPICS]
        if inconnus:
            raise AuthzDenied(400, "unknown_event",
                              f"`events`: `{inconnus[0]}` is not an Allo topic — one of "
                              f"{', '.join(WEBHOOK_TOPICS)}.")
    return vus


def _inscrire(ctx: ResolvedCtx, t: dict, client: Any, source: str, url: str,
              porteur: str, evenements: list, campaign_id: Optional[str],
              is_first: Optional[bool]) -> list:
    """Inscrit un webhook par événement. Un refus (ou une réponse sans identifiant)
    retire celles déjà faites et lève `source_registration_failed`."""
    inscrites: list = []
    for ev in evenements:
        motif = None
        try:
            r = client.add_webhook(url, type=ev, secret=porteur,
                                   campaign_id=campaign_id, is_first=is_first)
        except Exception as e:  # noqa: SILENT — journalisé, défait, et refusé par son nom
            motif = _motif(e)
            logger.warning("webhook %s : inscription `%s` chez %s refusée (%s)",
                           t["id"], ev, source, motif)
        else:
            remote_id = r.get("_id") if isinstance(r, dict) else None
            if remote_id:
                inscrites.append({"source": source, "remote_id": remote_id,
                                  "event": ev, "campaign_id": campaign_id})
                continue
            # Sans identifiant, oto ne saurait pas le retirer : refusé comme un échec.
            motif = "no webhook id returned"
            logger.error("webhook %s : %s a inscrit `%s` SANS rendre d'identifiant — "
                         "un webhook peut rester chez la source, à vérifier à la main",
                         t["id"], source, ev)
        _compenser(ctx, t, client, source, inscrites,
                   f"registration of `{ev}` refused ({motif})")
        raise AuthzDenied(
            502, "source_registration_failed",
            f"{source} refused to register `{ev}` ({motif}): nothing changed on the "
            "agent. Check the event name and the sender's key.")
    return inscrites


def _inscrire_allo(ctx: ResolvedCtx, t: dict, client: Any, url: str,
                   evenements: list) -> tuple[list, str]:
    """UN endpoint Allo pour tous les événements ; rend l'inscription et le secret de
    signature qu'Allo vient de fabriquer. ⚠️ Par le transport du client : ses
    méthodes retirent `signing_secret` de leurs réponses, et il n'est rendu qu'ici.
    Une réponse sans identifiant, ou sans secret utilisable, est un échec : retiré
    chez Allo s'il y a de quoi, et refusé par son nom."""
    try:
        r = client._request("POST", "/v2/api/webhooks", body={
            "url": url, "topics": evenements,
            "description": f"oto agent {t['id']}"})
    except Exception as e:  # noqa: SILENT — journalisé, et refusé par son nom
        motif = _motif(e)
        logger.warning("webhook %s : inscription chez allo refusée (%s)", t["id"], motif)
        raise AuthzDenied(
            502, "source_registration_failed",
            f"allo refused to register the webhook ({motif}): nothing changed on the "
            "agent. Check the sender's key (an admin key can manage webhooks).") from None
    donnees = (r or {}).get("data") if isinstance(r, dict) else None
    donnees = donnees if isinstance(donnees, dict) else {}
    remote_id = donnees.get("id")
    secret = donnees.get("signing_secret")
    if not remote_id:
        logger.error("webhook %s : allo a inscrit l'endpoint SANS rendre d'identifiant — "
                     "un webhook peut rester chez allo, à vérifier à la main", t["id"])
        raise AuthzDenied(
            502, "source_registration_failed",
            "allo answered without a webhook id: nothing changed on the agent. Check "
            "Allo's webhook settings for an endpoint to this agent's address.")
    inscrite = {"source": "allo", "remote_id": remote_id,
                "event": ", ".join(evenements), "campaign_id": None}
    if not (isinstance(secret, str) and runner_hook.cle_de_signature(secret)):
        _compenser(ctx, t, client, "allo", [inscrite],
                   "no usable signing secret returned")
        raise AuthzDenied(
            502, "source_registration_failed",
            "allo answered without a usable signing secret (`whsec_…`): the endpoint "
            "was removed, nothing changed on the agent.")
    return [inscrite], secret


def connecter(ctx: ResolvedCtx, t: dict, *, source: Optional[str],
              events: Optional[list], campaign_id: Optional[str],
              is_first: Optional[bool], account: Optional[str] = None) -> dict:
    """Branche `source` sur l'agent `t` (porte DÉJÀ vérifiée par l'appelant).

    Rend `{"connected": [...], "remote_cleanup_failed": [...]}` — sans porteur."""
    if source not in SOURCES:
        raise AuthzDenied(400, "unknown_source",
                          f"`source`: one of {', '.join(SOURCES)}.")
    signe = source in SOURCES_QUI_SIGNENT
    if signe and (campaign_id or is_first is not None):
        raise AuthzDenied(400, "option_not_supported",
                          f"`campaign_id` and `is_first` are Lemlist's; {source} "
                          "subscribes to whole event topics.")
    evenements = _evenements(source, events)
    # L'adresse privée AVANT l'inscription, mais en MÉMOIRE : la source garde l'URL
    # qu'on lui donne, et une adresse numérique se parcourt. Posée en base avec le
    # reste (`brancher_la_source`) : un branchement refusé ne la pose pas — elle
    # remplacerait l'adresse numérique, qu'une autre source utilise peut-être.
    slug = t.get("hook_slug") or runner_hook.nouvelle_adresse()
    url = _url_publique(slug)
    client = _client(ctx, source, account)
    # Un porteur NEUF dans les deux cas : en mode signature il n'ouvre rien (le
    # porteur y est refusé), mais l'ancien, s'il traînait quelque part, cesse de valoir.
    porteur, hache = runner_hook.nouveau_secret()
    secret_enc = None
    if signe:
        inscrites, secret = _inscrire_allo(ctx, t, client, url, evenements)
        try:
            secret_enc = runner_hook.chiffrer_secret_de_signature(t["id"], secret)
        except Exception as e:
            _compenser(ctx, t, client, source, inscrites,
                       f"secret encryption failed ({type(e).__name__})")
            raise
        finally:
            del secret
    else:
        inscrites = _inscrire(ctx, t, client, source, url, porteur, evenements,
                              campaign_id, is_first)
    if account:
        # Retenu avec chaque inscription : débrancher et supprimer la retireront avec
        # la clé qui l'a posée, pas avec celle que l'org résoudrait par défaut.
        inscrites = [{**i, "account": account} for i in inscrites]

    anciennes = list(t.get("hook_sources") or [])
    try:
        ecrit = db.brancher_la_source(
            t["id"], ctx.org_id,
            hook_auth=runner_hook.STANDARD_WEBHOOKS if signe else runner_hook.BODY_SECRET,
            secret_hash=hache, signing_secret_enc=secret_enc,
            hook_slug=slug, sources=inscrites, sources_avant=anciennes or None)
    except Exception as e:
        logger.error("webhook %s (org %s) : écriture du branchement en échec (%s) — "
                     "retrait chez %s de %s", t["id"], ctx.org_id, type(e).__name__,
                     source, _ids(inscrites))
        _compenser(ctx, t, client, source, inscrites,
                   f"database write failed ({type(e).__name__})")
        raise
    if not ecrit:
        # Supprimé, ou rebranché par un autre geste entre la lecture et l'écriture.
        logger.warning("webhook %s (org %s) : l'agent a changé pendant le branchement "
                       "— retrait chez %s de %s", t["id"], ctx.org_id, source,
                       _ids(inscrites))
        _compenser(ctx, t, client, source, inscrites, "the agent changed meanwhile")
        raise AuthzDenied(
            409, "trigger_changed",
            "The agent was changed or deleted while being wired: the registrations "
            "just made were removed at the sender. Read it again, then retry.")
    logger.warning("webhook %s (org %s) : source %s BRANCHÉE par %s — %d événement(s), "
                   "%s", t["id"], ctx.org_id, source,
                   ctx.sub, len(evenements),
                   "secret de signature chiffré posé" if signe
                   else "porteur neuf posé chez la source")
    restees = _retirer_partout(ctx, t, [a for a in anciennes if a.get("remote_id")],
                               clients={(source, account): client})
    return {"connected": inscrites, "remote_cleanup_failed": restees}


def deconnecter(ctx: ResolvedCtx, t: dict) -> dict:
    """Retire chez la source tout ce qui est inscrit pour l'agent `t` (porte DÉJÀ
    vérifiée). Ce qui résiste RESTE dans `hook_sources` — oto doit pouvoir le
    retirer plus tard — et est rendu. Le mode et le porteur ne changent pas."""
    inscrites = list(t.get("hook_sources") or [])
    if not inscrites:
        return {"disconnected": [], "remote_cleanup_failed": []}
    restees = _retirer_partout(ctx, t, inscrites)
    ids_restes = {r.get("remote_id") for r in restees}
    if not db.poser_sources_de_hook(
            t["id"], ctx.org_id,
            [i for i in inscrites if i.get("remote_id") in ids_restes],
            sources_avant=inscrites):
        # Rebranché (ou supprimé) entre la lecture et l'écriture : la ligne porte
        # d'AUTRES inscriptions, qu'on n'écrase pas — elles se retireraient plus.
        logger.warning("webhook %s (org %s) : l'agent a changé pendant le débranchement "
                       "— %d retirée(s) chez la source, ligne laissée telle quelle",
                       t["id"], ctx.org_id, len(inscrites) - len(restees))
        raise AuthzDenied(
            409, "trigger_changed",
            "The agent was changed or deleted while being disconnected: its current "
            "registrations were left as they are. Read it again, then retry.")
    logger.warning("webhook %s (org %s) : source(s) DÉBRANCHÉE(S) par %s — %d retirée(s), "
                   "%d restée(s)", t["id"], ctx.org_id, ctx.sub,
                   len(inscrites) - len(restees), len(restees))
    return {"disconnected": [i for i in inscrites if i.get("remote_id") not in ids_restes],
            "remote_cleanup_failed": restees}
