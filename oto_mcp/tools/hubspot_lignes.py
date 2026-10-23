"""HubSpot — push the rows of a table as contacts or companies, BY REFERENCE.

Second module of the connector (`hubspot` holds objects, lists and properties): it only
carries `hubspot_push_rows`. `hubspot_object op=create|update` took the record as
ARGUMENTS (`properties={"email": …, "firstname": …}`), one person per call. Here
the agent designates rows; the server reads them, creates or updates, associates, files into
a list, writes the HubSpot id and the state back on each row, and only returns
counts. The shared mechanics live in `datastore/par_reference.py`.

**Match without guessing.** Does a record already exist? We ask
HubSpot by the uniqueness property (`email` for a contact, `domain` for a
company), in ONE `IN` search call for the whole batch — never one call per row
(a private app's ceiling is 190 requests / 10 s). Two records for the
same value: the row fails (`hubspot_ambiguous_match`) rather than picking one.
A row that already carries its HubSpot id designates that record, without a search.

**An existing record is not touched by default** (`on_existing="skip"`): a customer CRM is not
rewritten with the values of a table without being asked (`update`), and the
answer says how many existing records were left intact and how to update them. Two
rows of the same batch with the same matching value designate ONE record: the
second finds the one the first just created.
"""
from __future__ import annotations

from typing import Literal, Optional, Union

from fastmcp import FastMCP

from .. import session_org
from ..datastore import par_reference as pr
from ..datastore.identite import AdresseJson as Adresse
from ..auth import hubspot as hubspot_auth
from .hubspot import _scope_refusal
from .hubspot_scopes import unknown_properties

#: The property that says "it's the same record", per object type.
CLE_PAR_DEFAUT = {"contacts": "email", "companies": "domain"}
CREE, MAJ, EXISTE, ECHEC = "created", "updated", "exists", "failed"
#: HubSpot caps a search page, and a list of `IN` values, at 100.
_PAGE = 100


def _cle(v) -> Optional[str]:
    """The matching value, compared case-insensitively (HubSpot stores emails and
    domains in lowercase)."""
    if v is None:
        return None
    s = str(v).strip().lower()
    return s or None


def _constantes(constants, mapping: dict[str, str]) -> dict:
    """The FIXED properties of a batch (`lifecyclestage`, an import batch…), written
    on every record. Not a column: a value known to the caller, which
    says nothing about a person. A property that is both fixed and read from a column is
    refused rather than silently arbitrated."""
    if constants is None:
        return {}
    if not isinstance(constants, dict):
        raise pr.refus("hubspot_constants_shape",
                       "`constants` is an object {property: value}. Nothing was sent.")
    doublons = sorted(k for k in constants if k in mapping)
    if doublons:
        raise pr.refus("hubspot_constant_mapped",
                       f"propert(y/ies) both fixed and read from a column: "
                       f"{', '.join(doublons)}. Nothing was sent.")
    out: dict = {}
    for k, v in constants.items():
        if not (isinstance(k, str) and k) or not isinstance(v, (str, int, float, bool)):
            raise pr.refus("hubspot_constants_shape",
                           "`constants`: non-empty names, scalar values. "
                           "Nothing was sent.")
        out[k] = ("true" if v else "false") if isinstance(v, bool) else v
    return out


def _proprietes(ligne: dict, mapping: dict[str, str]) -> tuple[dict, Optional[str]]:
    props: dict = {}
    for prop, colonne in mapping.items():
        v = pr.valeur(ligne, colonne)
        if v is None:
            continue
        if isinstance(v, bool):
            props[prop] = "true" if v else "false"
        elif isinstance(v, (str, int, float)):
            props[prop] = v
        elif isinstance(v, list) and all(isinstance(x, (str, int, float)) for x in v):
            # A HubSpot multi-select property is written `a;b;c`.
            props[prop] = ";".join(str(x) for x in v)
        else:
            return {}, "unsupported_value"
    return props, None


def _avis_existants(n: int, on_existing: str) -> dict:
    """What the answer says about existing records left intact: how many, and how to
    update them. Nothing when none were."""
    if not n:
        return {}
    return {"existing_left_untouched": (
        f"{n} record(s) already in HubSpot were left untouched (on_existing="
        f"'{on_existing}'): their rows got status `exists` and their HubSpot id. "
        "To overwrite them with the row values, call again on those rows with "
        "on_existing='update'.")}


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.hubspot.client import HubSpotClient

    def _client() -> HubSpotClient:
        key = hubspot_auth.bearer()
        return HubSpotClient(api_key=key)

    def _existants(c: HubSpotClient, object_type: str, prop: str,
                   valeurs: list[str]) -> dict[str, list[str]]:
        """{value: [ids]} for the values already present in HubSpot."""
        trouves: dict[str, list[str]] = {}
        for i in range(0, len(valeurs), _PAGE):
            tranche = valeurs[i:i + _PAGE]
            after = None
            while True:
                page = c.search_objects(
                    object_type, filters=[{"propertyName": prop, "operator": "IN",
                                           "values": tranche}],
                    properties=[prop], limit=_PAGE, after=after) or {}
                for rec in page.get("results") or []:
                    cle = _cle((rec.get("properties") or {}).get(prop))
                    if cle is not None:
                        trouves.setdefault(cle, []).append(str(rec.get("id")))
                after = ((page.get("paging") or {}).get("next") or {}).get("after")
                if not after:
                    break
        return trouves

    def _associer(c: HubSpotClient, object_type: str, object_id: str,
                  vers: str, vers_id: str) -> None:
        # HubSpot's DEFAULT association (API v4): no association type identifier
        # to guess, HubSpot picks the one for the object pair.
        c._request("PUT", f"/crm/v4/objects/{object_type}/{object_id}/associations/"
                          f"default/{vers}/{vers_id}")

    @mcp.tool()
    def hubspot_push_rows(
        datastore: Adresse,
        object_type: Literal["contacts", "companies"],
        field_mapping: dict[str, str],
        row_ids: Optional[list[str]] = None,
        filter: Optional[dict] = None,
        match_property: Optional[str] = None,
        associate_with: Optional[Literal["contacts", "companies", "deals"]] = None,
        associate_id_column: Optional[str] = None,
        list_id: Optional[str] = None,
        constants: Optional[dict[str, Union[str, int, float, bool]]] = None,
        on_existing: Literal["skip", "list_only", "update"] = "skip",
        id_column: str = "hubspot_id",
        status_column: str = "hubspot_status",
        batch_size: int = 25,
        dry_run: bool = False,
    ) -> dict:
        """Create or update HubSpot contacts/companies FROM DATASTORE ROWS — the bulk
        way; nothing personal goes through this call.

        Name the rows (`row_ids`, or a `filter`); oto reads them, finds existing
        records by `match_property` (one search for the whole batch), creates the
        missing ones, optionally associates each record and adds it to a list, and
        writes back `id_column` (the HubSpot id) and `status_column` (created |
        updated | exists | failed) on each row. The answer is counts plus
        `errors: [{row_id, code}]` — never a value read from a row.

        A record that ALREADY exists is left untouched by default: pass
        `on_existing="update"` to overwrite its properties with the row values.

        By `filter`, only rows whose `status_column` is empty are taken: call again
        with the same filter until `remaining` is 0; name a row in `row_ids` to retry
        it. A row that already carries its HubSpot id designates that record. Two
        HubSpot records for one match value fail the row (`hubspot_ambiguous_match`)
        rather than picking one. `dry_run` reads and checks, sends nothing.

        Args:
            datastore: the table, by name or number.
            object_type: contacts | companies.
            field_mapping: {HubSpot INTERNAL property name: column} — names from
                hubspot_property (`firstname`, not "First name").
            row_ids: the rows to push (at most 50). Exclusive with `filter`.
            filter: data_rows filter grammar; `{}` = every row not yet treated.
            match_property: the property that identifies a record (default `email`
                for contacts, `domain` for companies); it must be in `field_mapping`.
            associate_with: object type to associate each record with.
            associate_id_column: column holding the HubSpot id to associate with
                (with `associate_with`).
            list_id: a MANUAL or SNAPSHOT list to add every pushed record to.
            constants: {property: value} written on every record created or
                updated (e.g. `lifecyclestage`, a batch tag) — a fixed value, not a
                column.
            on_existing: what a record that ALREADY exists gets — skip (default:
                left untouched, not listed), list_only (left untouched, but added to
                `list_id`) or update (its properties overwritten with the row
                values). Left untouched, its row gets status `exists`.
            id_column: where the HubSpot id is written back.
            status_column: where created | updated | exists | failed is written
                back; the code of a failure goes in its `comment` layer.
            batch_size: rows per call with `filter` (1-50).
            dry_run: read and check only — no HubSpot write, nothing written back.
        """
        mapping = pr.valider_correspondance(field_mapping)
        fixes = _constantes(constants, mapping)
        prop_cle = match_property or CLE_PAR_DEFAUT[object_type]
        if prop_cle not in mapping:
            raise pr.refus("hubspot_match_unmapped",
                           f"`{prop_cle}` (the matching property) must appear "
                           "in `field_mapping`. Nothing was sent.")
        if (associate_with is None) != (associate_id_column is None):
            raise pr.refus("hubspot_association_incomplete",
                           "`associate_with` and `associate_id_column` go together. "
                           "Nothing was sent.")
        lot = pr.ouvrir(datastore, row_ids=row_ids, filter=filter,
                        colonne_etat=status_column, limite=batch_size)
        inconnues = pr.colonnes_inconnues(
            lot, [*mapping.values(), *([associate_id_column] if associate_id_column else [])])
        if inconnues:
            raise pr.refus("push_rows_unknown_columns",
                           f"columns missing from the table: {', '.join(inconnues)}. "
                           "Nothing was sent.", columns=inconnues)

        recu = pr.Recu()
        # First pass, no call: what goes out, what is set aside, and why.
        a_pousser: list[tuple[str, dict, Optional[str], Optional[str]]] = []
        traitees = 0
        for ligne in lot.lignes:
            traitees += 1
            rid = str(ligne.get("_id"))
            if pr.tenue_ailleurs(ligne):
                recu.ecarter(rid, "row_locked")
                continue
            props, code = _proprietes(ligne, mapping)
            if code:
                recu.ecarter(rid, code)
                continue
            connu = pr.valeur(ligne, id_column)
            cle = _cle(props.get(prop_cle))
            if connu is None and cle is None:
                recu.ecarter(rid, "missing_match_value")
                continue
            vers_id = pr.valeur(ligne, associate_id_column) if associate_id_column else None
            a_pousser.append((rid, props, str(connu) if connu is not None else None,
                              str(vers_id) if vers_id is not None else None))

        if dry_run:
            recu.compter("would_push", len(a_pousser))
            return recu.rendre(lot, selectionnees=len(lot.lignes), dry_run=True,
                               traitees=traitees, object_type=object_type,
                               match_property=prop_cle,
                               written_back={"id_column": id_column,
                                             "status_column": status_column})

        c = _client() if a_pousser else None
        # `pousses` = what joins the list; `ecrits` = what was created or modified
        # in HubSpot (the billed quantity). An existing record left intact (`list_only`)
        # is in the first, never in the second.
        pousses: list[str] = []
        ecrits = 0
        proprietes_inconnues: list[str] = []
        try:
            if c is not None and list_id:
                fiche = c.get_list(list_id) or {}
                if (fiche.get("list") or fiche).get("processingType") == "DYNAMIC":
                    raise pr.refus("hubspot_list_dynamic",
                                   f"list {list_id} is DYNAMIC: its members are "
                                   "recomputed by HubSpot. Nothing was sent.")
            a_chercher = sorted({_cle(p.get(prop_cle)) for _, p, connu, _ in a_pousser
                                 if connu is None} - {None})
            existants = _existants(c, object_type, prop_cle, a_chercher) if a_chercher else {}
        except UpstreamHTTPError as e:
            scope = _scope_refusal(e, object_type)
            if scope is not None:
                raise scope from None
            raise

        # A row not sent (budget, stop) is not "processed": it stays.
        traitees -= len(a_pousser)
        for rid, props, connu, vers_id in a_pousser:
            if recu.budget_epuise():
                recu.arret = "time_budget"
                break
            traitees += 1
            cible = connu
            if cible is None:
                ids = existants.get(_cle(props.get(prop_cle)), [])
                if len(ids) > 1:
                    recu.echec(rid, "hubspot_ambiguous_match")
                    pr.ecrire(lot, rid, {status_column: ECHEC,
                                         f"{status_column}.comment": "hubspot_ambiguous_match"})
                    continue
                cible = ids[0] if ids else None
            if cible is not None and on_existing != "update":
                # An existing record we do NOT touch: no property, no association. It
                # joins the list only if asked (`list_only`).
                recu.compter(EXISTE)
                if on_existing == "list_only":
                    pousses.append(cible)
                ecrit = pr.ecrire(lot, rid, {
                    id_column: cible, f"{id_column}.comment": f"hubspot {object_type}",
                    status_column: EXISTE})
                if ecrit:
                    recu.echec(rid, f"writeback_{ecrit}")
                continue
            try:
                if cible is None:
                    rec = c.create_object(object_type, hubspot_auth.default_owner(
                        object_type, {**fixes, **props})) or {}
                    cible, etat = str(rec.get("id") or ""), CREE
                    if not cible:
                        recu.echec(rid, "hubspot_not_created")
                        pr.ecrire(lot, rid, {status_column: ECHEC,
                                             f"{status_column}.comment": "hubspot_not_created"})
                        continue
                    # The batch finds it again: a later row with the same value designates
                    # THIS record, instead of creating a second one.
                    cle = _cle(props.get(prop_cle))
                    if cle is not None:
                        existants[cle] = [cible]
                else:
                    c.update_object(object_type, cible, {**fixes, **props})
                    etat = MAJ
                ecrits += 1
                if associate_with and vers_id:
                    _associer(c, object_type, cible, associate_with, vers_id)
                    recu.compter("associated")
            except UpstreamHTTPError as e:
                statut = getattr(e, "status_code", None)
                if _scope_refusal(e, object_type) is not None:
                    recu.arret, traitees = "hubspot_missing_scopes", traitees - 1
                    break
                inconnues = unknown_properties(e)
                if inconnues:
                    # A mapped column HubSpot does not have: every row carries it,
                    # every row would fail the same way — one stop, named.
                    recu.arret, traitees = "hubspot_property_doesnt_exist", traitees - 1
                    proprietes_inconnues = inconnues
                    break
                if statut in (401, 429):
                    recu.arret = "rate_limited" if statut == 429 else "hubspot_http_401"
                    traitees -= 1
                    break
                code = "hubspot_conflict" if statut == 409 else f"hubspot_http_{statut}"
                recu.echec(rid, code)
                ecrit = pr.ecrire(lot, rid, {status_column: ECHEC,
                                             f"{status_column}.comment": code})
                if ecrit:
                    recu.echec(rid, f"writeback_{ecrit}")
                continue
            recu.compter(etat)
            pousses.append(cible)
            ecrit = pr.ecrire(lot, rid, {
                id_column: cible, f"{id_column}.comment": f"hubspot {object_type}",
                status_column: etat})
            if ecrit:
                # The record EXISTS in HubSpot: this code says the row does not know it.
                recu.echec(rid, f"writeback_{ecrit}")

        if list_id and pousses:
            try:
                for i in range(0, len(pousses), _PAGE):
                    c.add_list_memberships(list_id, pousses[i:i + _PAGE])
                recu.compter("added_to_list", len(pousses))
            except UpstreamHTTPError as e:
                recu.arret = recu.arret or f"hubspot_list_http_{getattr(e, 'status_code', None)}"

        # The BILLED line counts the records written, as N calls to
        # hubspot_object would have (`tool_calls.quantity`).
        session_org.note_call_trace(quantity=ecrits)
        return recu.rendre(lot, selectionnees=len(lot.lignes), dry_run=False,
                           traitees=traitees, object_type=object_type,
                           match_property=prop_cle, on_existing=on_existing,
                           written_back={"id_column": id_column,
                                         "status_column": status_column},
                           **_avis_existants(recu.comptes.get(EXISTE, 0), on_existing),
                           **({"unknown_properties": proprietes_inconnues}
                              if proprietes_inconnues else {}))
