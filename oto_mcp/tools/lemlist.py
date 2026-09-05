"""Lemlist — campagnes, séquences, plannings, leads, stats et enrichissement.

Premier des DEUX modules du connecteur : celui-ci tient la CAMPAGNE et ses
leads, `lemlist_crm.py` tient tout le reste (CRM, inbox, désinscriptions, watch
lists, tâches, base partagée, équipe, boîtes mail, lemwarm, délivrabilité,
webhooks). Ensemble ils couvrent les 141 routes documentées de lemlist, sans
exception — et deux inventaires le prouvent plutôt que de l'affirmer :
`test_lemlist_coverage.py` côté oto-core (chaque route a un chemin dans le
client), `test_lemlist_surface_coverage.py` ici (chaque capacité du client est
appelée par un tool, les rares exceptions étant nommées avec leur raison).

Le connecteur savait LIRE une campagne et y poser des leads ; il ne savait pas
en conduire une. `lemlist_campaign`, `lemlist_campaign_start`,
`lemlist_sequence`, `lemlist_schedule` et `lemlist_lead` ferment ce trou : créer
et régler une campagne, écrire sa séquence pas à pas, tenir ses fenêtres
d'envoi, la valider, la démarrer, la mettre en pause, la dupliquer, la mesurer,
l'exporter, et mener ses leads de bout en bout.

La borne n'a pas disparu, elle s'est DÉPLACÉE là où elle mord vraiment : sur ce
qui met des messages sur le fil, pas sur l'écriture en général. QUATRE tools le
font ou l'arment, et tous les quatre sont masqués par défaut
(`DEFAULT_HIDDEN_TOOLS`, self-activables) : `lemlist_campaign_start` (lemlist
déroule la séquence pour tous les leads lancés), `lemlist_launch_lead` (un lead
sort de la revue), `lemlist_inbox_send` (les trois envois directs, sans campagne
ni revue devant eux) et `lemlist_campaign_auto_review`. Tout le reste — créer,
régler, dupliquer, pauser, planifier, ranger le CRM — travaille sur un BROUILLON
ou sur de la donnée, et n'envoie rien.

C'est ce grain-là qui a dicté le découpage : `DEFAULT_HIDDEN_TOOLS` a le grain du
TOOL, pas de l'`op`. `start` en `lemlist_campaign(op="start")` serait rentré dans
un tool visible et aurait dégondé la borne en silence — d'où un tool nu pour lui
seul, et de même pour les envois d'inbox.

Corollaire, et c'est le point le moins évident du module : `autoReview` /
`autoReviewConditions` ne passent PAS par le dict de réglages de
`lemlist_campaign`. Le champ n'est pas retiré du connecteur pour autant — il a
son propre tool masqué, `lemlist_campaign_auto_review`. La raison : ce réglage
lance tout lead dès son ajout, donc il ferait de `lemlist_create_lead` — visible,
et visible PARCE QU'il n'envoie rien — un chemin d'envoi, sans qu'aucun tool
masqué soit appelé. Le champ reste atteignable ; c'est le GESTE qui devient
explicite.

L'enrichissement (`lemlist_enrich`, `lemlist_enrich_lead`) n'envoie rien — mais
il DÉPENSE des crédits lemlist à chaque action. D'où la même borne, prise
autrement : aucune action par défaut, un appel sans action demandée échoue ici
(INVALID_PARAMS) plutôt que d'aller chercher le 400 documenté de lemlist.

Surface async, comme FullEnrich (signal #252) : le POST rend un `enrichment_id`
en ~1s et le travail continue côté lemlist. Le polling appartient à l'agent —
`lemlist_enrich_result` relève un statut et rend la main. Jamais de boucle
d'attente in-process : tout client MCP raccroche vers 60s, et le résultat
serait perdu ALORS QUE les crédits, eux, sont consommés.

Clé résolue par appel via `access.resolve_api_key("lemlist")`. Pas de
quota plateforme par défaut — chaque user voit SES propres campagnes,
donc user key obligatoire.
"""
from __future__ import annotations

from dataclasses import asdict
import datetime as _dt
from typing import Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..output_projection import project

#: Vocabulaire d'actions du bulk v2 de lemlist. Volontairement écrit à la main :
#: ce n'est PAS un snake_case des flags v1 — la vérification d'email s'appelle
#: `verify` et non `verify_email`. Miroir de `LemlistClient.ENRICH_BULK_ACTIONS`,
#: gardé aligné par un test de version-skew.
BULK_ACTIONS = {
    "find_email": "find_email",
    "verify_email": "verify",
    "linkedin_enrichment": "linkedin_enrichment",
    "find_phone": "find_phone",
}


#: Les DEUX réglages de campagne qui dissolvent la revue manuelle : avec eux, un
#: lead créé part TOUT DE SUITE. Le modèle de sûreté du connecteur repose sur
#: l'inverse — `lemlist_create_lead` est visible parce qu'il n'envoie rien, et
#: seul `lemlist_launch_lead` (masqué par défaut) déclenche l'envoi. Les laisser
#: passer dans un dict de réglages transformerait un tool visible en chemin
#: d'envoi, sans que rien ne le signale. Ils ne sont pas retirés du connecteur
#: pour autant — ils ont leur propre tool masqué, `lemlist_campaign_auto_review` :
#: le champ reste atteignable, c'est le GESTE qui devient explicite.
AUTO_REVIEW_KEYS = ("autoReview", "autoReviewConditions")

#: Plancher de la fenêtre de stats. Les deux dates sont OBLIGATOIRES côté lemlist
#: et un agent qui veut « les stats de la campagne » n'en a aucune en tête : ce
#: plancher précède lemlist lui-même, donc il vaut « depuis toujours ».
STATS_EPOCH = "2015-01-01T00:00:00.000Z"

#: Détail rendu sur `full=True` seulement — un `steps` par étape de séquence et
#: un `perChannel` par canal, là où la question courante tient dans les compteurs
#: de tête.
STATS_DETAIL = ("steps", "perChannel")


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _default_window(start_date: Optional[str], end_date: Optional[str]) -> tuple[str, str]:
    """Complète la fenêtre de stats — bornes ISO 8601, les deux exigées upstream."""
    # Aliasé `_dt` : `timezone` est un ARGUMENT de `lemlist_campaign`/
    # `lemlist_schedule` (la zone IANA de lemlist), l'importer nu le masquerait.
    now = _dt.datetime.now(_dt.timezone.utc)
    return start_date or STATS_EPOCH, end_date or (
        now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z")


def _project_stats(result: dict, *, full: bool) -> dict:
    """Coupe le détail par étape/par canal, et NOMME ce qui a été écarté."""
    if full or not isinstance(result, dict):
        return result
    dropped = {k: len(result[k]) for k in STATS_DETAIL
               if isinstance(result.get(k), (list, dict))}
    out = project(result, drop=STATS_DETAIL)
    if dropped:
        out["projection"] = {
            "dropped": dropped,
            "hint": "Détail par étape / par canal écarté — `full=True` le rend.",
        }
    return out


#: Plafond de l'audio d'une note vocale — 20 Mo, la limite que lemlist annonce
#: sur ses médias. La borne est ici parce que c'est NOUS qui téléchargeons : un
#: agent MCP n'a pas de disque partagé avec le serveur.
AUDIO_MAX_BYTES = 20 * 1024 * 1024


def _fetch_audio(source) -> tuple[bytes, str]:
    """Ramène l'audio d'une note vocale, par le seam PARTAGÉ `file_source`.

    Écrit à la main au premier jet, cette fonction refaisait une garde de taille
    et un contrôle de schéma — mais PAS l'anti-SSRF : une URL fournie par un
    agent aurait pu faire lire au serveur `localhost` ou l'IMDS cloud. Le seam
    (`file_source.resolve`, déjà utilisé par lighton et pennylane) porte cette
    garde, refuse les redirections, et accepte en prime `{"kind": "drive"}` et
    `{"kind": "gmail"}` — l'audio peut donc venir d'un Drive ou d'une pièce
    jointe, pas seulement d'une URL publique.

    Rend `(octets, nom de fichier)` : le nom vient de la SOURCE (pièce jointe,
    fichier Drive, dernier segment d'URL) et part en multipart. Le laisser
    tomber ferait arriver toutes les notes vocales sous le même nom générique
    côté lemlist.
    """
    from .. import file_source

    if isinstance(source, str):
        source = {"kind": "url", "url": source}
    try:
        resolved = file_source.resolve(source, max_bytes=AUDIO_MAX_BYTES)
    except file_source.FileSourceError as e:
        raise _bad(f"audio illisible : {e}")
    return resolved.data, resolved.filename or "audio.mp3"


def _refuse_auto_review(settings: dict) -> None:
    """Refuse `autoReview*` — cf. AUTO_REVIEW_KEYS."""
    present = [k for k in AUTO_REVIEW_KEYS if k in settings]
    if present:
        raise _bad(
            f"{', '.join(present)} ne se règle pas ici. Ce réglage fait partir "
            "tout lead ajouté SANS revue : il transformerait `lemlist_create_lead` "
            "en envoi. Il a son propre tool, `lemlist_campaign_auto_review`, masqué "
            "par défaut (`oto_enable_tool lemlist_campaign_auto_review`) — armer "
            "l'envoi demande un geste délibéré, pas une clé qui passe dans un dict "
            "de réglages."
        )


def _found_digest(data: dict) -> dict:
    """Ce qui a VRAIMENT été trouvé, par axe — `data` porte toujours la clé de
    l'axe demandé, même vide, donc sa seule présence ne dit rien.

    Formes relevées en live (au-delà du schéma publié) : `email` porte `email`
    et un `status` de vérification (`deliverable`/`undeliverable`), `phone`
    porte `phone`, `linkedin` porte un profil complet — ou `{}` quand le profil
    n'a pas pu être résolu. `notFound` n'est PAS fiable : on l'a vu à `false`
    sur une charge sans numéro.
    """
    found = {}
    email = (data.get("email") or {}).get("email")
    if email:
        found["email"] = email
    status = (data.get("email") or {}).get("status")
    if status:
        found["email_status"] = status
    phone = (data.get("phone") or {}).get("phone")
    if phone:
        found["phone"] = phone
    linkedin = data.get("linkedin") or {}
    if linkedin:
        found["linkedin"] = {
            k: v for k, v in linkedin.items()
            if k in ("firstName", "lastName", "tagline", "locationName", "linkedinUrl")
        } or True
    return found


def register(mcp: FastMCP) -> None:
    from oto.tools.lemlist import LemlistClient

    def _client() -> tuple[LemlistClient, bool]:
        key, is_platform = access.resolve_api_key("lemlist")
        return LemlistClient(api_key=key), is_platform

    def _record_if_platform(is_platform: bool) -> None:
        if is_platform:
            access.record_platform_usage("lemlist")

    @mcp.tool()
    def lemlist_status() -> dict:
        """Workspace status (account, credits, plan)."""
        client, is_platform = _client()
        result = client.status()
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    def lemlist_list_campaigns(
        status: Optional[str] = None,
        created_by: Optional[str] = None,
        newest_first: bool = False,
        max_campaigns: int = 500,
    ) -> dict:
        """List the campaigns of the workspace.

        Returns `{campaigns: [{id, name, status, senders, emoji, labels,
        timezone, created_at, created_by, has_error, errors}], count,
        truncated}`. `truncated: true` means the ceiling was hit and the list is
        INCOMPLETE — do not conclude a campaign is absent from it.

        Args:
            status: Keep only `running`, `draft`, `archived`, `ended`, `paused`
                or `errors`. A campaign can hold several at once (paused WITH
                errors), so this filters, it does not partition.
            created_by: Keep only campaigns created by a user id (`usr_…`).
            newest_first: Sort on creation date, most recent first.
            max_campaigns: Ceiling on the walk (lemlist pages 100 at a time).
        """
        client, is_platform = _client()
        filters = {}
        if status is not None:
            filters["status"] = status
        if created_by is not None:
            filters["created_by"] = created_by
        if newest_first:
            filters["sort_order"] = "desc"
        pages = max(1, -(-max_campaigns // 100))  # ceil
        campaigns, truncated = client.list_all_campaigns(max_pages=pages, **filters)
        _record_if_platform(is_platform)
        return {
            "campaigns": [asdict(c) for c in campaigns],
            "count": len(campaigns),
            "truncated": truncated,
        }

    @mcp.tool()
    def lemlist_get_campaign(campaign_id: str) -> dict:
        """Fetch full campaign details by ID."""
        client, is_platform = _client()
        result = client.get_campaign(campaign_id)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    def lemlist_get_campaign_stats(
        campaign_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        channels: Optional[list[str]] = None,
        ab_selected: Optional[str] = None,
        send_user: Optional[str] = None,
        full: bool = False,
    ) -> dict:
        """Campaign performance (leads reached, opened, replied, bounced…).

        Reads lemlist's own counters. Previously derived from one page of
        activities, which under-counted every campaign past 1000 events — the
        field names changed with the fix (`nbLeads`, `messagesSent`, `opened`,
        `replied`… instead of `emails_sent` & co).

        Args:
            start_date / end_date: ISO 8601 window. Defaults to "since 2015" →
                now, i.e. the campaign's whole life.
            channels: Any of `email`, `linkedin`, `others`.
            ab_selected: `A` or `B`, to read one side of a running A/B test.
            send_user: `usr_…|sender@email` — both halves required.
            full: Also return the per-step (`steps`) and per-channel
                (`perChannel`) breakdowns, dropped by default for size.
        """
        client, is_platform = _client()
        start, end = _default_window(start_date, end_date)
        result = client.get_campaign_stats_v2(
            campaign_id, start_date=start, end_date=end,
            channels=channels, ab_selected=ab_selected, send_user=send_user,
        )
        _record_if_platform(is_platform)
        return _project_stats(result, full=full)

    @mcp.tool()
    def lemlist_get_activities(
        campaign_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        activity_type: Optional[str] = None,
        lead_id: Optional[str] = None,
        is_first: Optional[bool] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        min_date: Optional[str] = None,
        max_date: Optional[str] = None,
        all_pages: bool = False,
        since: Optional[str] = None,
        max_pages: int = 50,
    ) -> dict:
        """Get activity events (opens, clicks, replies, LinkedIn actions…).

        Args:
            campaign_id: Restrict to a campaign.
            limit: Max events (default 100).
            offset: Pagination offset.
            activity_type: One event name (`emailsSent`, `emailsOpened`,
                `emailsReplied`, `linkedinInviteAccepted`, `paused`…).
            lead_id: Restrict to one lead.
            is_first: Keep only the first event of its kind per lead.
            start_date / end_date: ISO 8601 bounds.
            min_date / max_date: the OTHER documented pair of bounds — lemlist
                exposes both on this route; kept distinct rather than guessed
                into one.
            all_pages: Walk the pages instead of returning one. Use `since`
                (ISO date) to stop early, and `max_pages` to cap the cost
                (default 50 = 5 000 events). Ignores the other filters — the
                paging route only takes the campaign and the date floor.
            since: With `all_pages`, keep only what is newer than this date.
            max_pages: Ceiling on the walk.
        """
        client, is_platform = _client()
        if all_pages:
            events = client.sync_activities(
                campaign_id=campaign_id, since=since, max_pages=max_pages)
        else:
            events = client.get_activities(
                campaign_id=campaign_id, limit=limit, offset=offset,
                type=activity_type, lead_id=lead_id, is_first=is_first,
                start_date=start_date, end_date=end_date,
                min_date=min_date, max_date=max_date,
            )
        _record_if_platform(is_platform)
        return {"activities": events, "count": len(events)}

    @mcp.tool()
    def lemlist_get_leads(campaign_id: str) -> dict:
        """List all leads for a campaign with their state (sent, replied…).

        ⚠️ Passe par l'export JSON, PAS par `get_all_leads` : ce dernier appelle
        l'export sans `state`, donc avec le défaut de lemlist — qui filtre tout et
        rend une liste vide se lisant « pas de leads ». Une campagne d'un lead
        revenait ainsi vide (signal 719) alors que la route unitaire le rendait très
        bien, et le guide annonçait le forçage comme acquis pour tout le connecteur.
        `export_campaign_leads` porte le défaut `state="all"`, vérifié en live le
        2026-08-31 : on prend la surface qui a la garde plutôt que d'en refaire une.
        """
        client, is_platform = _client()
        exported = client.export_campaign_leads(campaign_id, format="json")
        _record_if_platform(is_platform)
        return {"leads": exported if isinstance(exported, list)
                else (exported or {}).get("leads", exported)}

    @mcp.tool()
    def lemlist_create_lead(
        campaign_id: str,
        email: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        company_name: Optional[str] = None,
        job_title: Optional[str] = None,
        linkedin_url: Optional[str] = None,
        phone: Optional[str] = None,
        company_domain: Optional[str] = None,
        icebreaker: Optional[str] = None,
        timezone: Optional[str] = None,
        contact_owner: Optional[str] = None,
        custom_variables: Optional[dict] = None,
        deduplicate: bool = False,
        linkedin_enrichment: bool = False,
        find_email: bool = False,
        verify_email: bool = False,
        find_phone: bool = False,
    ) -> dict:
        """Create a lead in a campaign.

        All lead fields are optional (lemlist accepts phone/LinkedIn-only
        leads), but you'll usually pass at least `email` or `linkedin_url`.
        `custom_variables` merges extra key-value pairs into the lead, used for
        campaign personalization (e.g. `{{variableName}}` in a template).

        Enrichment flags (all default False, each may cost lemlist credits):
        `deduplicate` skips the insert if the email already exists in another
        campaign, `linkedin_enrichment` runs LinkedIn enrichment,
        `find_email`/`verify_email` find or verify the email, `find_phone`
        finds a phone number.

        Returns the created lead, including `_id` — pass it to
        `lemlist_launch_lead`/`lemlist_add_lead_variables`. If the campaign has
        review-before-send enabled, the lead is created paused and won't send
        until `lemlist_launch_lead` is called.
        """
        lead = {
            k: v for k, v in {
                "email": email,
                "firstName": first_name,
                "lastName": last_name,
                "companyName": company_name,
                "jobTitle": job_title,
                "linkedinUrl": linkedin_url,
                "phone": phone,
                "companyDomain": company_domain,
                "icebreaker": icebreaker,
                "timezone": timezone,
                "contactOwner": contact_owner,
            }.items() if v is not None
        }
        if custom_variables:
            lead.update(custom_variables)
        client, is_platform = _client()
        result = client.create_lead(
            campaign_id, lead,
            deduplicate=deduplicate, linkedin_enrichment=linkedin_enrichment,
            find_email=find_email, verify_email=verify_email, find_phone=find_phone,
        )
        _record_if_platform(is_platform)
        return result

    def _require_action(**flags: bool) -> None:
        if not any(flags.values()):
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=("No enrichment requested — set at least one of "
                         "find_email, verify_email, linkedin_enrichment, find_phone."),
            ))

    @mcp.tool()
    def lemlist_enrich(
        email: Optional[str] = None,
        linkedin_url: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        company_name: Optional[str] = None,
        company_domain: Optional[str] = None,
        find_email: bool = False,
        verify_email: bool = False,
        linkedin_enrichment: bool = False,
        find_phone: bool = False,
        webhook_url: Optional[str] = None,
    ) -> dict:
        """Submit an ASYNC enrichment on a person — no campaign, no lead needed.

        Returns immediately with an `enrichment_id`; the work runs server-side.
        THEN call `lemlist_enrich_result(enrichment_id)` to collect it (first
        poll after ~10-20s, then every ~15-30s until `done`).

        Args:
            email, linkedin_url, first_name, last_name, company_name,
                company_domain: the identity to enrich. All optional, but
                lemlist only resolves what it can match — pass a LinkedIn URL,
                or a first/last name together with a company domain.

        
        Enrichment actions — at least one is required, and each spends lemlist
        credits: `find_email` finds a verified email, `verify_email` verifies the
        email you passed (debounce), `linkedin_enrichment` runs the LinkedIn
        enrichment, `find_phone` finds a phone number. Ask only for what you need.
        """
        _require_action(
            find_email=find_email, verify_email=verify_email,
            linkedin_enrichment=linkedin_enrichment, find_phone=find_phone,
        )
        client, is_platform = _client()
        result = client.enrich(
            email=email, linkedin_url=linkedin_url,
            first_name=first_name, last_name=last_name,
            company_name=company_name, company_domain=company_domain,
            find_email=find_email, verify_email=verify_email,
            linkedin_enrichment=linkedin_enrichment, find_phone=find_phone,
            webhook_url=webhook_url,
        )
        _record_if_platform(is_platform)
        return {
            "enrichment_id": result.get("id"),
            "next_step": ("Enrichment accepted. Call "
                          "lemlist_enrich_result(enrichment_id) in ~10-20s."),
        }

    @mcp.tool()
    def lemlist_enrich_lead(
        lead_id: str,
        find_email: bool = False,
        verify_email: bool = False,
        linkedin_enrichment: bool = False,
        find_phone: bool = False,
        webhook_url: Optional[str] = None,
    ) -> dict:
        """Enrich a lead that is ALREADY in a campaign, in place.

        Same actions as `lemlist_enrich`, but the identity comes from the
        existing lead and lemlist writes the result back onto it. Async too:
        returns an `enrichment_id` for `lemlist_enrich_result`.

        Only works on a lead still AWAITING REVIEW — lemlist answers
        `400 "lemrich is not available for lead reviewed"` once a lead has been
        reviewed, which is the state of every lead in a campaign without
        review-before-send. For anyone else, enrich the person with
        `lemlist_enrich` and write the result back yourself.

        Enrichment actions — at least one is required, and each spends lemlist
        credits: `find_email` finds a verified email, `verify_email` verifies the
        email you passed (debounce), `linkedin_enrichment` runs the LinkedIn
        enrichment, `find_phone` finds a phone number. Ask only for what you need.

        Args:
            lead_id: the lead's `_id`, as returned by `lemlist_create_lead`.
        """
        _require_action(
            find_email=find_email, verify_email=verify_email,
            linkedin_enrichment=linkedin_enrichment, find_phone=find_phone,
        )
        client, is_platform = _client()
        result = client.enrich_lead(
            lead_id,
            find_email=find_email, verify_email=verify_email,
            linkedin_enrichment=linkedin_enrichment, find_phone=find_phone,
            webhook_url=webhook_url,
        )
        _record_if_platform(is_platform)
        return {
            "enrichment_id": result.get("id"),
            "next_step": ("Enrichment accepted. Call "
                          "lemlist_enrich_result(enrichment_id) in ~10-20s."),
        }

    @mcp.tool()
    def lemlist_enrich_result(enrichment_id: str | list[str]) -> dict:
        """Collect the result of an enrichment submitted with `lemlist_enrich`,
        `lemlist_enrich_lead` or `lemlist_enrich_bulk`.

        Single status check per id, returns immediately — no waiting. If a
        result is not `done`, wait ~15-30s and call again.

        Returns `results`: one entry per id, `{enrichment_id, status, done,
        input, data, found}`. `status` is `done`, `in-progress` or `not-found`.
        `data` is lemlist's raw payload; `found` is the digest to read — only
        the axes that actually carry a value (`email`, `email_status`
        `deliverable`/`undeliverable`, `phone`, `linkedin`). An axis key is
        present in `data` even when empty, so presence alone means nothing, and
        `notFound: false` has been seen on a payload with no number.

        A result can come back `done` with nothing in it: lemlist sometimes
        flips the status before the payload lands. Such an entry carries a
        `warning` and is NOT counted in `all_done` — poll it once more before
        concluding nothing was found (a poll costs no credits).

        Args:
            enrichment_id: one id, or a list of ids (a bulk submit yields one id
                per person, so pass them all here in one go).
        """
        ids = [enrichment_id] if isinstance(enrichment_id, str) else list(enrichment_id)
        client, _ = _client()
        results = []
        for eid in ids:
            res = client.get_enrichment(eid)
            status = res.get("enrichmentStatus", "unknown")
            data = res.get("data") or {}
            row = {
                "enrichment_id": res.get("enrichmentId", eid),
                "status": status,
                # `not-found` est terminal aussi : re-poller ne le fera pas
                # apparaître, c'est un id inconnu de lemlist.
                "done": status in ("done", "not-found"),
                "input": res.get("input", {}),
                "data": data,
                "found": _found_digest(data),
            }
            if status == "done" and not row["found"]:
                # Observé en live : lemlist bascule parfois sur `done` AVANT que
                # la charge utile soit posée (un `data` vide, puis peuplé au
                # relevé suivant). Sans ce garde-fou, un agent lit « done + rien »
                # et conclut « pas trouvé » sur une donnée qui arrive juste après.
                # Un relevé ne coûte pas de crédit : autant le refaire une fois.
                row["warning"] = (
                    "done but empty — lemlist sometimes flips to done before the "
                    "payload lands. Poll once more (~15s) before concluding "
                    "nothing was found."
                )
            results.append(row)
        pending = [r["enrichment_id"] for r in results if not r["done"]]
        settling = [r["enrichment_id"] for r in results if r.get("warning")]
        # `all_done` ne parle QUE de ce qui tourne encore : un résultat
        # légitimement vide (personne introuvable) le resterait à jamais, et un
        # agent qui boucle sur `all_done` ne s'arrêterait plus. Le re-relevé
        # d'un `done` vide est une SUGGESTION, à faire une fois — pas une
        # condition de sortie.
        out = {"results": results, "all_done": not pending}
        if settling:
            out["recheck_suggested"] = settling
        if pending or settling:
            bits = []
            if pending:
                bits.append(f"{len(pending)} still running")
            if settling:
                bits.append(f"{len(settling)} done-but-empty (re-poll ONCE, "
                            "then treat as not found)")
            out["next_step"] = (
                ", ".join(bits) + " — call lemlist_enrich_result again in ~15-30s."
            )
        return out

    @mcp.tool()
    def lemlist_enrich_bulk(
        people: list[dict], webhook_url: Optional[str] = None,
    ) -> dict:
        """Submit several enrichments in one call.

        Returns `submitted`: one entry per person, in order, each carrying
        either `enrichment_id` or `error` (e.g. `MISSING_INPUTS`). Unlike a
        FullEnrich job, a bulk submit yields one id PER PERSON — pass the whole
        list of ids to `lemlist_enrich_result`.

        Args:
            people: one entry per person. Identity keys (all optional, same
                matching rules as `lemlist_enrich`): `email`, `linkedin_url`,
                `first_name`, `last_name`, `company_name`, `company_domain`.
                Plus `actions`: a list among `find_email`, `verify_email`,
                `linkedin_enrichment`, `find_phone` — required, and each action
                spends lemlist credits per person.
        """
        if not people:
            raise McpError(ErrorData(
                code=INVALID_PARAMS, message="`people` is empty — nothing to enrich.",
            ))
        client, is_platform = _client()

        items = []
        for i, person in enumerate(people):
            actions = person.get("actions") or []
            if isinstance(actions, str):
                actions = [actions]
            unknown = [a for a in actions if a not in BULK_ACTIONS]
            if unknown:
                raise McpError(ErrorData(
                    code=INVALID_PARAMS,
                    message=(f"people[{i}]: unknown action(s) {unknown} — allowed: "
                             f"{sorted(BULK_ACTIONS)}."),
                ))
            if not actions:
                raise McpError(ErrorData(
                    code=INVALID_PARAMS,
                    message=(f"people[{i}]: no `actions` — set at least one of "
                             f"{sorted(BULK_ACTIONS)}."),
                ))
            item = {
                "input": {
                    k: v for k, v in {
                        "email": person.get("email"),
                        "linkedinUrl": person.get("linkedin_url"),
                        "firstName": person.get("first_name"),
                        "lastName": person.get("last_name"),
                        "companyName": person.get("company_name"),
                        "companyDomain": person.get("company_domain"),
                    }.items() if v is not None
                },
                # Vocabulaire v2 : `verify`, pas `verify_email` — la table de
                # correspondance vit dans le client, ce n'est pas un snake_case
                # mécanique des flags v1.
                "enrichmentRequests": [BULK_ACTIONS[a] for a in actions],
                "metadata": {"index": str(i)},
            }
            items.append(item)

        raw = client.bulk_enrich(items, webhook_url=webhook_url)
        if is_platform:
            # Un bulk est facturé À LA PERSONNE : la consommation est le nombre
            # d'entrées soumises, pas 1 pour l'appel (même règle que FullEnrich).
            access.record_platform_usage("lemlist", len(items))
        submitted = []
        for i, entry in enumerate(raw if isinstance(raw, list) else []):
            # `metadata` est renvoyé tel quel par lemlist, mais sa forme n'est
            # pas stable (leur propre exemple montre `{"id": ...}` ET une
            # chaîne nue) : on s'en sert quand il porte bien l'index qu'on a
            # posé, sinon on retombe sur la position — les entrées reviennent
            # dans l'ordre soumis.
            meta = entry.get("metadata")
            index = i
            if isinstance(meta, dict) and str(meta.get("index", "")).isdigit():
                index = int(meta["index"])
            row = {"index": index}
            if entry.get("id"):
                row["enrichment_id"] = entry["id"]
            if entry.get("error"):
                row["error"] = entry["error"]
            submitted.append(row)
        ids = [r["enrichment_id"] for r in submitted if r.get("enrichment_id")]
        return {
            "submitted": submitted,
            "enrichment_ids": ids,
            "next_step": ("Call lemlist_enrich_result(enrichment_ids) in ~10-20s."
                          if ids else "Nothing accepted — check the per-entry errors."),
        }

    @mcp.tool()
    def lemlist_launch_lead(lead_id: str) -> dict:
        """Launch a lead that's paused for manual review.

        Only relevant for a campaign with review-before-send enabled — such a
        campaign leaves a newly created lead paused until launched. Returns
        `{"ok": true}` on success; raises with a lemlist error code if it can't
        launch (already launched, paused, no sender available, invalid AI
        variable, campaign step errors…).
        """
        client, is_platform = _client()
        result = client.launch_lead(lead_id)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    def lemlist_add_lead_variables(lead_id: str, variables: dict) -> dict:
        """Set custom variables on a lead — merged into its personalization
        data, e.g. for `{{variableName}}` placeholders in campaign templates."""
        client, is_platform = _client()
        result = client.add_lead_variables(lead_id, variables)
        _record_if_platform(is_platform)
        return result

    # --- Gestion de campagne ---------------------------------------------------
    #
    # Trois tools à `op`, un tool nu. Le découpage n'est pas cosmétique : le
    # masquage par défaut (`DEFAULT_HIDDEN_TOOLS`) a le grain du TOOL, pas de
    # l'op. `start` — le seul geste ici qui mette des messages sur le fil — vit
    # donc à part, masqué ; le reste (créer, régler, dupliquer, mettre en pause,
    # valider) n'envoie rien et tient dans un tool visible par famille.

    @mcp.tool()
    def lemlist_campaign(
        op: Literal["create", "update", "pause", "duplicate", "statutes",
                    "reports", "batch_stats", "export_start", "export_status",
                    "export_email", "export_leads"],
        campaign_id: Optional[str] = None,
        campaign_ids: Optional[list[str]] = None,
        name: Optional[str] = None,
        timezone: Optional[str] = None,
        settings: Optional[dict] = None,
        sender_user_ids: Optional[list[str]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        channels: Optional[list[str]] = None,
        send_user: Optional[str] = None,
        ab_selected: Optional[str] = None,
        export_id: Optional[str] = None,
        email: Optional[str] = None,
        state: Optional[str] = None,
        format: Optional[str] = None,
        full: bool = False,
    ) -> dict:
        """Manage campaigns: create, configure, pause, duplicate, validate,
        report, export. `oto_guide op=read slug="lemlist-playbook"` : ordre de construction, d'où vient chaque id, et les écarts doc↔API.

        Nothing here sends: a created or duplicated campaign lands in DRAFT.
        Putting messages on the wire is `lemlist_campaign_start` (hidden by
        default — enable it with `oto_enable_tool lemlist_campaign_start`).

        Args by op:
        - `create`: `name` (required), optional `timezone` (IANA, drives the
          auto-created schedule; server default `Europe/Paris`). Returns the
          campaign with `sequenceId` and `scheduleIds` — the two ids
          `lemlist_sequence` and `lemlist_schedule` need. `settings` is REFUSED
          here (the endpoint takes name + timezone only) — chain `update` on
          the returned id rather than believe a setting landed.
          ⚠️ The campaign is created in state RUNNING, not draft (its `status`
          reads "draft" only because it has no step and no lead yet). It sends
          nothing while the review gate holds, but chain `op="pause"` if you
          want to build it with the switch off.
        - `update`: `campaign_id` + `name`, `sender_user_ids` (`usr_…`, the
          senders) and/or `settings` (raw PATCH body: `stopOnEmailReplied`,
          `stopOnMeetingBooked`, `stopOnLinkClicked`, `disableTrackOpen`,
          `disableTrackClick`, `disableTrackReply`, `tracking`, `onReplied`,
          `aiFeatures`…). Only the keys sent change.
        - `pause`: `campaign_id`. THE off switch — a campaign created here is
          already running. Stops it advancing; already-scheduled leads are NOT
          recalled. Errors if the campaign is not running.
        - `duplicate`: `campaign_id` + optional `name`. Copies sequence, steps,
          schedules and AI templates into a fresh DRAFT (CRM settings excluded).
        - `statutes`: `campaign_id`. The validation the lemlist UI runs — read it
          BEFORE starting: `level` 3 blocks the launch (no sender, broken DNS),
          2 warns (daily limit, missing schedule), 1 informs.
        - `reports`: `campaign_ids`. One row per campaign in operator vocabulary
          (`emailsSent`, `emailsOpened`, `emailsReplied`, `senderNames`, `state`)
          — the shape for comparing campaigns.
        - `batch_stats`: `campaign_ids` (≤ 100) + optional `start_date`/`end_date`
          (defaults to the whole life), `channels` (`email`/`linkedin`/`others`),
          `send_user` (`usr_…|sender@email`), `ab_selected` (`A`/`B`).
          Same counters as `lemlist_get_campaign_stats`, in one call.

        - `export_start`: `campaign_id`. Opens an ASYNCHRONOUS stats export and
          returns its id; poll `export_status` (`campaign_id` + `export_id`),
          or ask to be notified with `export_email` (+ `email`).
        - `export_leads`: `campaign_id` + optional `state` (defaults to `all`;
          lemlist's own default filters EVERYTHING out and returns an empty
          list that reads as "no leads"), `format` (`csv`, the API default, or
          `json`). Returns the leads directly — the synchronous cousin of the
          export above.

        `autoReview`/`autoReviewConditions` are not settable here: they make
        every added lead send immediately, which would turn `lemlist_create_lead`
        into a send path. They live in `lemlist_campaign_auto_review`, hidden by
        default — a switch that arms sending should take a deliberate gesture,
        not ride along in a settings dict.
        """
        client, is_platform = _client()
        settings = dict(settings or {})

        if op == "create":
            if not name:
                raise _bad("`name` requis pour créer une campagne")
            _refuse_auto_review(settings)
            if settings:
                # `POST /campaigns` ne prend que name + timezone : accepter un
                # `settings` ici rendrait une campagne d'apparence réglée dont
                # aucun réglage n'aurait pris. Refuser plutôt que jeter en
                # silence — et plutôt que create-puis-update, qui laisse une
                # campagne à moitié réglée quand le second appel échoue.
                raise _bad(
                    "`settings` ne s'applique pas à la création — la campagne "
                    "naît avec `name` (+ `timezone`). Enchaîne "
                    'op="update" sur l\'id rendu.')
            result = client.create_campaign(name, timezone=timezone)
            # Le retour de lemlist porte `state: running` et un `status` qui dit
            # « draft » : l'agent qui lit le second croit la campagne à l'arrêt.
            # On le dit plutôt que de le laisser déduire.
            if isinstance(result, dict):
                result = {**result, "warning": (
                    "Campagne créée en state=running (son `status` affiche "
                    "\"draft\" tant qu'elle n'a ni étape ni lead). Rien ne part "
                    "tant qu'un lead n'est pas lancé, mais appelle "
                    "op=\"pause\" si tu veux la construire interrupteur coupé.")}

        elif op == "update":
            if not campaign_id:
                raise _bad("`campaign_id` requis")
            _refuse_auto_review(settings)
            if name is not None:
                settings["name"] = name
            if sender_user_ids is not None:
                settings["sendUserIds"] = sender_user_ids
            if not settings:
                raise _bad(
                    "rien à mettre à jour — passe `name`, `sender_user_ids` "
                    "et/ou `settings`")
            result = client.update_campaign(campaign_id, settings)

        elif op == "pause":
            if not campaign_id:
                raise _bad("`campaign_id` requis")
            result = client.pause_campaign(campaign_id)

        elif op == "duplicate":
            if not campaign_id:
                raise _bad("`campaign_id` requis")
            result = client.duplicate_campaign(campaign_id, name=name)

        elif op == "statutes":
            if not campaign_id:
                raise _bad("`campaign_id` requis")
            result = client.get_campaign_statutes(campaign_id)

        elif op == "reports":
            if not campaign_ids:
                raise _bad("`campaign_ids` requis (liste d'ids de campagne)")
            result = {"reports": client.get_campaign_reports(campaign_ids)}

        elif op == "batch_stats":
            if not campaign_ids:
                raise _bad("`campaign_ids` requis (liste d'ids de campagne)")
            start, end = _default_window(start_date, end_date)
            result = client.get_batch_campaign_stats(
                campaign_ids, start_date=start, end_date=end, channels=channels,
                send_user=send_user, ab_selected=ab_selected)
            if not full:
                result = {**result, "results": [
                    _project_stats(r, full=False) for r in result.get("results", [])
                ]}

        elif op == "export_start":
            if not campaign_id:
                raise _bad("`campaign_id` requis")
            result = client.start_campaign_export(campaign_id)

        elif op == "export_status":
            if not (campaign_id and export_id):
                raise _bad("`campaign_id` ET `export_id` requis")
            result = client.get_campaign_export_status(campaign_id, export_id)

        elif op == "export_email":
            if not (campaign_id and export_id and email):
                raise _bad("`campaign_id`, `export_id` ET `email` requis")
            result = client.set_campaign_export_email(campaign_id, export_id, email)

        elif op == "export_leads":
            if not campaign_id:
                raise _bad("`campaign_id` requis")
            # `state`/`format` OMIS quand ils ne sont pas demandés : le client
            # a des défauts qui comptent (`state="all"`, sans quoi lemlist rend
            # une liste vide qui se lit « pas de leads »), et passer None les
            # écraserait. Un défaut client ne survit pas à un None explicite.
            exported = client.export_campaign_leads(campaign_id, **{
                k: v for k, v in (("state", state), ("format", format))
                if v is not None})
            result = exported if isinstance(exported, dict) else {"leads": exported}

        else:
            raise _bad(
                f'op inconnu "{op}" — attendu: create, update, pause, duplicate, '
                "statutes, reports, batch_stats, export_start, export_status, "
                "export_email, export_leads")

        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    def lemlist_campaign_start(campaign_id: str) -> dict:
        """Start (or resume) a campaign — lemlist begins sending.

        THE send gesture at campaign level: from here lemlist walks the sequence
        for every launched lead, to real people. A no-op if already running.

        Read `lemlist_campaign(op="statutes", …)` first — it names what would
        block or degrade the launch (missing sender, broken DNS, daily limit)
        with the same validation the UI runs.

        ⚠️ In practice this RESUMES a paused campaign: one created through
        `lemlist_campaign(op="create")` is already running, and lemlist answers
        `400 "You can't start campaigns that are already running"`.
        """
        client, is_platform = _client()
        result = client.start_campaign(campaign_id)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    def lemlist_sequence(
        op: Literal["get", "add_step", "update_step", "delete_step",
                    "ab_create", "ab_get", "ab_update", "ab_delete", "ab_winner"],
        campaign_id: Optional[str] = None,
        sequence_id: Optional[str] = None,
        step_id: Optional[str] = None,
        step: Optional[dict] = None,
        variant: Optional[str] = None,
    ) -> dict:
        """Read and edit the steps of a campaign sequence, and its A/B tests.

        A campaign owns a sequence (`seq_…`, returned by `create`) whose steps
        (`stp_…`) are the emails, LinkedIn actions and conditions it runs.

        Args by op:
        - `get`: `campaign_id`. Every sequence of the campaign with its steps —
          conditional steps branch into further sequences, so a campaign can
          hold several.
        - `add_step`: `sequence_id` + `step`. `step.type` is required, one of
          email, manual, phone, api, linkedinVisit, linkedinInvite,
          linkedinSend, linkedinVoiceNote, linkedinFollow, linkedinLikeLastPost,
          linkedinCommentLastPost, linkedinEndorse, linkedinWithdrawInvitation,
          sendToAnotherCampaign, conditional, whatsappMessage, sms. Common
          fields: `index` (insert position, appended when omitted), `delay`
          (days), `subject` + `message` (email), `title` (manual),
          `method` + `url` (api), `conditionKey` + `delayType` (conditional),
          `campaignId` (sendToAnotherCampaign).
        - `update_step`: `sequence_id` + `step_id` + `step`. `step.type` is
          required and must MATCH the existing step — it identifies the shape,
          it does not convert it. `images`/`videos` REPLACE what is there.
        - `delete_step`: `sequence_id` + `step_id`. Refused by lemlist while the
          campaign is running — pause it first.
        - `ab_create`: `sequence_id` + `step_id` (an EMAIL step). Creates
          variant B prefilled from A and STARTS the split. Email Pro plan.
        - `ab_get` / `ab_update` (`step` = the B fields: `subject`, `message`,
          `altMessage`, `cc`, `plainText`).
        - `ab_delete`: optional `variant` (default `B`). `A` promotes B to A.
        - `ab_winner`: `variant` (`A` or `B`) — the winner's template is then
          sent to every remaining lead.
        """
        client, is_platform = _client()

        if op == "get":
            if not campaign_id:
                raise _bad("`campaign_id` requis")
            result = {"sequences": client.get_sequences(campaign_id)}
        else:
            if not sequence_id:
                raise _bad("`sequence_id` requis (il vient de `lemlist_campaign` "
                           "op=create, ou de op=\"get\" ici)")
            if op == "add_step":
                if not step:
                    raise _bad("`step` requis (au minimum `{\"type\": …}`)")
                result = client.add_step(sequence_id, step)
            elif op in ("update_step", "delete_step", "ab_create", "ab_get",
                        "ab_update", "ab_delete", "ab_winner"):
                if not step_id:
                    raise _bad("`step_id` requis")
                if op == "update_step":
                    if not step:
                        raise _bad("`step` requis (et `step.type` doit correspondre "
                                   "au type existant)")
                    result = client.update_step(sequence_id, step_id, step)
                elif op == "delete_step":
                    result = client.delete_step(sequence_id, step_id)
                elif op == "ab_create":
                    result = client.create_ab_variant(sequence_id, step_id)
                elif op == "ab_get":
                    result = client.get_ab_variant(sequence_id, step_id)
                elif op == "ab_update":
                    if not step:
                        raise _bad("`step` requis (les champs de la variante B)")
                    result = client.update_ab_variant(sequence_id, step_id, step)
                elif op == "ab_delete":
                    result = client.delete_ab_variant(
                        sequence_id, step_id, variant=variant or "B")
                else:  # ab_winner
                    if not variant:
                        raise _bad("`variant` requis — 'A' ou 'B'")
                    result = client.select_ab_winner(sequence_id, step_id, variant)
            else:
                raise _bad(
                    f'op inconnu "{op}" — attendu: get, add_step, update_step, '
                    "delete_step, ab_create, ab_get, ab_update, ab_delete, ab_winner")

        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    def lemlist_schedule(
        op: Literal["list", "get", "create", "update", "delete",
                    "for_campaign", "associate"],
        schedule_id: Optional[str] = None,
        campaign_id: Optional[str] = None,
        name: Optional[str] = None,
        timezone: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        weekdays: Optional[list[int]] = None,
        seconds_to_wait: Optional[int] = None,
        public: Optional[bool] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        page: Optional[int] = None,
        newest_first: bool = False,
    ) -> dict:
        """Manage sending windows (schedules) — days, hours, timezone, pacing.

        A schedule belongs to the TEAM, not to a campaign: several campaigns can
        share one, and a campaign can carry several. Creating a campaign
        auto-creates one and returns its id in `scheduleIds`.

        Args by op:
        - `list`: optional `limit`, `offset`/`page`, `newest_first` — the
          route is paginated, so a team with many windows needs them.
        - `get` / `delete`: `schedule_id`.
        - `create`: `name` (required) + `timezone` (IANA, default
          `Europe/Paris`), `start`/`end` (`HH:mm`, default 09:00-18:00),
          `weekdays` (1 = Monday … 7 = Sunday, default Mon-Fri),
          `seconds_to_wait` (pacing between two sends), `public` (offer it as a
          team template).
        - `update`: `schedule_id` + any of the same fields; only what is sent
          changes.
        - `for_campaign`: `campaign_id`. The schedules attached to a campaign.
        - `associate`: `campaign_id` + `schedule_id`. Attaches an existing
          window to a campaign.
        """
        client, is_platform = _client()

        if op == "list":
            result = client.list_schedules(
                limit=limit, offset=offset, page=page,
                sort_order="desc" if newest_first else None)
        elif op == "get":
            if not schedule_id:
                raise _bad("`schedule_id` requis")
            result = client.get_schedule(schedule_id)
        elif op == "create":
            if not name:
                raise _bad("`name` requis pour créer un planning")
            kwargs = {k: v for k, v in {
                "timezone": timezone, "start": start, "end": end,
                "weekdays": weekdays, "seconds_to_wait": seconds_to_wait,
                "public": public,
            }.items() if v is not None}
            result = client.create_schedule(name, **kwargs)
        elif op == "update":
            if not schedule_id:
                raise _bad("`schedule_id` requis")
            data = {k: v for k, v in {
                "name": name, "timezone": timezone, "start": start, "end": end,
                "weekdays": weekdays, "secondsToWait": seconds_to_wait,
                "public": public,
            }.items() if v is not None}
            if not data:
                raise _bad("rien à mettre à jour — passe au moins un champ")
            result = client.update_schedule(schedule_id, data)
        elif op == "delete":
            if not schedule_id:
                raise _bad("`schedule_id` requis")
            result = client.delete_schedule(schedule_id)
        elif op == "for_campaign":
            if not campaign_id:
                raise _bad("`campaign_id` requis")
            result = {"schedules": client.get_campaign_schedules(campaign_id)}
        elif op == "associate":
            if not (campaign_id and schedule_id):
                raise _bad("`campaign_id` ET `schedule_id` requis")
            result = client.associate_schedule(campaign_id, schedule_id)
        else:
            raise _bad(
                f'op inconnu "{op}" — attendu: list, get, create, update, delete, '
                "for_campaign, associate")

        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    def lemlist_campaign_auto_review(
        campaign_id: str,
        enabled: bool,
        conditions: Optional[list[str]] = None,
    ) -> dict:
        """Arm (or disarm) auto-review on a campaign — leads then send on ADD.

        With auto-review on, a lead added to this campaign is launched
        immediately instead of waiting for manual review: `lemlist_create_lead`
        stops being a staging gesture and becomes a send. That is the whole
        reason this is its own tool, hidden by default, rather than a field in
        `lemlist_campaign(op="update")` — arming sending should be a deliberate
        act, not a key that rides along in a settings dict.

        Args:
            campaign_id: Campaign to arm or disarm.
            enabled: True arms it, False takes it back off.
            conditions: Restrict auto-launch to leads whose email verification
                is `deliverable`, `risky`, `undeliverable` or `unverified`.
                Narrowing to `["deliverable"]` is the cautious setting.
        """
        settings: dict = {"autoReview": enabled}
        if conditions is not None:
            settings["autoReviewConditions"] = conditions
        client, is_platform = _client()
        result = client.update_campaign(campaign_id, settings)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    def lemlist_lead(
        op: Literal["get", "list", "update", "delete", "unsubscribe",
                    "pause", "resume", "interested", "not_interested",
                    "vars_update", "vars_delete", "import_crm", "upload_audio"],
        campaign_id: Optional[str] = None,
        lead_id: Optional[str] = None,
        email: Optional[str] = None,
        fields: Optional[dict] = None,
        variables: Optional[dict] = None,
        variable_names: Optional[list[str]] = None,
        state: Optional[str] = None,
        limit: Optional[int] = None,
        crm: Optional[str] = None,
        user_id: Optional[str] = None,
        filter_id: Optional[str] = None,
        filter_type: Optional[str] = None,
        deduplicate: Optional[bool] = None,
        step_id: Optional[str] = None,
        audio: Optional[dict] = None,
    ) -> dict:
        """Lead lifecycle inside a campaign — read, edit, pause, qualify, remove.
        `oto_guide op=read slug="lemlist-playbook"` : ordre de construction, d'où vient chaque id, et les écarts doc↔API.

        A LEAD is a person's copy inside ONE campaign (its sending state, its
        variables); the person themself is a contact (`lemlist_contact`).
        Creating a lead is `lemlist_create_lead`; releasing one held for review
        is `lemlist_launch_lead`.

        Args by op:
        - `get`: `lead_id` or `email`. `list`: `campaign_id` + optional `state`
          (`sent`, `replied`, `paused`…) and `limit`. ⚠️ `state` defaults to
          `"all"` HERE, which is NOT lemlist's own default: without it the API
          filters everything out and returns an empty list that reads as "no
          leads on this campaign". Pass a state only to narrow deliberately.
        - `update`: `campaign_id` + `lead_id` + `fields` (`firstName`,
          `lastName`, `companyName`, `jobTitle`, `preferredContactMethod`).
        - `delete`: `campaign_id` + `lead_id`/`email` — really removes it.
          `unsubscribe`: same arguments, but the lead STAYS on the campaign,
          marked unsubscribed. One lemlist route serves both, and its default is
          the soft one; here the two are named apart so neither is a surprise.
        - `pause`: `lead_id`; WITHOUT `campaign_id` it pauses the lead in EVERY
          campaign, not one. `resume`: `lead_id` — undoes a pause, so lemlist
          starts sending to that lead again (it does not skip a review; that is
          `lemlist_launch_lead`).
        - `interested` / `not_interested`: `lead_id` or `email`; with
          `campaign_id` it applies to that campaign, without it to all.
        - `vars_update`: `lead_id` + `variables`. `vars_delete`: `lead_id` +
          `variable_names` (the values are erased).
        - `import_crm`: `campaign_id` + `crm` + `user_id` + `filter_id`
          (from `lemlist_team(op="crm_filters")`), optional `filter_type`,
          `deduplicate`.
        - `upload_audio`: `lead_id` + `step_id` + `audio` — the audio of a
          `linkedinVoiceNote` step. `audio` is a source dict:
          `{"kind": "url", "url": …}`, `{"kind": "drive", "file_id": …}` or
          `{"kind": "gmail", "message_id": …, "filename": …}`. The server
          fetches it (≤ 20 MB) and forwards the bytes.
        """
        client, is_platform = _client()
        target = lead_id or email

        if op == "get":
            if not target:
                raise _bad("`lead_id` ou `email` requis")
            result = (client.get_lead(lead_id=lead_id) if lead_id
                      else client.get_lead_by_email(email))

        elif op == "list":
            if not campaign_id:
                raise _bad("`campaign_id` requis")
            # ⚠️ `state="all"` par DÉFAUT — le défaut de lemlist filtre TOUT et rend
            # une liste vide qui se lit « pas de leads » (vérifié en live le
            # 2026-08-31, cf. `export_campaign_leads` côté client). Le guide
            # `lemlist-playbook` annonçait ce forçage comme acquis pour le connecteur
            # entier ; il n'existait que sur la route d'export, et cette route-ci
            # rendait donc `[]` sur une campagne qui contient bien des leads
            # (signal 719). Un `state` explicite reste maître ; il n'y a PAS
            # d'échappatoire vers le brut — une valeur magique non documentée serait
            # le défaut d'à côté, et le brut n'est utile à personne : il filtre tout.
            result = {"leads": client.get_campaign_leads(
                campaign_id, state=(state or "all"), limit=limit)}

        elif op == "update":
            if not (campaign_id and lead_id and fields):
                raise _bad("`campaign_id`, `lead_id` ET `fields` requis")
            result = client.update_lead(campaign_id, lead_id, fields)

        elif op in ("delete", "unsubscribe"):
            if not (campaign_id and target):
                raise _bad("`campaign_id` ET `lead_id`/`email` requis")
            result = client.delete_lead(
                campaign_id, target, action="remove" if op == "delete" else None)

        elif op == "pause":
            if not lead_id:
                raise _bad("`lead_id` requis")
            result = client.pause_lead(lead_id, campaign_id=campaign_id)

        elif op == "resume":
            if not lead_id:
                raise _bad("`lead_id` requis")
            result = client.resume_lead(lead_id)

        elif op in ("interested", "not_interested"):
            if not target:
                raise _bad("`lead_id` ou `email` requis")
            mark = (client.mark_lead_interested if op == "interested"
                    else client.mark_lead_not_interested)
            result = mark(target, campaign_id=campaign_id)

        elif op == "vars_update":
            if not (lead_id and variables):
                raise _bad("`lead_id` ET `variables` requis")
            result = client.update_lead_variables(lead_id, variables)

        elif op == "vars_delete":
            if not (lead_id and variable_names):
                raise _bad("`lead_id` ET `variable_names` requis")
            result = client.delete_lead_variables(lead_id, variable_names)

        elif op == "import_crm":
            if not (campaign_id and crm and user_id and filter_id):
                raise _bad(
                    "`campaign_id`, `crm`, `user_id` ET `filter_id` requis — "
                    'le filtre vient de `lemlist_team(op="crm_filters")`')
            result = client.import_leads_from_crm(
                campaign_id, crm=crm, user_id=user_id, filter_id=filter_id,
                filter_type=filter_type, deduplicate=deduplicate)

        elif op == "upload_audio":
            if not (lead_id and step_id and audio):
                raise _bad(
                    "`lead_id`, `step_id` ET `audio` requis — `audio` est une "
                    'source : {"kind": "url"|"drive"|"gmail", …}')
            data, filename = _fetch_audio(audio)
            result = client.upload_lead_audio(
                lead_id, step_id, data, filename=filename)

        else:
            raise _bad(
                f'op inconnu "{op}" — attendu: get, list, update, delete, '
                "unsubscribe, pause, resume, interested, not_interested, "
                "vars_update, vars_delete, import_crm, upload_audio")

        _record_if_platform(is_platform)
        return result
