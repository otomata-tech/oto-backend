"""Lemlist — push a table's rows as leads, BY REFERENCE.

Third module of the connector (`lemlist` holds the campaign, `lemlist_crm` the rest):
this one carries a single action, `lemlist_push_rows`. `lemlist_create_lead` took the
person as ARGUMENTS — name, email, phone went through the tool call, once per
lead. Here the agent designates rows; the server reads them, creates the leads, writes
back the lemlist id and the state on each row, and only returns counts. The shared
mechanics (batch, lease, write-back, receipt) live in `datastore/par_reference.py`.

⚠️ Like `lemlist_create_lead`, this action SENDS nothing: a created lead waits for the
campaign's review. What puts a message on the wire remains `lemlist_launch_lead` and
`lemlist_campaign_start`, hidden by default (see `tools/lemlist.py`).
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access, session_org
from ..datastore import par_reference as pr
from ..datastore.identite import AdresseJson as Adresse
from .lemlist import _campagne_introuvable, _lead_deja_pris, _texte

#: The names of `lemlist_create_lead` → the lemlist field. A `field_mapping`
#: key outside this table goes through as is: lemlist files any unknown key
#: as a custom variable (`{{nom}}` in a template).
CHAMPS = {
    "email": "email",
    "first_name": "firstName",
    "last_name": "lastName",
    "company_name": "companyName",
    "job_title": "jobTitle",
    "linkedin_url": "linkedinUrl",
    "phone": "phone",
    "company_domain": "companyDomain",
    "icebreaker": "icebreaker",
    "timezone": "timezone",
    "contact_owner": "contactOwner",
}
#: A lead with none of these fields is reachable by no step of a campaign.
IDENTITE = ("email", "linkedinUrl", "phone")

POUSSE, DOUBLON, ECHEC = "pushed", "duplicate", "failed"


def construire_lead(ligne: dict, mapping: dict[str, str]) -> tuple[dict, Optional[str]]:
    """`(lead, code)` — the lead to send, or the code that rules it out."""
    lead: dict = {}
    for champ, colonne in mapping.items():
        v = pr.valeur(ligne, colonne)
        if v is None:
            continue
        texte = _texte(v)
        if texte is None:
            return {}, "unsupported_value"
        lead[CHAMPS.get(champ, champ)] = texte
    if not any(lead.get(c) for c in IDENTITE):
        return {}, "missing_identity"
    return lead, None


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.lemlist import LemlistClient

    def _client(units: int = 1) -> tuple[LemlistClient, bool]:
        key, is_platform = access.resolve_api_key("lemlist", units=units)
        return LemlistClient(api_key=key), is_platform

    @mcp.tool()
    def lemlist_push_rows(
        datastore: Adresse,
        campaign_id: str,
        field_mapping: dict[str, str],
        row_ids: Optional[list[str]] = None,
        filter: Optional[dict] = None,
        deduplicate: bool = False,
        id_column: str = "lemlist_lead_id",
        status_column: str = "lemlist_status",
        batch_size: int = 25,
        dry_run: bool = False,
    ) -> dict:
        """Create lemlist leads FROM DATASTORE ROWS — the bulk way; nothing personal
        goes through this call.

        Name the rows (`row_ids`, or a `filter`); oto reads them, creates one lead per
        row in the campaign, and writes back `id_column` (the lead id) and
        `status_column` (pushed | duplicate | failed) on each row. The answer is
        counts plus `errors: [{row_id, code}]` — never a value read from a row.

        By `filter`, only rows whose `status_column` is empty are taken: call again
        with the same filter until `remaining` is 0. A row already treated is not
        picked again — name it in `row_ids` to retry it. A row with `id_column`
        already set is skipped (`already_pushed`); one reserved by another run is
        skipped (`row_locked`) before anything is sent. `dry_run` reads and checks
        the mapping, sends nothing. Like lemlist_create_lead, this sends no email: a
        lead waits for review until lemlist_launch_lead.

        Args:
            datastore: the table, by its NUMBER (`ns_id`) — a name is refused.
            campaign_id: lemlist campaign id WITH its `cam_` prefix.
            field_mapping: {lead field: column}. Lead fields are those of
                lemlist_create_lead (email, first_name, last_name, company_name,
                job_title, linkedin_url, phone, company_domain, icebreaker, timezone,
                contact_owner); any other key becomes a custom variable. A row needs
                an email, a linkedin_url or a phone.
            row_ids: the rows to push (at most 50). Exclusive with `filter`.
            filter: data_rows filter grammar; `{}` = every row not yet treated.
            deduplicate: skip a lead whose email is already in another campaign.
            id_column: where the lemlist lead id is written back.
            status_column: where pushed | duplicate | failed is written back; the
                code of a duplicate or a failure goes in its `comment` layer.
            batch_size: rows per call with `filter` (1-50).
            dry_run: read and check only — no lemlist call, nothing written.
        """
        if not campaign_id.startswith("cam_"):
            raise pr.refus("lemlist_campaign_id_format",
                           "`campaign_id` must carry its `cam_` prefix, as "
                           "lemlist_campaign returns it. Nothing was sent.")
        mapping = pr.valider_correspondance(field_mapping)
        lot = pr.ouvrir(datastore, row_ids=row_ids, filter=filter,
                        colonne_etat=status_column, limite=batch_size)
        inconnues = pr.colonnes_inconnues(lot, mapping.values())
        if inconnues:
            raise pr.refus("push_rows_unknown_columns",
                           f"columns missing from the table: {', '.join(inconnues)}. "
                           "Nothing was sent.", columns=inconnues)

        recu = pr.Recu()
        client = is_platform = None
        if not dry_run and lot.lignes:
            client, is_platform = _client(units=len(lot.lignes))
        traitees = 0
        for ligne in lot.lignes:
            if recu.budget_epuise():
                recu.arret = "time_budget"
                break
            traitees += 1
            rid = str(ligne.get("_id"))
            if pr.tenue_ailleurs(ligne):
                recu.ecarter(rid, "row_locked")
                continue
            if pr.valeur(ligne, id_column) is not None:
                recu.ecarter(rid, "already_pushed")
                continue
            lead, code = construire_lead(ligne, mapping)
            if code:
                recu.ecarter(rid, code)
                continue
            if dry_run:
                recu.compter("would_push")
                continue
            try:
                cree = client.create_lead(campaign_id, lead, deduplicate=deduplicate)
            except UpstreamHTTPError as e:
                if _campagne_introuvable(e):
                    traitees -= 1
                    recu.arret = "lemlist_campaign_not_found"
                    break
                raison = _lead_deja_pris(e)
                statut = getattr(e, "status_code", None)
                if raison is None and statut in (401, 403, 429):
                    # The key, the plan or the rate: the next row would fail the same way.
                    traitees -= 1
                    recu.arret = "rate_limited" if statut == 429 else f"lemlist_http_{statut}"
                    break
                if raison is not None:
                    recu.compter("duplicates")
                    ecrit = pr.ecrire(lot, rid, {status_column: DOUBLON,
                                                 f"{status_column}.comment": raison})
                else:
                    recu.echec(rid, f"lemlist_http_{statut}")
                    ecrit = pr.ecrire(lot, rid, {status_column: ECHEC,
                                                 f"{status_column}.comment":
                                                     f"lemlist_http_{statut}"})
                if ecrit:
                    recu.echec(rid, f"writeback_{ecrit}")
                continue
            lead_id = cree.get("_id") if isinstance(cree, dict) else None
            if not lead_id:
                recu.echec(rid, "lemlist_lead_not_created")
                pr.ecrire(lot, rid, {status_column: ECHEC,
                                     f"{status_column}.comment": "lemlist_lead_not_created"})
                continue
            recu.compter("pushed")
            ecrit = pr.ecrire(lot, rid, {
                id_column: lead_id, f"{id_column}.comment": f"lemlist {campaign_id}",
                status_column: POUSSE})
            if ecrit:
                # The lead EXISTS at lemlist: this code says the row does not know it.
                recu.echec(rid, f"writeback_{ecrit}")

        poussees = recu.comptes.get("pushed", 0)
        if not dry_run:
            # The BILLED line counts the leads created, as N calls to
            # lemlist_create_lead would have (`tool_calls.quantity`).
            session_org.note_call_trace(quantity=poussees)
        doublons = recu.comptes.get("duplicates", 0)
        avis = {"existing_left_untouched": (
            f"{doublons} lead(s) already taken were left untouched: lemlist refuses a "
            "lead already in this campaign (or, with `deduplicate`, in another one). "
            "Their rows got status `duplicate`, the reason in its comment.")} if doublons else {}
        return recu.rendre(lot, selectionnees=len(lot.lignes), dry_run=dry_run,
                           traitees=traitees, campaign_id=campaign_id,
                           written_back={"id_column": id_column,
                                         "status_column": status_column}, **avis)
