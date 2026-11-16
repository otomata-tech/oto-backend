"""Datastore — lightweight structured data storage per user (native PG, ADR 0016).

Each user has their own set of "datastores". Free schema: each row = a JSON
dict (stored as JSONB, types preserved), fields appear as they go. Three
auto-managed fields exposed flat: `_id`, `_created_at`,
`_updated_at`. No external dependency — self-contained platform surface.

Surface ("fewer tools, more args"): `data_write`/`data_rows`/`data_share`
merge append↔update / get↔list / share↔unshare via a mode arg. The
destructive ones (delete_datastore, delete_row) and creation stay separate.
"""
from __future__ import annotations

import json
import uuid
from typing import Annotated, Optional

from pydantic import WithJsonSchema

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, db, ownership
from ..datastore import claimable, couches, identite, jetons, mots_deprecies
from ..datastore import recherche, upsert_implicite
from ..datastore import colonnes_non_declarees
from ..datastore import validation_complete
from ..datastore import charge_a_renvoyer
from ..datastore import forcage as fcg
from ..datastore import via as ds_via
from ..datastore import layers as dsl
from ..datastore import versions as dsver
from ..datastore.identite import AdresseJson as Adresse
from ..datastore.schema_keys import forme_schema
from ..datastore.outils import _current_run, adresse_servie
from ..datastore import schema as dsv2
from ..datastore.errors import RevisionConflict
from ..datastore.precondition import revision_attendue
from ..datastore.core import (
    indice_de_liberation,
    InvalidCursor,
    BusinessKeyRequired,
    DatastoreExists,
    DatastoreForbidden,
    DatastoreNotFound,
    DatastoreReadOnly,
    RowLocked,
    RowNotFound,
    RowValidationError,
    make_org_store,
    make_store,
)


_MARQUE_COUCHES = "<<couches>>"
_MARQUE_MOTS_DEPRECIES = "<<mots_deprecies>>"
_MARQUE_UPSERT_IMPLICITE = "<<upsert_implicite>>"
_MARQUE_UPSERT = "<<upsert>>"
_MARQUE_CLE_METIER = "<<cle_metier>>"
_MARQUE_COLONNES = "<<colonnes_non_declarees>>"
_MARQUE_Q = "<<recherche_q>>"
_MARQUE_Q_SCOPE = "<<recherche_q_scope>>"

#: Le paramètre `schema` porte sa FORME (cas du 14/10/2026) : le client passe au modèle les
#: clés admises nommées — `options` sur une colonne, jamais dans `meta`. Décrit, pas
#: analysé : le handler reçoit le même `dict`, `validate_schema_def` reste le seul juge.
_SchemaDeclare = Annotated[dict, WithJsonSchema(forme_schema())]


def _inserer(fn, phrases: dict):
    """Replaces each marker of the served description with its sentence. A missing
    marker RAISES: a description that had lost its sentence would serve the write without
    its vocabulary, and nobody would see it."""
    for marque, phrase in phrases.items():
        if marque not in (fn.__doc__ or ""):
            raise RuntimeError(f"{fn.__name__}: marker {marque} missing from the "
                               "description")
        fn.__doc__ = fn.__doc__.replace(marque, phrase)
    return fn


def _avec_la_phrase_des_couches(fn):
    """Inserts into the served description the layers sentence, held by
    `couches.DESCRIPTION_ECRITURE` — the same one the REST face serves (oto#91) —,
    the refusal of the removed words (`@keep`, `@clear`, oto#140) and the one on
    merging on the business key, which is REQUESTED (`upsert`, oto#141)."""
    return _inserer(fn, {
        _MARQUE_COUCHES: couches.DESCRIPTION_ECRITURE,
        _MARQUE_MOTS_DEPRECIES: mots_deprecies.DESCRIPTION_ECRITURE,
        _MARQUE_UPSERT_IMPLICITE: upsert_implicite.DESCRIPTION_ECRITURE,
        _MARQUE_UPSERT: upsert_implicite.description_parametre(),
        _MARQUE_COLONNES: (colonnes_non_declarees.DESCRIPTION_ECRITURE + " "
                           + validation_complete.description_ecriture())})


def _avec_la_regle_de_cle(fn):
    """What the declared business key does on write (oto#141), and the rule on
    undeclared columns (oto#124) — the same sentences as `data_patch_schema` and the
    REST face."""
    return _inserer(fn, {
        _MARQUE_CLE_METIER: upsert_implicite.description_cle_schema(),
        _MARQUE_COLONNES: (colonnes_non_declarees.description_schema() + " "
                           + validation_complete.description_schema())})


def _avec_la_recherche(fn):
    """Ce que `q` et `q_scope` font (#307) — les phrases de `datastore.recherche`,
    les mêmes que sert la face REST, défaut compris."""
    return _inserer(fn, {_MARQUE_Q: recherche.DESCRIPTION_Q,
                         _MARQUE_Q_SCOPE: recherche.DESCRIPTION_Q_SCOPE})


def _avec_la_creation(fn):
    """What creation says about the birth schema (oto#124) — the same sentence as the
    REST face (`POST /api/datastores`)."""
    return _inserer(fn, {_MARQUE_COLONNES: colonnes_non_declarees.description_creation()})


def _store_for(sub: str):
    return make_store(sub)


def _acting_store():
    """Datastore store for the current actor, for the NON-governance tools
    (list/read/write/schema).

    - Authenticated user (`sub`) → their store, context = their active org (unchanged).
    - MCP `secret` endpoint with datastore opt-in (ADR 0032) → store acting UNDER
      THE ORG that owns the project (sub-less), **scoped to the tables LINKED to the project**
      (anti-leak #193) and **read-only** unless a separate write opt-in.
    - Otherwise (login-less endpoint WITHOUT opt-in) → McpError "Unauthenticated".

    The GOVERNANCE/destructive tools (create/delete/rename/share) do NOT use
    this seam: they keep `current_user_sub_or_raise()` → never exposed on an endpoint
    without an identified user."""
    sub = access.current_user_sub_from_token()
    if sub:
        return make_store(sub)
    from .. import subdomain_project
    if subdomain_project.current_anon_datastore_exposed():
        return make_org_store(
            int(subdomain_project.current_anon_org()),
            allowed_ns_ids=_anon_project_tableau_ns_ids(
                subdomain_project.current_anon_project_id()),
            read_only=not subdomain_project.current_anon_datastore_writable())
    access.current_user_sub_or_raise()  # no opt-in → raises "Unauthenticated"


def _anon_project_tableau_ns_ids(project_id: Optional[int]) -> frozenset:
    """Ids of the datastores LINKED to the project (`project_links` of type tableau) — the datastore
    exposed on a shared endpoint is scoped to THESE tables, never the whole datastore of
    the org (anti-leak #193). The identifier is the one `db.list_project_links`
    resolved (`datastore_id`): a link by NAME is resolved there within the scope of the project's
    owner, an ambiguous or out-of-scope name exposes NOTHING (#365) — the same rule as
    the rail and the shared page, written once. project_id None / error / no link ⇒
    frozenset() (nothing exposed, never an open fallback)."""
    if project_id is None:
        return frozenset()
    try:
        return frozenset(int(l["datastore_id"]) for l in db.list_project_links(int(project_id))
                         if l.get("target_type") == "tableau"
                         and l.get("datastore_id") is not None)
    # noqa: SILENT — anonymous hint: empty set rather than a wrong list
    except Exception:  # noqa: BLE001
        return frozenset()


def _project_hint(datastore: str) -> Optional[str]:
    """Inverse run→link suggestion (ADR 0035 B5): writing under an ACTIVE PROJECT into a
    datastore NOT linked to the project ⇒ suggest the link — today it is LLM
    discipline ("remember to link"), here the substrate reminds at the moment of
    the act. Never blocking, never auto-link (the link is a decision).
    Best-effort: any error ⇒ None."""
    try:
        pid = access.current_project()
        if pid is None:
            return None
        links = db.list_project_links(int(pid))
        # ⚠️ **The debt noted here on 08/09 is PAID BACK (09/09/2026)**, exactly
        # as it prescribed: by an alias and a date, not by a text
        # replacement. `db.list_project_links` now sets `datastore` AND `namespace`
        # on every `tableau` link; `/api/me/projects` therefore serves both to the three
        # fronts, and the duplicate drops at `RETRAIT_DATASTORE` (08/11/2026) along with the 24
        # REST paths and the doubled key of the datastore responses.
        #
        # We read the NEW key. ⚠️ Reading the one that will disappear was the real trap:
        # on removal day, the set would have become empty again and the hint would have suggested
        # linking a table ALREADY linked, on every call and for everyone, without
        # any error signalling it — it is the defect we had already paid for by
        # reading the wrong one of the two, the other way round.
        # ⚠️ **A link is designated in TWO forms, and this hint only compared one**
        # (#858, 10/09/2026). `list_project_links` enriches each `tableau` link with the
        # NAME of its table under `datastore`; but a write commonly targets its
        # ID. A table linked by id, written by id, therefore missed: the hint
        # claimed "not linked" on a link set seven minutes earlier. Four
        # successive sub-agents believed it, two reported it up as an action
        # to take, one proposed creating the link — a duplicate narrowly avoided.
        # The immediate neighbour (`_anon_project_tableau_ns_ids`) ALREADY resolved both
        # forms, thirty lines above, stating so in its docstring.
        cible = str(datastore).strip()
        tableaux = [l for l in links if l.get("target_type") == "tableau"]
        formes = {str(l.get("datastore") or "").strip() for l in tableaux}
        formes |= {str(l.get("target_ref") or "").strip() for l in tableaux}
        if cible in formes:
            return None
        # ⚠️ **And when in doubt, this hint stays SILENT.** One case remains that cannot be
        # settled without one more query: the target is an id, a link carries a
        # name, and nothing here says whether that name IS this table. Claiming "not linked" there
        # would replay exactly the defect we are fixing, whereas staying silent costs
        # only a reminder. The hint has never been blocking: its silence is free,
        # its error is not.
        if cible.isdigit() and any(not f.isdigit() for f in formes if f):
            return None
        return (f"this table `{datastore}` is not linked to the active project (#{pid}) — "
                f"if it is an output of the project, link it: `oto_project op=link "
                f"project_id={pid} target_type=tableau target_ref=<datastore id> "
                "(+ slot='<name>' if it fulfils a procedure slot)`.")
    # noqa: SILENT — declared debt: the project hint disappears silently (#424, verdict C)
    except Exception:  # noqa: BLE001
        return None


def _omitted_run_hint(e: RowLocked) -> Optional[str]:
    """The refusal TEACHES the most frequent fault: omitted `_run_id` (#547).

    Same seam as #515 — rephrase the refusal from the CALLER's point of view. Measured on
    29/08/2026 on a campaign: 31 writes refused out of 100, **all** on a
    row the caller itself held, the token passed at claim time (140/140)
    then omitted at write time. A refusal that describes the state of the world ("reserved by w8")
    lets one deduce the fault; this one NAMES it, but only when it can
    prove it.

    Three conditions, all necessary:
    - the call carries NO run, neither `_run_id` nor the session's active run (#830)
      — that is precisely the fault;
    - the lease is held by an identified run;
    - that run belongs to the SAME sub as the caller. ⚠️ Without this last test we
      would reveal to a third party the token that lifts the lock: `_run_id` authorizes nothing,
      it NAMES (see `call_axes._pin_run`) — printing it in a refusal addressed to
      someone else would turn the lock into a label.

    Best-effort: any error ⇒ None. A refusal does not fail because a hint is missing.
    """
    try:
        from .. import session_org
        if session_org.current_call_or_session_run():
            return None                      # a run is known (named or session's): other cause
        run = getattr(e, "claimed_run", None)
        if not run:
            return None                      # lease without a run (worker only): nothing to say
        sub = access.current_user_sub_from_token()
        head = db.get_run_head(str(run))
        if not sub or not head or head.get("sub") != sub:
            return None                      # not yours: we don't name the run
        return (f"You passed no `_run_id` on this call, and this row is held "
                f"by YOUR run `{run}` — you probably omitted it: pass "
                f"`_run_id={run}` again on EVERY call until `run_finish`, it is not "
                f"inherited from one call to the next.")
    # noqa: SILENT — a missing hint must never mask the refusal itself
    except Exception:  # noqa: BLE001
        return None


def _row_locked_message(e: RowLocked) -> str:
    """The text served for a reserved-row refusal: the refusal's message, plus
    the `_run_id` omission hint when it is proven (#547)."""
    hint = _omitted_run_hint(e)
    return f"{e} {hint}" if hint else str(e)


# Bound on a delete batch (#1268): each row is a transaction, the batch
# stays a short call.
MAX_DELETE_IDS = 500


def _delete_rows(store, datastore: str, id, expected_revision, ids) -> dict:
    """MCP face of the delete batch (#1268): `ids` = `_id`s, or
    `{id, expected_revision}`.

    ⚠️ **The whole batch is VERIFIED before the first deletion**: an unreadable item
    at the 30th position must not leave 29 rows already gone. PER-ROW refusals (lease,
    revision), on the other hand, do not cut the batch: they are returned with their row."""
    def refus(message: str):
        return McpError(ErrorData(code=INVALID_PARAMS, message=message))
    if id is not None or expected_revision is not None:
        raise refus("`ids` replaces `id` and `expected_revision`: put each row's "
                    "revision in its item, `{\"id\": …, \"expected_revision\": …}`")
    if not isinstance(ids, list) or not ids:
        raise refus("`ids` = a non-empty list of `_id` (or `{id, expected_revision}`)")
    if len(ids) > MAX_DELETE_IDS:
        raise refus(f"`ids`: {len(ids)} items, at most {MAX_DELETE_IDS} per call")
    datastore, _ = _adresse(datastore)
    items, vus = [], set()
    for rang, item in enumerate(ids):
        if isinstance(item, dict):
            row_id, attendue = item.get("id"), item.get("expected_revision")
            inconnues = set(item) - {"id", "expected_revision"}
            if inconnues:
                raise refus(f"`ids[{rang}]`: unknown keys {sorted(inconnues)} — "
                            "an item is `{id, expected_revision}`. Nothing deleted.")
        else:
            row_id, attendue = item, None
        if not isinstance(row_id, str) or not row_id.strip():
            raise refus(f"`ids[{rang}]`: an `_id` string is required, got {item!r}. "
                        "Nothing deleted.")
        if row_id in vus:
            raise refus(f"`ids[{rang}]`: `{row_id}` appears twice. Nothing deleted.")
        vus.add(row_id)
        try:
            jetons.verifier_champs(id=row_id)
            items.append((row_id, revision_attendue(attendue)))
        except ValueError as e:                  # JetonMalPlace, unreadable revision
            raise refus(f"`ids[{rang}]`: {e}")
    try:
        bilan = store.delete_rows(datastore, items)
    except DatastoreNotFound as e:
        raise refus(_inconnu(datastore, e))
    except DatastoreReadOnly:
        raise refus(f"datastore `{datastore}` shared read-only")
    refused = []
    for row_id, e in bilan["refused"]:
        entree = {"id": row_id,
                  "error": _row_locked_message(e) if isinstance(e, RowLocked) else str(e)}
        if isinstance(e, RevisionConflict):
            entree["current_revision"] = e.current_revision
        refused.append(entree)
    return {"ok": not refused, "count": len(bilan["deleted"]),
            "deleted": bilan["deleted"], "not_found": bilan["not_found"],
            "refused": refused}


def _adresse_de_couche_valide(champ: str, present: set, declared: set) -> bool:
    """Does `effectif.origine` target a layer of a KNOWN column? (#350)

    ⚠️ Exact recognition, never approximation: the layer must be one of the three
    names the server knows, and the column must be present on the page or
    declared in the schema. `effectif.bidule` stays unknown, `inconnue.origine` too — we
    stay silent only on what is really addressable.

    The schema does NOT declare the layers: they are native and universal (every
    column has them), so `declared` will never contain them. That is what made
    the warning unavoidable as soon as the layer was empty across the whole page."""
    base, point, couche = champ.partition(".")
    if not point or couche not in dsv2.LAYER_KEYS:
        return False
    return base in present or base in declared


def _datastore_keys(store, datastore: str) -> set[str]:
    """Keys actually present in the datastore's DATA (bounded sampling).

    Third judge, after the schema and the page: an ORPHAN column — present
    in the database, dropped from the schema by a rename — is neither declared nor necessarily on
    the page drawn. Announcing it as "unknown, check the spelling" would again point to
    a wrong cause; it exists, it is simply no longer in the format.
    Unavailable ⇒ set() (we don't stay silent on a doubt, we keep the least
    costly accusation: signal)."""
    try:
        ns_id = store._resolve(datastore)
        return set(db.datastore_row_keys(ns_id))
    # noqa: SILENT — unreadable datastore keys ⇒ no typo warning
    except Exception:  # noqa: BLE001
        return set()


def _targeted_columns(filter: Optional[dict], filters: Optional[list]) -> set[str]:
    """The columns a call TARGETS, whatever form is used.

    Both forms must feed the anti-typo warning: a misspelled column in `fields` would return fewer
    rows without saying anything, which is exactly the trap this warning exists to close — and reopening it on the
    NEW form would be reopening it where the agent most needs help.

    The layer suffix is removed (`email.comment` targets the `email` column), and the
    system columns are set aside: they never appear in `data`, announcing them as
    unknown would point to a wrong cause."""
    vise = set(filter or {})
    for f in (filters or []):
        if not isinstance(f, dict):
            continue
        cibles = f.get("fields") if f.get("fields") is not None else [f.get("field")]
        for c in (cibles if isinstance(cibles, (list, tuple)) else []):
            if isinstance(c, str) and c:
                vise.add(db.split_layer(c)[0])
    return {c for c in vise if not c.startswith("_")}


def _unknown_filter_keys(store, datastore: str, filter, filters=None) -> set[str]:
    """Keys of `filter`/`filters` absent from ALL the rows of a datastore sample
    (feedback #163: filter on a nonexistent column = silent 0 results,
    indistinguishable from "no row matches"). Empty-result path only.
    Empty datastore or error ⇒ set() (nothing assertable, no false warning).

    ⚠️ The schema takes precedence over the sample, for the same reason as the projection:
    a declared but sparsely filled column may be missing from the 50 rows drawn, and
    announcing it as unknown would send someone looking for a spelling mistake that does not
    exist. A legitimate filter on a rare column returns 0 rows — that is an answer,
    not a symptom."""
    # The schema is read separately: if it is unavailable, we FALL BACK on the sample
    # instead of switching the warning off. Putting it in the shared try would make
    # a useful signal disappear at the first hiccup reading the schema —
    # exactly the kind of silence this warning exists to fight.
    try:
        known = set(dsv2.top_level_keys(store.get_schema(datastore)))
    # noqa: SILENT — unreadable schema ⇒ fall back on the sample, the warning survives
    except Exception:  # noqa: BLE001
        known = set()
    try:
        sample = store.cursor_rows(datastore, limit=50)["rows"]
        if not sample and not known:
            return set()
        for r in sample:
            known |= set(r.keys())
        unknown = {k for k in _targeted_columns(filter, filters) if k not in known}
        # Same last resort as the projection: an orphan exists in the database
        # without being declared nor necessarily in the sample.
        return {k for k in unknown
                if k not in _datastore_keys(store, datastore)} if unknown else set()
    # noqa: SILENT — last resort: undeclared orphan column, no warning
    except Exception:  # noqa: BLE001
        return set()


def _inconnu(datastore: str, e: DatastoreNotFound) -> str:
    """"unknown" — and, when the table exists in another org of the caller, WHERE and
    WHAT to pass (#631). The hint comes from the store (`datastore/hors_org`), the same lookup
    as the REST face; without a hint, the bare refusal as before."""
    indice = getattr(e, "indice", None)
    return f"datastore `{datastore}` unknown" + (f" — {indice}" if indice else "")


def _introuvable(row_id: object, piste: Optional[str]) -> str:
    """The "not found" refusal of the write path (#517) — the form is DESCRIBED, it
    is not shown.

    The first version showed an identifier as an example, "five hexadecimal
    groups". On 29/08 at 15:24, an agent read a template to fill in there and returned
    `6738f4c2-57c0-43b9-9d78-XXXXXXXXXXXX` — twelve X's in place of the group it did not
    know. An example in a refusal is a template: we no longer put any. And
    when what is received does not even have the shape of an identifier, we say so — it is
    proof that it was made up, not altered."""
    try:
        uuid.UUID(str(row_id))
        forme = ""
    except (ValueError, AttributeError, TypeError):
        forme = " (and this is not the shape of a row identifier)"
    return (f"row `{row_id}` not found{forme} — a row identifier is a 36-character "
            "UUID returned by `data_write`/`data_claim_next`: it is not made "
            "up, it is read back from the reply that returned it"
            + (f" ; {piste}" if piste else ""))


# The rendering of an EMPTY claim also says what NOT to do next. On 29/08 at 15:24,
# a job received `row: null` then wrote anyway — the pronoun of the time (`@claimed`,
# since removed), then a fabricated identifier. Nothing got through, but the claim's
# rendering had not warned it.
_HINT_RIEN_TENU = (" — you hold NO row: write nothing, invent no "
                   "identifier, finish your work (`run_finish`)")
_HINT_FILE_VIDE = ("nothing left to claim (queue empty for this filter, or everything is under an "
                   "active lease)" + _HINT_RIEN_TENU)


# #727: without a run, the claim returned a real row AND set a lease — but a lease is
# held by its RUN (`_lease_guard`), so the write was refused AFTER the investigation, and the
# next claim, under a run, returned another row. The refusal names the missing gesture and sets
# nothing. Never an implicit run: opening a trace in the caller's place would
# attribute to it a fact it did not set.
_REFUS_SANS_RUN = (
    "`data_claim_next` refused: no active run on this call — NOTHING was reserved, the "
    "row stays with the next agent. A reservation is held by its run: outside a run, you "
    "would investigate a row you could not write. Open your work with "
    "`run_start`, then pass its `run_id` as `_run_id=` on THIS call and on every "
    "write that follows.")


def _hint_file_vide(perimetre: dict, filter: Optional[dict]) -> str:
    """Nothing served: the queue is empty FOR THIS SCOPE, and it is named (#517) — a
    filter that contradicts the table's declaration must not read as "queue
    empty". What follows does not change: the agent holds nothing, it writes nothing."""
    if not perimetre:
        return _HINT_FILE_VIDE
    return claimable.phrase_vide(perimetre, filter) + _HINT_RIEN_TENU


def _row_not_found_hint(store, datastore: str, row_id: object) -> str:
    """Actionable message for a failed `id` lookup (feedback #161: the `id` param
    searches by technical `_id` UUID; when the schema declares a business key —
    often named `id` — the agent naturally passes ITS value and hits
    "not found" with no lead). If a row matches the business key, we say so."""
    msg = f"row `{row_id}` not found (the `id` param searches by technical `_id`)"
    try:
        key = store.declared_key(datastore)
        if key:
            hit = store.cursor_rows(datastore, filter={key: row_id}, limit=1)["rows"]
            if hit:
                return (f"{msg} ; a row does have `{key}={row_id}` (business key) — "
                        f"use `filter={{\"{key}\": \"{row_id}\"}}`, its `_id` is "
                        f"`{hit[0].get('_id')}`")
            return f"{msg} ; for the business key `{key}`, use `filter={{\"{key}\": …}}`"
    # noqa: SILENT — declared debt: the "row not found" hint disappears (#424, verdict C)
    except Exception:  # noqa: BLE001
        pass
    return msg


def _project_row(row: dict, fields: list[str]) -> dict:
    """Projects a row onto `fields` (subset of columns, feedback #191) while
    ALWAYS keeping `_id` — without it the agent could no longer address/update
    the row. A DECLARED column is always in the served row (as `null` when it has no value,
    oto#182) and therefore comes out as `null`; an undeclared and absent name is omitted."""
    if TOUT in fields:
        # `["*"]` asks for EVERYTHING — not a column named `*`. The token has always been legitimate on
        # `oto_doc` and on the feed; refusing it here returned `_id` alone to
        # an agent that believed it was asking for the whole row (inventory of 29/08).
        return row
    keep = set(fields)
    keep.add("_id")
    return {k: v for k, v in row.items() if k in keep}


TOUT = "*"  # `fields=["*"]` — "all columns", the same token as on oto_doc


def _adresse(datastore: str, id=None):
    """The ADDRESS fields of a call: verified, then the table resolved — the SAME
    gesture on all verbs (#517).

    Written once rather than six times: the two faces diverged exactly once, and
    silently — `slot:` was resolved by the schema operations and passed raw by
    the row ones, which answered "datastore unknown" on a perfectly valid
    token.

    ⚠️ **No longer takes `store`, `worker`, nor `ligne`** (07/09/2026): these three
    parameters existed only for `@claimed`, which read the current run's lease to
    turn a pronoun into an identifier. With the pronoun removed, `id` is only VERIFIED
    — and verifying it remains necessary, because that is where an agent that still writes it
    receives a refusal that names the gesture that succeeds, instead of "row not found".

    The refusal crosses the surface as `INVALID_PARAMS` — it CARRIES the course of action, and
    an internal error would erase it at the very moment it is useful."""
    try:
        return jetons.resoudre(datastore, id, resoudre_slot=_ns)
    except jetons.JetonMalPlace as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))


def _ns(datastore: str) -> str:
    """SLOT addressing (ADR 0035 B3): `slot:<name>` = the table bound under that
    name by the ACTIVE PROJECT (`access.resolve_slot_tableau` — actionable error if
    no active project / slot not bound / dangling binding, NEVER a fallback).
    A bare name passes unchanged (zero magic on literal names).

    Body moved to `access.resolve_datastore_ref` (single source): the datastore
    capabilities need it too, and having kept it here left `slot:` unresolved
    on their MCP face."""
    return access.resolve_datastore_ref(datastore)


def _destinataire(email: str, recipient_sub: str) -> dict:
    """The account that will receive access — never guessed.

    MCP face of the same gesture as `capabilities/datastore/sharing._destinataire`.
    Both exist (accepted debt: `data_*` in MCP, `/api/datastore/*` in
    REST) and it is THIS one that agents take. Fixing the other alone
    would have closed the back door while leaving the main one open.

    ⚠️ An address does not designate an account: ten of them carry two (measured on
    05/09/2026). Sharing on one of them opened the table to whichever one
    `fetchone()` returned first, without the owner finding out.
    """
    email = (email or "").strip()
    recipient_sub = (recipient_sub or "").strip()
    if email and recipient_sub:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="give `email` OR `recipient_sub`, not both: they may "
                    "designate different accounts, and the share would succeed toward "
                    "a target that nothing would name."))
    if recipient_sub:
        row = db.get_user(recipient_sub)
        if not row:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=f"no oto account with the sub `{recipient_sub}`"))
        return row
    if not email:
        raise McpError(ErrorData(code=INVALID_PARAMS,
                                 message="`email` (or `recipient_sub`) is required."))
    porteurs = db.get_users_by_email(email)
    if not porteurs:
        raise McpError(ErrorData(code=INVALID_PARAMS,
                                 message=f"no oto user with the email {email}"))
    if len(porteurs) > 1:
        subs = ", ".join(f"`{u['sub']}`" for u in porteurs)
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"The address `{email}` designates {len(porteurs)} accounts: {subs}. "
                    "Retry with `recipient_sub` (without `email`) to say which one you mean."))
    return porteurs[0]


def pick_fiche(rows: list, schema: Optional[dict], row: str) -> Optional[dict]:
    """Find ONE record by `row`: match on `_id`, the declared business key
    (`schema.key`), or the title field's value — the agent's natural landmark. Shared by
    both faces of `data_app` (the Prefab card here, the front's bundle in
    `datastore_table_app`)."""
    biz_key = (schema or {}).get("key")
    title_key = (dsv2.title_field(schema) or {}).get("key")
    target = str(row)
    for r in rows:
        for probe in ("_id", biz_key, title_key):
            if probe and str(r.get(probe)) == target:
                return r
    return None


def register(mcp: FastMCP) -> None:

    # ⚠️ The docstring names `ns_id` DELIBERATELY (oto#176). This catalog only returned
    # the number under `id` while `data_rows` prescribed `ns_id` as "the
    # form to use": the agent looked for the prescribed key in the only reply
    # that lacked it. The registry now serves both — the text says so, without
    # which the key exists for whoever already knows it, i.e. for nobody.
    @mcp.tool()
    def data_list_datastores() -> dict:
        """List the datastores of the active org: the org's and your teams' tables,
        those shared with the org or your teams, and your personal tables created in
        it. A table shared with you as a person (`shared: true`) is listed in the org
        it belongs to when you are a member there, otherwise in your active org.

        A table missing from this list is not gone: it still opens by its number
        from any org.

        Each entry carries the table's NUMBER under BOTH names — `ns_id` (the form
        to pass as `datastore` from here on, and the one every other reply uses)
        and `id` (the same number, kept for the dashboard's deep links)."""
        store = _acting_store()
        return {"datastores": store.list_datastores()}

    # ⚠️ This description said "unique per user" until 04/09/2026, when the code
    # had always created ORG tables. The lie was fixed TWICE that
    # day: first the text (`ab6d0eff`), then the behaviour itself (ADR 0068,
    # the table is born personal) — and it is the second that makes the first obsolete.
    # The story lives HERE and not in the docstring: a description is an instruction
    # reread on every call, and QUOTING the faulty phrasing there, even to deny it, is
    # re-serving it to the model.
    # `tests/test_description_dit_le_proprietaire.py` reads the real default in the code
    # then requires the served text to name that default — never the reverse. It is what
    # refused to turn green when the default changed, before this text moved.
    @mcp.tool()
    @_avec_la_creation
    def data_create_datastore(datastore: Adresse,
                              schema: Optional[_SchemaDeclare] = None) -> dict:
        """Create a new datastore (PG-backed), optionally WITH its typed schema.

        <<colonnes_non_declarees>>

        The table is PRIVATE: it belongs to you, and no one else can read it — not
        the other members of your org, not its admins. That is the default and it is
        never implicit (ADR 0068).

        ⚠️ It used to be owned by your ACTIVE ORG, readable by every member. To share
        a table with your org or a team, say so: the REST route takes
        `owner: {type: "org"|"group", id: N}`. Tables created before 2026-09-04 keep
        the owner they have.

        ⚠️ **`_org=` does NOT change the owner.** It decides which org you read and
        write under — never who owns what you create. Create a table under `_org=N`
        without an owner and it is still YOURS: every later call of yours keeps
        working, so nothing looks wrong. It shows up at the second agent, or at the
        colleague who cannot find the table and concludes it does not exist. The
        reply tells you the owner, and warns you in exactly that case.

        A personal table is LISTED in your PERSONAL org, whatever org you create it
        under — `data_list_datastores` from any other org does not show it. Its number
        (`ns_id`) opens it from anywhere.

        Args:
            datastore: kebab-case identifier, unique per owner (e.g. `timetrack`).
            schema: optional — the table's schema, the SAME object as
                `data_set_schema(schema=…)`. Omitted (or null) = a free table.
        """
        sub = access.current_user_sub_or_raise()
        if not datastore or not datastore.strip():
            raise McpError(ErrorData(code=INVALID_PARAMS, message="datastore required"))
        if datastore.strip().lower().startswith(access.SLOT_PREFIX):
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=("a slot binds an EXISTING table — create the datastore under its "
                         "real name, then bind it to the project "
                         "(`oto_project op=link target_type=tableau … slot='<name>'`).")))
        store = _store_for(sub)
        try:
            return store.create_datastore(datastore.strip(), schema=schema)
        except DatastoreExists:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"datastore `{datastore}` already exists",
            ))
        except ValueError as e:
            # The birth schema was refused (oto#124): the table was not created.
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"{e} — the table `{datastore.strip()}` was NOT created."))

    @mcp.tool()
    def data_delete_datastore(datastore: Adresse) -> dict:
        """Delete a datastore and all its rows (irreversible). Owner (or org/platform
        admin governing it) only."""
        sub = access.current_user_sub_or_raise()
        datastore = _ns(datastore)
        store = _store_for(sub)
        try:
            store.delete_datastore(datastore)
        except DatastoreNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(datastore, e)))
        except DatastoreForbidden:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=f"you are not allowed to delete `{datastore}`"))
        # The identity of what was just deleted, not an echo of the address: the
        # table no longer exists, so this is the ONLY trace the caller keeps of it.
        return {"ok": True, **identite.de_releve(store.dernier_tableau, datastore)}

    @mcp.tool()
    def data_rename_datastore(datastore: Adresse, new_name: str) -> dict:
        """Rename a datastore. Only the name changes — the id, URL/deeplink and shares
        stay stable (grants are keyed by id). Governance right required (owner, or the
        org/platform admin governing it). The new name must be free for the same owner.

        Use this to lift a name collision (e.g. two `reconcile_log` across orgs) before
        transferring/consolidating: rename one side, then transfer with `oto_resource`.

        Args:
            datastore: current datastore (or `slot:<name>` under the active project).
            new_name: the new kebab-case name (must be unique for the owner).
        """
        sub = access.current_user_sub_or_raise()
        datastore = _ns(datastore)
        if not new_name or not new_name.strip():
            raise McpError(ErrorData(code=INVALID_PARAMS, message="new_name required"))
        if new_name.strip().lower().startswith(access.SLOT_PREFIX):
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message="`slot:` is reserved for addressing — pick a real name."))
        store = _store_for(sub)
        try:
            return store.rename_datastore(datastore, new_name)
        except DatastoreNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(datastore, e)))
        except DatastoreForbidden:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=f"you are not allowed to rename `{datastore}`"))
        except DatastoreExists as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))

    @mcp.tool()
    @_avec_la_regle_de_cle
    def data_set_schema(datastore: Adresse, schema: Optional[_SchemaDeclare] = None,
                        semantic_search: Optional[bool] = None) -> dict:
        """Declare (or clear with schema=null) a datastore's TYPED schema (ADR 0032 §6).

        A typed datastore renders as readable cards/records instead of a flat table.
        `schema` = {"fields": [{"key": str, "label"?: str, "type"?: "text|number|date|
        datetime|bool|json|object|list|url|email|phone|ref|enum",
        "options"?: [str], "display"?: "title", "role"?: "status|metric|note|qualif",
        "description"?: str, "meta"?: {…}}],
        "key"?: str, "new_rows"?: "create|reject", "description"?: str,
        "meta"?: {…}}.
        An `enum` column's allowed values go in `options`, ON the field
        (`{"key": "status", "type": "enum", "options": ["kept", "rejected"]}`) —
        that is what constrains the values and what a board groups on. Never in
        `meta`: a key the field admits is REFUSED there (stored, it would do
        nothing).
        ⚠️ **The vocabulary is CLOSED: a key its level does not admit is REFUSED** —
        at the head, on a field, on a sub-field, in `of`, in `lifecycle`. The refusal
        names the path, the key and the closest known one (`read_only` → `readonly`),
        or where it belongs (`states` → `lifecycle.states`, `semantic_search` → a
        parameter of this call). An unknown key ALREADY stored and left unchanged is
        tolerated (named in `warning`), never one you add or change. Your own
        annotations go in `meta` — an object, allowed at every level, carried as is,
        never read, bounded in size. Help text goes in `description` (`note`,
        `help`, `hint`, `placeholder` are refused). Each level's keys, who reads
        each one and the `meta` bound (`meta_max_bytes`) are served at
        `GET /api/datastore/schema/keys`.
        ⚠️ This REPLACES the schema — it does not merge. Any setting absent from the
        body you send DISAPPEARS (a field note, a bound, options, the business key and
        its UNIQUE index). The response now names what it just removed
        (`declarations_effacees`, with the lost VALUES — it is the only copy left) and
        `enforced` (the validation keys THIS version applies). To EDIT a schema without
        risking part of it, use `data_patch_schema`: it merges by key and cannot
        destroy what it does not name.
        The response also carries `existing_violations`: per path (`contacts[].email`),
        the rows ALREADY IN PLACE that the posed schema condemns — `rows`,
        `blocking_rows` (rows that accept no write at all until fixed), `sample_ids`
        and `consequence`. Clean them before arming the guard;
        `existing_violations_scope.complete: false` means the row cap was hit and the
        counts are floors.

        The optional top-level `"key"` names the field that is the row's BUSINESS KEY
        (e.g. "email", "siren"). <<cle_metier>>
        <<colonnes_non_declarees>>
        ONE head setting is left (`strict`, `unknown_fields` and `key_required` were
        replaced on 2026-10-02; `unknown_columns` was REMOVED on 2026-10-05 — no
        setting decides what is checked, all are refused):
        - `"new_rows"` — whether a NEW row may be born: `"create"` (default) or
          `"reject"`, which CLOSES the table: a write that designates NO existing
          row (no `id`, and no key value the table already carries) is REFUSED
          instead of creating one. Needs `key`. A table often fills up before it
          has its key, hence the default.
        `data_get_schema` serves it AS APPLIED (`reglages`). Pass schema=null to
        switch back to free-table mode.

        PRESENTATION — the schema also DRIVES THE UI (there is no visual editor:
        this tool IS the way to configure how a table looks):
        - field ORDER in the record view = the order of `fields` here. Reorder the
          list to reorder the form.
        - `type` picks the WIDGET: `date`/`datetime` → date picker (readable, not a
          raw ISO string), `url` → compact field + open link (never a giant text
          box), `email` → mail field, `enum` (+ `options: [...]`) → dropdown,
          `bool` → true/false, `number` → numeric. Untyped fields fall back to a
          plain text box, so DECLARE the type when the rendering matters.
        - `width: "half"|"full"` = the field's width in the record form. Without it
          the width is derived from the widget — declare it to keep a stable layout.
        - `hidden: true` = keep the field OUT of the table columns by default (still
          editable in the record). Use it for opaque ids and technical fields.
        - `display: "title"` NAMES the row: that field titles the record everywhere
          the server names a line (work queue, undo, cards) instead of a raw `_id`.
          One per table.
        - `role` only steers the dashboard's rendering (`metric` tiles, `note`
          placed last, `status` badges) — oto does not read it: a `lifecycle` block
          designates its own column, no role needed.
          ⚠️ The dashboard still titles rows from `role: "title"`, which the server
          no longer reads. On a table meant to look right in BOTH, declare both until
          they converge.

        STRUCTURED RECORDS (ADR 0046 — every layer opt-in):
        - nested types: `type:"object"` + `fields:[…]` (sub-record, e.g. occupant);
          `type:"list"` + `of:<field-def>` (list of scalars or sub-records, e.g.
          contacts = list of {nom, titre, email}).
        - write validation: `field.required: true`, type conformity,
          `field.required_when: {"<field>": "<value>"}` (e.g. deliverables required
          when status="qualified" — ⚠️ to UNDO, clear the gated field AND leave the
          triggering state in the SAME `data_write` call: the gate is judged on the
          MERGED row, so a call that only clears the field while the state still
          matches is refused), `field.max_length: <int>` on a SCALAR field, and
          `field.pattern: "<regex>"` for its SHAPE when the size does not separate
          anything (a code, a snake_case identifier) — `re.search`, so anchor it
          yourself (`^…$`). A `pattern` applies on its own: the value it reads is
          bounded by `max_length` when declared (≤1000), else by 1000 characters —
          a longer value is refused. The cost of a regex is bounded against that
          length, and oto refuses what it cannot price — an ambiguous repeated
          group, a backreference, a lookaround are rejected AT DECLARATION TIME,
          each naming why. `type: "phone"` holds a phone number: international
          (`+`, country code — E.164) preferred, national digits accepted, spaces,
          dots and dashes allowed; a sentence or an identifier is refused.
          `type: "ref", "target": <table number>` is a LINK column: the cell holds
          the `_id` of a row of that table (a list of links:
          `"type": "list", "of": {"type": "ref", "target": <number>}`). A name
          written there is refused — find the row with `data_rows` on the target
          and write its `_id`. The target must be a table you can read.
          `field.required_layers: ["comment"]` refuses a write that leaves a
          NON-EMPTY value in that column without posting the layer — the value must
          arrive WITH its provenance, in the SAME call
          (`"col": {"valeur": …, "comment": "where it comes from"}`). A null or
          empty value, and a layer posted alone without a value (a note beside the
          value, not an assumed empty — that is `@empty`), trigger nothing — even
          when the value already in place lacks the layer: that is how you add it
          (`"col": {"comment": …}`), without re-sending the value.
          Applies to sub-fields of objects and of list items too; never to
          `readonly` nor the lifecycle column; never to a column this
          write does not name. It arms ITSELF — no head setting needed. ⚠️ It does NOT
          make a comment TRUE: it forces you to NAME a source, which makes a lie
          checkable — the truth is still established on the documents.
          Every declared constraint applies (the format as a whole from the date
          above). A non-conforming write FAILS naming the culprit
          (max_length reports the actual length AND the bound; pattern reports the
          value it saw AND the motif).
          Only what a write SETS is judged: a row whose stored value is already
          off-format still accepts a write to its OTHER columns — the stale value
          is named back in `hors_type`, never a reason to refuse.
          Fields the caller does NOT write — one question ("whose column is this?"),
          and each refusal names the field, the reason and where the thing goes:
          `field.readonly: true` refuses a write that CHANGES a value already SET
          (layers stay open — what another source says goes in `<field>.comment`);
          a cell with NO value (key absent, `null` or `""`) can still be filled once,
          `@empty` counts as set, and clearing a set value (`null`) is a change;
          ⚠️ `field.origine: "system"` was REMOVED on 2026-09-08 and is now
          REFUSED like any unknown key. Nothing captures a previous value
          automatically any more: the origin is set by the call that BRINGS the
          data, with `donnees_d_origine=true` (`data_write` says how). Re-sending
          the SAME value is never a write, so re-emitting a record you just read
          always passes.
          Bound the fields meant to hold ONE short value (a job title, a city): a
          column that collects reasoning stops being groupable/filterable. The
          bound applies to the keys a write actually SETS, so rows already over it
          keep working until that field is rewritten — and setting a bound on a
          table that already overflows answers with a `warning` saying how many.
        - lifecycle: the column that carries the block IS the status column (no
          `role` tag needed), `lifecycle: {states:[…],
          transitions:{from:[to…]}, terminal?:[…]}` — unknown state or undeclared
          transition is refused. Each `transitions` value is a LIST, even for one
          destination: `{"a": ["b"]}`; `{"a": "b"}` is refused at declaration; `terminal` is a LIST too. ⚠️ It no longer releases the work-queue claim:
          writing a "final" state does NOT free the row (#317). Release is a gesture
          of the LOCK — data_release, or closing your run — never an inference from
          a business value.
        - step labels: `lifecycle.labels: {"a_qualifier": "To qualify", …}` — the
          name a screen shows for each state instead of its code. Presentation
          only: no write reads it, a row still carries the code. Each key must be a
          state of `states` (an unknown one is REFUSED and named), each value a
          non-empty string of at most 60 characters; a state without a label is
          fine (the screen derives one from the code).
        - work-queue ceiling: `lifecycle.max_claims: <int >= 1>` +
          `lifecycle.abandon_state: "<terminal state>"` — a row claimed that many
          times WITHOUT a successful write leaves the queue in that state, with a
          platform reason in `_abandon` (ending with what the last attempt hit; its
          run in `_abandon_run`). Both go together: a ceiling without an
          abandon state, or an abandon state that is not terminal, is REFUSED here.
          Counter (`_claims`) resets on the first successful write to the row.
          Undeclared, a platform default ceiling (3) still applies: the row is set
          aside with its reason in `_abandon`, its status left untouched.
          `lifecycle.claimable: {col: val | {op: val}}` (`filter` grammar) = the
          rows the queue SERVES: no claim hands out a row outside it, whatever
          `filter` says.
        - passes: `lifecycle.advance: {"societe": "dirigeant", "dirigeant": "email"}`
          — the counterpart of `abandon_state`. A claimed row RELEASED (data_release,
          run_finish) after at least one successful write since its claim moves to
          the next state, as a platform revision of that run. Agents claim by state
          and never write it; if a pass writes the state itself, its write wins. No
          write, no move (the ceiling applies). Each step must be a declared
          transition, never from a terminal state — refused here otherwise.

        SEMANTIC SEARCH (#67 V2.2 — opt-in per datastore): pass `semantic_search=true`
        to make this datastore's ROWS findable by MEANING via oto_search (not just exact
        words), embedding each row (has a per-row cost → off by default; enable it on the
        tables you actually search by concept). `false` turns it off and purges the
        embeddings. Passing ONLY `semantic_search` leaves the schema untouched.

        Args:
            datastore: target datastore (must exist; you must have write access).
            schema: the schema object, or null to clear it. Head key
                `new_rows: "create"|"reject"` (`unknown_columns` is refused: no
                setting any more); a field may carry `readonly: true` (a value once set is
                locked, an empty cell fills; layers open). ⚠️ `origine` was REMOVED on 2026-09-08 and is refused — the
                origin is set by the call that brings the data
                (`donnees_d_origine=true`), not by a schema format.
            semantic_search: true/false to toggle semantic row search; null = leave as is.
        """
        store = _acting_store()
        datastore = _ns(datastore)
        try:
            out: dict = {}
            # Schema set/cleared — unless the call targets ONLY the semantic toggle
            # (schema omitted + semantic given): the schema must not be cleared then.
            if schema is not None or semantic_search is None:
                out = store.set_schema(datastore, schema)
            if semantic_search is not None:
                out.update(store.set_semantic(datastore, semantic_search))
            return out or identite.de_releve(store.dernier_tableau, datastore)
        except DatastoreNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(datastore, e)))
        except DatastoreReadOnly:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=f"datastore `{datastore}` shared read-only"))
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))

    # `data_drop_column` (purging a dead column, #296) is NOT here: it is a
    # capability (`capabilities/datastore/columns.py`) — a platform verb is born
    # as a capability, ADR 0042 §Convergence of surfaces.

    @mcp.tool()
    @_avec_la_phrase_des_couches
    @ds_via.sous_via
    def data_write(datastore: Adresse, row: dict | None = None, id: str | None = None,
                   rows: list | None = None, key: str | None = None,
                   readonly_override: bool = False,
                   origine_override: bool = False,
                   donnees_d_origine: bool = False,
                   force: list | None = None,
                   expected_revision: str | None = None,
                   upsert: bool = False,
                   layers: str = dsl.DEFAUT,
                   versions: Optional[list[str]] = None,
                   empties: str = dsl.EMPTIES_DEFAUT,
                   via: str | None = None) -> dict:
        """Write one row, or a BATCH of rows in a single call.

        ⚠️ **Provenance goes in `comment`, never in `origine`.** Put WHAT you
        established and WHERE it came from in `<field>.comment`, and the page in
        `<field>.link`. `origine` is the platform's layer — an agent never writes
        it. Writing `<field>.origine` is REFUSED unless the call declares
        `origine_override=true` — which belongs to an import, not to a write of
        your own.

        <<couches>>
        What a write destroys, what `readonly` and the business key protect,
        and where the REST face differs: guide `datastore-semantics`
        (`oto_guide op=read slug=datastore-semantics`).

        ⚠️ **Writing a value DROPS the `comment` and `link` that came with it** —
        they described the OLD value. To keep one while the value changes, send it
        back as it is.

        Two gestures on a cell, and only two:
        `null` = ERASE the cell: `{"field": null}`. The erased value is kept
        nowhere: the response hands it back once, in `valeurs_effacees`.
        `"@empty"` = "searched, nothing found", its reason in `comment`:
        `{"valeur": "@empty", "comment": "registry and imprint: none"}`. It
        satisfies `required` (a plain `""` does not) and reads back `""` — or
        `"@empty"` with `empties="sentinel"`. On a layer (`comment`, `link`), it
        only empties that layer.

        ⚠️ **On a cell that holds a value: keep it and write the layers alone
        (`{"field": {"comment": "…"}}`), erase it with `null`, or discard it with
        `@empty` and the reason in `comment`.** A field you leave out is "not mine" —
        its value stays. A `comment` alone is a note beside the value, never
        "searched, nothing found".

        <<mots_deprecies>>

        `""` and `[]` are values: they REPLACE the value in place, like any other
        value, and the replaced value comes back in `valeurs_effacees`. To keep a
        value, leave the field out; to erase it, write `null`.

        <<upsert_implicite>>

        <<colonnes_non_declarees>>

        ⚠️ **`@empty` must be the ENTIRE sub-field, alone.** Mixed into a sentence it
        is just text and gets stored as such — `"@empty ; nothing on the imprint"`
        lands in the cell verbatim, and a client reads it in their deliverable. The
        reason goes in `comment`.

        In a list, the word goes on the element's field:
        `{"contacts": [{"nom": "Alice", "fonction": "@empty"}]}`. A list whose schema
        declares no `of.key` is REPLACED by what you send: read the row with
        `empties="sentinel"` and send each element back as it came, so an assumed
        empty stays `"@empty"`. Refused on an element's identity (`of.key`), in a list
        of plain values, and inside an object or a `json` column.

        ONE element of a list is written at its RANK — the address you read and
        filter by: `{"contacts[1].email": "d@x.fr"}` (or `{"valeur": …, "comment":
        …}`; `contacts[1].email.comment` alone annotates it, `null` erases it).
        `{"contacts[+]": {…a whole record…}}` appends, `{"contacts[0]": null}` removes.
        Ranks start at 0 and point at the list AS YOU READ IT; the append comes last.
        To add or remove WITHOUT reading the row first: `{"tags[+]": ["x", "y"]}`
        appends one element or a list of them, in order, duplicates kept;
        `{"tags[-]": ["x"]}` removes every occurrence of each value (plain values
        only — a record is removed at its rank; an absent value is refused). Both are
        applied under the row lock: two concurrent appends both land. On a `text`
        column, `{"journal[+]": "…"}` appends a line to the cell (a list = several
        lines) without resending it; a log kept by many is better as a table — past
        64 000 characters (unless the column declares `max_length`) an append is
        refused: move the log to a table, one row per entry.
        Refused, with the form that works: a rank that does not exist (append with
        `contacts[+]`), `contacts[0]: {…}` (write its fields), `contacts[].x`, and the
        whole column together with one of its ranks.

        A requirement declared on a sub-field (`required`, `options`, `max_length`…
        on an element's `nom`) is enforced like one on a column. In a list with
        `of.key`, only the elements your write CHANGES are judged: an element sent
        back unchanged never blocks yours — its defect comes back in `hors_type`.
        Written at its rank, only that element is judged, with or without `of.key`.

        Dates: a `date`/`datetime` column reads ISO in any variant, `04/09/2026`
        (day first) and Unix timestamps, and STORES one form — `2026-09-04T10:00:00Z`
        (UTC) for an instant, `2026-09-04` for a day; known only to the month or the
        year, it stays `2026-09` / `2026`. An instant without a time zone is taken as
        UTC, and `notices` says so: write its offset (`+02:00`) if it is local time.

        A schema refusal ends with the payload to send back: only the fields to fix,
        a `<…>` template in place of each value, `| @empty` where that is allowed —
        `{"contacts": [{"role": "RH", "nom": "<text> | @empty"}]}`. Fill the
        templates and write again; a list goes back WHOLE, that element fixed in
        its place (by its `of.key`, else its rank), the others as they were — or
        write only that element's fields at its rank (`contacts[1].nom`).

        ⚠️ **A write DESTROYS what is in the column.** On an open column there is no
        undo: the previous value leaves the row the moment yours lands. It survives
        only in the row's revision journal — `data_row_history` serves each write's
        before and after, kept 90 days by default — and nothing puts it back for you.
        If the value was supplied by the table's owner, announce what you are about to
        change on a column you did not fill yourself.

        ⚠️ **There is NO automatic safety net.** `origine: "system"` was REMOVED on
        2026-09-08. It captured the previous value lazily, on the first write that
        changed it — which required having been declared BEFORE the row existed.
        Declared after the fact it caught nothing: 837 cells out of 846 lost on one
        campaign table, the owner's values already overwritten by agents.

        What replaces it is a DECLARED gesture, carried by the call that brings the
        data in: `donnees_d_origine=true`. It sets the FIRST version of the data (the
        origin) and marks the write as an import in the journal — the current value
        and the origin both go in at the moment the value enters, so there is
        no "before" and no "after" to get wrong. Three rules: an origin ALREADY set
        is never touched (a re-import updates the current value, never the origin) ;
        an EMPTY column receives nothing (supplying nothing is not supplying blank) ;
        the call's layers go into BOTH versions.

        So on a column whose origin was never captured, overwriting is FINAL in the
        row: only `data_row_history` still shows the previous value. The 28 799 `origine` layers already in the
        base are untouched — they are still read, served and protected; it is the
        mechanism that went, not the data.

        Re-writing the SAME value changes nothing and captures nothing, by design.
        And it does not depend on how the row was created: a row appended through
        the MCP tool and one created through the REST face behave identically
        (measured 2026-09-04, both faces call the same store).

        SINGLE (`row`): WITHOUT `id` = ADD a NEW row (new JSON keys auto-create
        columns, unless the table is CLOSED — see below); if the table declares a
        business `key` and your row carries a value that already exists, the
        business-key rule above applies. WITH `key=<the declared key>` = DESIGNATE the
        row by its key value: an existing value modifies that row and returns its
        `_id`, a new one creates it. WITH `id` = PARTIAL update of that row (only
        provided fields change). Returns the row
        (with `_id`/`_created_at`/`_updated_at`/`_revision`).

        `expected_revision` (with `id` only) — Only when what you write was COMPUTED
        from a row you read (a list or text you re-send whole, a status chosen from the
        current one): pass that read's `_revision`. If the row changed since — any
        column, or its reservation — nothing is written and the call is refused with
        `revision_conflict` and the current revision: read again, recompute, write
        again. Omit it otherwise: writes to different columns never overwrite each other.

        On a row you CLAIMED, address it by the `_id` of the row data_claim_next
        returned.

        BATCH (`rows` = list of dicts): write them all at once — for importing a
        dataset without round-tripping each row through your context. If a business
        KEY is in effect (the `key` arg, else the datastore's declared `schema.key`):
        with `key=` the batch DESIGNATES — a row whose key value exists modifies that
        row, a new value creates one; without `key=` it ADDS, and the business-key
        rule above applies to values already in the table. Two rows of the batch
        with the same key value follow that rule either way; rows without a key are
        appended. Returns a summary
        {inserted, updated, count, key, ids, fusions?} — `ids` holds one `_id` per row
        sent, rank for rank. Use `data_set_schema` to
        declare a persistent `key`. For a FILE, never retype its rows here: `oto_import`
        loads it from where it is (link, Drive, project file), `oto_upload_url` takes
        it from your disk.

        ⚠️ An error on the way BACK (expired session, dropped connection, timeout)
        does NOT mean the write failed: it may have committed before the error.
        Read the row back (`data_rows`, by `id` or filtered on its key) before
        re-sending.
        Re-sending is safe with `id`, or with `key=` (or `upsert=true`) on a table
        with a business `key`: the row is designated, or merged; a keyless append
        re-sent creates a DUPLICATE.

        ⚠️ A table can be CLOSED by its schema (`new_rows: "reject"`, next to its
        business `key`) — `data_get_schema` says whether it is. On such a table there
        is NO append at all: a write designating no existing row (no `id`, and no key
        value the table already carries) is REFUSED, single row and batch alike, and
        nothing is created — including a key value that is simply NEW. That is a
        deliberate setting of that table, not a platform rule. To make a row EXIST
        there, it is a schema move and not a write:
        `data_patch_schema(datastore=…, new_rows="create")`, your write, then
        `data_patch_schema(datastore=…, new_rows="reject")` to close it back.

        `origine_override=true` belongs to an IMPORT, not to a write of your own:
        it declares that this call knowingly sets the `origine` layer — refused
        without it. ⚠️ For a real import, prefer
        `donnees_d_origine=true`, which sets the first version (origin) in one gesture; the
        override only says "I know what I am doing on this layer".

        ⚠️ A COLUMN can be LOCKED by the schema (`readonly: true`) — it holds a
        value someone put there, and an ordinary write that CHANGES it is refused by
        name (writing the same value again is fine, and `<column>.comment` always
        stays open for what another source says). The lock holds what is SET, not
        what is missing: a cell with no value (key absent, `null` or `""`) can be filled
        once; `@empty` ("searched, nothing") IS a value, and clearing one (`null`)
        is a change. To REPLACE it anyway, pass
        `readonly_override=true` ON THIS CALL. It is open to the OWNER of the table
        (you, your org or your team) or to whoever GOVERNS it — a table merely SHARED
        with you in write is refused, by design. It applies to this one call and
        nothing else: there is no schema setting to reopen and therefore none to
        close back. Every forced replacement is written to the call journal (row,
        column, replaced value) next to who called. ⚠️ That journal entry is
        BOUNDED: at most 25 replacements per call, each value cut at 120
        characters (`…`, with `was_len`/`now_len` giving the original length). When
        it is cut short it says so (`readonly_forced_bilan`: "25 of N") — the count
        you read there is then a floor. The full history, whole values, is the row
        history: `data_row_history`.

        ⚠️ The datastore must EXIST first (create it with `data_create_datastore`);
        writing to an unknown datastore raises "datastore inconnu" — it is NOT
        auto-created.

        ⚠️ **Address the table by its NUMBER.** The reply carries `ns_id` — the
        table's number — and that is the form to pass as `datastore`:
        `data_write(datastore=174, id=…)`. A name still resolves until 2026-11-08,
        then it is REFUSED.

        `datastore` also accepts `slot:<name>` = the table BOUND under that slot
        name by the ACTIVE project (procedures reference tables as <slot:name>;
        the project maps the name via its links). Requires an active project +
        the binding — otherwise an actionable error, never a fallback.

        Args:
            datastore: the table's NUMBER (`ns_id`, e.g. 174) — the form to use.
                Its name still resolves until 2026-11-08, then is refused.
                `slot:<name>` also works. It must already exist.
            row: single-row content as a dict (JSON-encoded automatically).
            id: omit = append a new row ; provided = partial update of that `_id`
                (the one data_write / data_claim_next returned for that row).
            expected_revision: with `id` only — the `_revision` of that row as you read
                it, when what you write was computed from that read (see above).
            force: force the NAMED columns on this call instead of everything it
                carries — `["raison_sociale", "raison_sociale.origine"]`. Naming
                them is enough; no need for `readonly_override` as well. ⚠️ It
                changes the SCOPE, not the right: forcing stays reserved to the
                table's owner or whoever governs it. A locked column absent from
                the list is refused normally, and the refusal says so.
            donnees_d_origine: sets the FIRST version of the data (the origin) and
                marks the write as an import in the journal: this call brings data
                AS THE CLIENT HANDED IT OVER. Each cell gets its `origine` version frozen at
                the same time as its current value, carrying the same layers, so
                `comment` says where the data came from. Use it for the import
                itself, NOT for enrichment: an agent's findings are the current
                version. An origin already set is never overwritten (a re-import
                updates the current version and leaves the origin alone), and an
                empty cell gets nothing — the client handed over nothing there,
                which is not the same as handing over an empty value.
            rows: BATCH mode — a list of row dicts written in one call.
            key: DESIGNATES rows by this business key column: an existing value
                modifies its row, a new one creates it — no `upsert` needed. BATCH
                (with `rows=[…]`): any column (default `schema.key`, but then the
                batch ADDS). Alongside a single `row`, accepted only when it names the
                table's DECLARED business key, `row` carries its value and no `id`
                is given; refused otherwise — wrap the row in `rows=[…]` to designate
                by another column.
            upsert: without `id` only. <<upsert>>
            readonly_override: `true` = overwrite the `readonly` columns THIS CALL
                writes, instead of being refused. Owner or governor of the table
                only ; valid for this call alone ; journaled.
            origine_override: IMPORT path only — declares that this call sets the
                `origine` layer knowingly (without it, such a write is refused).
                ⚠️ `origine: "system"` was removed on 2026-09-08:
                there is no longer a "formatted column". For a real import, prefer
                `donnees_d_origine`. This call only.
            layers: shape of the ROW this write returns (`flat` default, `nested`) —
                same as `data_rows`. Single row only.
            versions: which versions of each cell the returned row carries. Default:
                the current value only; the origin (the FIRST version of the data)
                is asked for with `versions=["current","origine"]`. Single row only;
                the reply states what it served in `versions_servies`.
            empties: how the returned row serves an assumed empty (`plain` default,
                `sentinel`) — same as `data_rows`. Single row only.
            via: <<via>>
        """
        store = _acting_store()
        try:
            # Checked and resolved HERE, before anything else, so that any refusal
            # comes out through the same actionable path as the others (ValueError →
            # INVALID_PARAMS). It is also what gives the agent that still writes
            # `@claimed` — removed on 07/09/2026 — a refusal that NAMES the gesture
            # that succeeds, rather than the storage's "unknown datastore".
            datastore, id = _adresse(datastore, id)
            # Refusals that NAME the parameter and its shape, while the caller can
            # still correct — never a bare `invalid_input`.
            cibles = fcg.chemins_forces(force)
            layers = dsl.check(layers)
            vers = dsver.check(versions)
            empties = dsl.check_empties(empties)
            # A batch returns no row: a requested row shape would be meaningless
            # there — refused, not ignored.
            if rows is not None and (versions is not None or layers != dsl.DEFAUT
                                    or empties != dsl.EMPTIES_DEFAUT):
                raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                    "`layers`/`versions` set the shape of the returned ROW: a batch "
                    "(`rows=`) returns none. Remove them, or write a single row (`row=`).")))
            jetons.verifier_contenu(row)
            jetons.verifier_contenu(rows)
            # ⚠️ **`key` only makes sense on a batch** — it names the dedup column of
            # `write_rows`. On a single-row write it was passed to nothing: neither to
            # `append_row` nor to `update_row`. **Silently ignored.**
            #
            # Measured on 09/09/2026, and this is what it costs: a campaign wrote ten
            # times `data_write(datastore=…, key="@claimed", row={…})` believing it was
            # targeting the row it held. All ten calls returned 200 and created ten
            # new rows with no business key; the three reserved rows ended in failure
            # without ever having been written. **172,500 tokens for a parameter that
            # did nothing.** Here, an offered parameter WILL be honored — that is the
            # rule, not the accident.
            #
            # We target the AXIS, not the value: any `key` that is inoperative on this
            # path is refused. Closing only `@claimed` would fix one case and leave
            # the whole class — the next agent would write `key="_id"` or `key="siren"`
            # and go off for ten silent writes.
            #
            # One exception, and only one: `key` naming the DECLARED business key,
            # with its value in `row` and without `id=`: the write then DESIGNATES the
            # row by its key (oto#141, passed to the store), without `upsert` (signals
            # 986, 1125, 1135, 1154 — the "upsert on the key" idiom is written this way).
            if key is not None and rows is None:
                declaree = (store.get_schema(datastore) or {}).get("key")
                if not jetons.key_unitaire_redondant(key, declaree, row, id):
                    raise McpError(ErrorData(
                        code=INVALID_PARAMS,
                        message=jetons.refus_de_key_sans_lot(key, declaree)))
            # SAME axis as `key` just above: a precondition with no designated row
            # compares nothing. Offered, it will be honored; ignored, the write would
            # go through without the protection the caller thinks they asked for.
            if expected_revision is not None and (id is None or rows is not None):
                raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                    "`expected_revision` only applies with `id=` — the row you read: "
                    "without it there is nothing to compare, and nothing is written.")))
            # oto#141, SAME axis: `id=` already targets its row, there is nothing to merge.
            if upsert and id is not None:
                raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                    "`upsert=true` only applies WITHOUT `id=`: with `id=`, the write "
                    "already targets its row and merges nothing. Nothing is written — "
                    "drop one of the two.")))
            if rows is not None:
                if row is not None or id is not None:
                    raise McpError(ErrorData(code=INVALID_PARAMS,
                                             message="pass `rows` (batch) OR `row`/`id`, not both"))
                if not isinstance(rows, list):
                    raise McpError(ErrorData(code=INVALID_PARAMS, message="rows must be a list of dicts"))
                recap = store.write_rows(datastore, rows, key=key,
                                         readonly_override=readonly_override,
                                         origine_override=origine_override,
                                         donnees_d_origine=donnees_d_origine,
                                         force=cibles, upsert=upsert)
                # The batch has an ENVELOPE (its body is not a row): it carries
                # the full identity — the CANONICAL name, plus the echo of the string
                # received, and the number to use next.
                out = {**identite.de_releve(store.dernier_tableau, datastore),
                       **recap}
            else:
                if row is None:
                    raise McpError(ErrorData(code=INVALID_PARAMS,
                                             message="provide `row` (object) or `rows` (list of objects, batch mode)"))
                if not isinstance(row, dict):
                    raise McpError(ErrorData(code=INVALID_PARAMS, message="row must be a dict"))
                out = store.append_row(datastore, row,
                                       readonly_override=readonly_override,
                                       origine_override=origine_override,
                                       donnees_d_origine=donnees_d_origine,
                                       force=cibles, upsert=upsert,
                                       # oto#141: a named `key=` = DESIGNATION.
                                       key=key, layers=layers, versions=vers,
                                       **dsl.relayer_empties(empties)) \
                    if id is None \
                    else store.update_row(datastore, id, row,
                                          readonly_override=readonly_override,
                                          origine_override=origine_override,
                                          donnees_d_origine=donnees_d_origine,
                                          force=cibles,
                                          # `RevisionConflict` is a `ValueError`:
                                          # INVALID_PARAMS, the text of the REST refusal.
                                          expected_revision=expected_revision,
                                          layers=layers, versions=vers,
                                          **dsl.relayer_empties(empties))
            # Fields set outside the declared format (#294): the write is accepted (a
            # free field remains a right of the contract), but it is no longer silent.
            # The table NUMBER goes with it (`ns_id`): the write is the gesture the
            # agent repeats, and it must be able to re-read the address to use. The
            # name, however, is NOT added here — the body of a single-row write
            # IS the row, and `datastore` would collide with a column there.
            out = {**out, **store.off_schema_report(),
                   **identite.numero(store.dernier_tableau)}
            if rows is None:
                out["versions_servies"] = list(vers)
            hint = _project_hint(datastore)
            return {**out, "project_hint": hint} if hint else out
        except (RowValidationError, BusinessKeyRequired) as e:
            # oto#135: the MCP face has no structured envelope — the payload to
            # resend (`details.a_renvoyer`, rendered as-is by REST) ends the
            # message. BEFORE `ValueError`, from which both derive. oto#151: the
            # business-key refusal carries it too.
            raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                str(e) + charge_a_renvoyer.clause(e.details))))
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        except DatastoreNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(datastore, e)))
        except DatastoreReadOnly:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"datastore `{datastore}` shared read-only"))
        except RowNotFound:
            # #517: this refusal arrives at the ONLY moment the agent can still correct.
            # A bare "Not found" lets it retry WITHOUT an identifier — and a write
            # without an identifier CREATES a row instead of correcting one. So we
            # hand back the two missing things: what an identifier looks like,
            # and what its own work already holds.
            try:
                piste = store.claimed_hint(datastore)
            # noqa: SILENT — a lead is a bonus: failing to compute it must never replace an actionable refusal with an internal error
            except Exception:  # noqa: BLE001
                piste = None
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_introuvable(id, piste)))
        except RowLocked as e:
            # #317: a refusal, not a 500. Without this translation the agent sees "Internal
            # server error" where it needs WHO holds the row, UNTIL WHEN,
            # and HOW to release — lived in production on 15/08, on a blocked campaign.
            # The exception's message already carries all three; `_row_locked_message`
            # adds the CAUSE when it is proven (`_run_id` omitted, #547).
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_row_locked_message(e)))

    @mcp.tool()
    def data_claim_next(datastore: Adresse, worker: str, filter: Optional[dict] = None,
                        lease_s: int = 900, max_claims: Optional[int] = None,
                        # ⚠️ `dsl.DEFAUT`, never a literal. `layers.py` promises that
                        # "the default is read here and nowhere else, so that a
                        # switch is a single gesture" — this surface hardcoded it
                        # and silently broke the promise. It is the read an
                        # agent repeats the MOST: the switch would have left it behind,
                        # and the default would have diverged where it is least visible.
                        layers: str = dsl.DEFAUT,
                        filters: Optional[list] = None,
                        empties: str = dsl.EMPTIES_DEFAUT) -> dict:
        """Atomically claim the NEXT unprocessed row of a datastore (work queue).

        Claim with `empties="sentinel"` when you will re-send a list: an assumed
        empty (written `@empty`, its reason in `comment`) then comes back
        `"@empty"` and, sent back as is, stays one; sent back as `""` it becomes an
        ordinary empty, refused on a required field. Never copy `@empty` into a
        deliverable.

        ⚠️ **Your `filter` MUST name a column your processing WRITES.** That is the
        one condition which makes the queue EMPTY, and nothing enforces it. The
        order: rows never served first, then the least recently served — a
        released row goes behind every fresh row, but it comes back once they are
        served, for as long as it still matches your filter.

        ⚠️ **How to see a queue running empty**: the row carries `_claims`. Above 1
        it means « you have already been served this row and it was not written »
        — that is the signature. Stop and re-read your filter;
        claiming again will not help.

        ⚠️ **A claim lives in a run.** Call `run_start` first and pass its
        `run_id` as `_run_id=` on this call: without an active run the claim is
        REFUSED and nothing is reserved — a row held outside a run could not be
        written, so you would research it for nothing.

        Write your result and release it by the `_id` of the returned row.

        ⚠️ **Address the table by its NUMBER, not its name**: the reply carries
        `ns_id` (e.g. `174`), the form to pass as `datastore` in every following
        call — `data_write(datastore=174, id=…)`, `data_release(datastore=174, …)`.

        The primitive for draining a table with N parallel (sub-)agents without
        collisions: picks, among rows whose claim lease is free or expired, the one
        served least recently (never-served rows first, then creation order),
        stamps `_claimed_by`/`_claimed_until` and returns it — two concurrent
        workers never get the same row. Returns `{row: null}` when nothing is
        left to claim.

        Filter on column A, write into column B, and the queue never empties:
        once the fresh rows are served, your own finished rows come back, round
        after round. Measured on a 3 766-row table, when the order was still
        « oldest first »: three workers filtered on a column the processing never
        touched. **834 fresh rows were never reached**, and one worker claimed the
        SAME row seven times. Every call succeeded and no error was raised — a
        livelock, not a failure, and you cannot see it from inside. Rows claimed
        again and again without a write end up set aside (the ceiling below) —
        LOST to the pass, without being at fault.

        A name still resolves as `datastore` until 2026-11-08 — then it is refused —
        but the number is what to carry: it survives a rename, it is unique where
        a name is only unique per owner, and it is what the platform records.
        `datastore` in the reply is the table's REAL name whatever form you passed
        in, so reading `"600"` back from a claim on `600` no longer happens.

        `worker` is a label YOU choose and REUSE verbatim on data_release — the
        guard so one agent cannot release another's claim.
        `filter` (exact {col: val}, e.g. {"status": "nouveau"}) selects what counts
        as claimable; it narrows the table's declared `lifecycle.claimable`, never
        widens it. The claim does NOT change the row: write your progress via
        data_write (id=…), then release it — writing a "final" status does not
        free it (#317). Release the row with `data_release` if you have it;
        otherwise finishing your run (`run_finish`) releases it. Never write your
        intent into the row (no `_action`/`_liberation` columns). The lease
        (`lease_s`, default 900s) only covers a worker that died. While you hold a
        row, nobody else can write it.

        Multi-pass table (`lifecycle.advance` declared): claim by the state of YOUR
        pass (`filter={"statut": "societe"}`), write your findings, release — the
        platform moves the row to the next state. Do not write that state yourself.

        The row carries `_claims` = how many times it has been claimed since the
        last successful write. A row claimed over and over WITHOUT a write is a
        queue running empty: past the ceiling — `lifecycle.max_claims` declared on
        the table, else a platform default of 3; `max_claims` here can only RAISE
        it for this pass — the server sets it aside: it stamps `_abandon` with the
        reason, ending with what the LAST attempt hit (the job's error, a refused
        write, the tools in error, or no write attempted) and `_abandon_run` with
        that attempt's run, moves it to `lifecycle.abandon_state` when the table declares a
        ceiling (with the default one its status is left untouched), and STOPS
        serving it — whatever your filter says. It stays readable and repairable:
        an explicit data_write puts it back in the queue and resets the counter.

        ⚠️ `layers="nested"` gives the row back in the SHAPE YOU WRITE — a cell
        that carries layers comes as `{"valeur": …, "comment": …}` instead of the
        flat pair `champ` + `champ.comment`. ⚠️ It stays a READ: do NOT send this
        row back. Write only the fields you established — and never
        `<field>.origine`, which is read here and set by the platform, never by an
        agent. The flat form shows a key with a DOT, and a dot does not look
        like a field name: agents turn `effectif.comment` into `effectif_comment`,
        which creates a ghost column — or, on a table that refuses unknown columns,
        loses the whole row. This is the only read that feeds a WRITE loop, so it is
        the one where the shape matters.

        Every column DECLARED in the schema is on the row, `null` when no value is in
        place — `null` means "nothing here yet": find it if your task needs it, never
        make it up. Sending such a `null` back changes nothing (a `null` only erases a
        value that is in place); still, write only the fields you established.

        `filters` is the LIST form — `[{field, op, value}]`, ANDed with `filter`.
        Use it when one column needs TWO bounds: `filter` accepts a single
        operator per column, so a range (`score >= 10 AND score <= 20`) can only
        be expressed here. Same grammar as `data_rows`.

        `datastore` also accepts `slot:<name>` (table bound by the active project).

        Args:
            datastore: the table's NUMBER (`ns_id`, e.g. 174) — the form to use.
                Its name still resolves until 2026-11-08, then is refused.
                `slot:<name>` also works. It must already exist.
            layers: shape of a cell that carries layers. `flat` (default): each
                filled layer beside the value (`email.comment`). `nested`:
                `{"valeur": …, "comment": …}`, the shape you write. Any other value
                is refused.
            empties: how an assumed empty (written `@empty`) comes back. `plain`
                (default): `""`, like any empty cell. `sentinel`: `"@empty"`, the
                word that writes it. Any other value is refused.
        """
        if not _current_run():
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_REFUS_SANS_RUN))
        store = _acting_store()
        datastore = _ns(datastore)
        warnings: list = []
        perimetre: dict = {}
        try:
            row = store.claim_next(datastore, worker=worker, filter=filter,
                                   lease_s=lease_s, max_claims=max_claims,
                                   warnings=warnings, perimetre=perimetre,
                                   layers=dsl.check(layers), filters=filters,
                                   **dsl.relayer_empties(dsl.check_empties(empties)))
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        except DatastoreNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(datastore, e)))
        except DatastoreReadOnly:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=f"datastore `{datastore}` shared read-only"))
        # The table's IDENTITY, not an echo of the address received (see `datastore/
        # identite.py`): `datastore` is its canonical name and `ns_id` its NUMBER.
        # This is THE handover where the number matters — the agents' loop starts here,
        # and what they read back in a response is what they reuse afterwards.
        return {**identite.de_releve(store.dernier_tableau, datastore), "row": row,
                **({"warning": warnings[0]} if warnings else {}),
                **({} if row else {"hint": _hint_file_vide(perimetre, filter)})}

    @mcp.tool()
    def data_release(datastore: Adresse, id: str, worker: str) -> dict:
        """Release a claimed row — the NORMAL end of processing one row, and the
        counterpart of data_claim_next. Guarded by `worker` (same label as at claim
        time), and addressed by the `_id` that data_claim_next returned.

        ⚠️ Call it after EVERY row you finish, not only when abandoning: writing a
        "final" status no longer frees the row (#317). If you wrap your work in
        run_start / run_finish, closing the run frees everything it held — that is
        the safety net when you forget.

        On a table that declares `lifecycle.advance`, releasing a row you WROTE since
        claiming it moves it to the next state (`advanced: {field, from, to}` in the
        reply); a row you did not write stays where it is.

        `datastore` = the table's NUMBER (`ns_id`, the one data_claim_next handed
        you) — the form to use. Its name still resolves until 2026-11-08, then is
        refused. `slot:<name>` also works."""
        store = _acting_store()
        try:
            datastore, id = _adresse(datastore, id)
            issue = store.release_claim(datastore, id, worker=worker)
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        except DatastoreNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(datastore, e)))
        except DatastoreReadOnly:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=f"datastore `{datastore}` shared read-only"))
        # ⚠️ TWO opposite situations shared this `false` and this hint (#517):
        # "there was nothing to release" (benign) and "the row belongs to another job"
        # (failure). A fleet wired its stop limit to it and cut itself off at five
        # records out of a hundred, on 29/08. The response therefore carries the REASON —
        # closed vocabulary, machine-readable — and the hint says WHICH of the two.
        return {**identite.de_releve(store.dernier_tableau, datastore),
                "id": id, "released": issue["released"],
                "reason": issue["reason"],
                **({} if issue["released"] else {"hint": indice_de_liberation(issue)}),
                **({"advanced": issue["advanced"]} if issue.get("advanced") else {})}

    @mcp.tool()
    @_avec_la_recherche
    def data_rows(
        datastore: Adresse, id: str | None = None,
        filter: Optional[dict] = None, limit: int = 100,
        cursor: str | None = None, fields: Optional[list[str]] = None,
        count_only: bool = False, q: str | None = None,
        q_scope: recherche.PorteeRecherche | None = None,
        order_by: str | None = None, order_dir: str = "desc",
        filters: Optional[list[dict]] = None, layers: str = dsl.DEFAUT,
        # ⚠️ `dsver.DEFAUT`, never a literal — same promise as `layers.DEFAUT`:
        # the default is read in one place only so that a switch is a single gesture.
        versions: Optional[list[str]] = None,
        empties: str = dsl.EMPTIES_DEFAUT,
    ) -> dict:
        """Read rows. WITH `id` = the single row (by `_id`). WITHOUT `id` = one PAGE
        of rows (`filter`/`q` narrow it, `order_by` sorts it) with a stable cursor.

        Read with `empties="sentinel"` before re-sending a list: an assumed empty
        (written `@empty`, its reason in `comment`) then comes back `"@empty"` — an
        ordinary `""` stays `""` — and, sent back as is, stays one; sent back as `""`
        it becomes an ordinary empty, refused on a required field.

        ⚠️ `datastore` = the table's NUMBER (`ns_id`, e.g. 174) — the form to use;
        the reply carries it back. A name still resolves until 2026-11-08, then
        is refused. `slot:<name>` also works.

        ⚠️ List mode returns `{rows, count, next_cursor, ns_id}`. When `next_cursor`
        is not null there are MORE rows: call again with `cursor=<next_cursor>`
        (same datastore/filter/order) — repeat until `next_cursor` is null. A page
        is not the table.

        `versions` picks which VERSIONS of each cell you get. By default, the current
        value only; the origin (the FIRST version of the data) is asked for with
        `versions=["current","origine"]`: `current` is what we established,
        `origine` what the client handed over.
        ⚠️ Ask for BOTH in ONE call when you compare them — two calls are not atomic,
        and an write in between would make you compare the before of one state with
        the after of another.

        Layers come back FLAT by default (`champ.origine` beside the bare name);
        `layers="nested"` returns the shape you write — guide `datastore-semantics`.
        The bare name ALWAYS carries the current version; `versions` only decides
        what is added beside it — at any depth: without `origine`, a list item
        carries no `item["email.origine"]` either. The reply states what it served in
        `versions_servies`, so "I did not ask for it" never looks like "this cell
        has none".
        The REST face `GET …/rows` pages by `offset` with a `total`, no cursor.

        Without `order_by` the cursor is keyset-stable (rows created meanwhile don't
        shift the paging). With `order_by` it pages by offset instead, since an
        arbitrary sort has no stable keyset — so a row inserted mid-walk can shift the
        remaining pages. `q` without `order_by` ranks the rows (best match first), so
        it pages by offset too. Keep the SAME `order_by` and `q` across a walk:
        passing a cursor from one regime into the other is rejected rather than
        silently mispaged.

        Use `count_only=True` to get just the TOTAL number of (optionally filtered)
        rows — computed server-side, no rows returned — when you only need the count
        (e.g. how many leads match a filter) without pulling the data into context.

        Use `fields` to PROJECT a subset of columns when the full row is heavy and you
        only need a few (e.g. name + email + score over a large vivier): each row is
        trimmed to those columns (plus `_id`, always kept so you can still update the
        row), drastically shrinking the payload. Bump `limit` when projecting — narrow
        rows let you pull far more per page.

        Every column DECLARED in the schema is on each row, `null` when no value is in
        place ("nothing here yet", not "no such column") — including in a projection.

        Args:
            datastore: target datastore, or `slot:<name>` = the table bound under
                that slot name by the ACTIVE project (actionable error if unbound).
            id: `_id` of one row ; omit = list rows.
            filter: dict `{column: value}` — exact match. A column may instead take
                ONE operator: `{"posted_at": {"gte": "2026-06-01"}}`,
                `{"author": {"contains": "sylvie"}}`, `{"status": {"ne": "processed"}}`,
                `{"idcc": {"in": ["573", "86"]}}`, `{"email": {"not_empty": true}}`.
                Ops: eq, ne, contains, in, gt, gte, lt, lte, empty, not_empty.
                The system columns are filterable too — `_updated_at`/`_created_at`
                (ops eq/ne/gt/gte/lt/lte; a plain `YYYY-MM-DD` means that WHOLE day,
                so `{"_updated_at": {"gte": "2026-08-01"}}` = touched since the 1st)
                and `_id`. Filtering happens in SQL — never pull the whole table to
                filter it yourself. (list mode only)
            filters: list of clauses, for what `filter` cannot express — ONE clause
                may target SEVERAL columns at once. A single notion often lives on
                numbered columns (`contact1_fonction`, `contact2_fonction`…): ask
                about all of them in one go by NAMING them.
                `[{"fields": ["contact1_fonction", "contact2_fonction",
                "contact3_fonction"], "op": "in", "value": ["DRH", "DAF"]}]`
                = rows where ANY of those three holds an HR/finance role, whichever
                rank carries it. `match` picks the sense: `any` (default, one column
                is enough) or `all` (every listed column) — `all` + `empty` is how you
                get "rows with NO contact at all", which is NOT the negation of the
                first. A clause may also name a single column (`{"field": …}`), and
                a `champ.origine`/`.comment`/`.link` suffix targets that layer.
                Clauses combine with AND, and with `filter`. (list mode only)
            q: <<recherche_q>> The way to find a row when you don't know WHICH
                column holds the word. Combines with `filter` (AND). (list mode only)
            q_scope: <<recherche_q_scope>>
            limit: page size (default 100, list mode only).
            cursor: opaque `next_cursor` from a previous call = fetch the NEXT page.
            fields: list of column names to keep (projection) — the returned rows
                carry only these plus `_id`; a declared one with no value comes back
                `null`. Omit = full rows.
            count_only: return only `{total}` (filtered row count), no rows.
            order_by: sort column — a user field, or a system one (`_created_at`,
                `_updated_at`, `_id`). Omit = creation order. Sorting in SQL is how
                you get "the 10 most recent" or "the top scores" without pulling the
                table and sorting it yourself. With `q` and no `order_by`, best
                matches come first. (list mode only)
                Sorting honors the DECLARED type of the column: a `number` sorts
                numerically (never "10 < 2"), an `enum` sorts in its declared
                option order, a `date` chronologically. Values that don't fit the
                type (junk in a number column, a value outside the enum's options)
                go to the TAIL in both directions, alphabetically; empty cells go
                last of all. When that happens the response carries
                `order_health: {off_type, empty}` — counts over the whole filtered
                set, absent when everything conforms.
            order_dir: `desc` (default) or `asc`. Meaningful with `order_by`; with
                `q` alone, it orders rows of EQUAL rank by creation.
            layers: shape of a cell that carries layers (`origine`/`comment`/`link`).
                ⚠️ This is a READ: do NOT send the row back. Write only the fields
                you established — and never `<field>.origine`, which is read here
                and set by the platform, never by an agent.
                You WRITE nested (`{"valeur": …, "comment": …}`) and, by default,
                read back FLAT — this parameter lifts that asymmetry. `flat`
                (default): `row["email"]` is the value, and each filled layer sits
                BESIDE it as `row["email.origine"]`. `nested`: `row["email"]` is
                `{"valeur": …, "origine": …, "comment": …, "link": …}` — `valeur`
                always, the other keys only when filled — i.e. the shape you WRITE
                with `data_write`. A cell without layers is the same plain value in
                both shapes. Any other value is refused. With `nested`, `fields`
                names columns (a nested cell keeps its layers); `email.origine` as
                a field name only exists in `flat`. The default WILL switch to
                `nested`, with dated notice: pass `layers` explicitly if you depend
                on one shape.
            empties: how an assumed empty (written `@empty`) comes back.
                `plain` (default): `""`, like any empty cell. `sentinel`:
                `"@empty"`, the word that writes it — top level, nested
                (`{"valeur": "@empty", …}`) and inside list elements alike. It is not
                a value: never copy it into a deliverable. Any other value is refused.
        """
        store = _acting_store()
        datastore, id = _adresse(datastore, id)
        try:
            jetons.verifier_champs(fields=fields, filter=filter, filters=filters)
            layers = dsl.check(layers)
            empties = dsl.check_empties(empties)
            # Refusal that NAMES the parameter, the value received and what is allowed — it
            # goes out as INVALID_PARAMS like the other address refusals, at the only
            # moment the caller can still correct.
            vers = dsver.check(versions)
            if count_only:
                total = store.count_rows(datastore, filter=filter, q=q,
                                         q_scope=q_scope, filters=filters)
                return {"total": total, **identite.numero(store.dernier_tableau)}
            if id is not None:
                row = store.get_row(datastore, id, layers=layers,
                                    versions=vers, **dsl.relayer_empties(empties))
                # ⚠️ The NUMBER is NOT added here, and that is deliberate: this handover
                # has no envelope, its body IS the row — and it is exactly
                # the object the platform invites you to re-read and then republish as is
                # (promotion of `_id`, #354/#390). A response key placed inside it
                # would come back on write and create a phantom column there, or
                # make the row get lost on a table that refuses unknown fields. The page
                # below, on the other hand, has an envelope: the number fits there safely.
                return _project_row(row, fields) if fields else row
            page = store.cursor_rows(datastore, filter=filter, limit=limit,
                                     cursor=cursor, q=q, q_scope=q_scope,
                                     filters=filters,
                                     order_by=order_by, order_dir=order_dir,
                                     layers=layers, versions=vers, fields=fields,
                                     **dsl.relayer_empties(empties))
            # Projected from `cursor_rows` onward (oto-backend#980, batch 2): `_row_to_dict` no
            # longer builds the layers of unrequested columns, rather than
            # building then discarding them here. `page["rows"]` already comes out in the right shape.
            rows = page["rows"]
            out = {"rows": rows, "count": len(rows),
                   "next_cursor": page["next_cursor"],
                   # The response DECLARES what it serves: "not requested" and "absent
                   # from this cell" stop looking alike.
                   # ⚠️ From `vers`, the value THIS face just validated — not
                   # from the store's response. Relaying it would make the face depend
                   # on a key it already knows, and two sources for the same fact
                   # always end up diverging.
                   "versions_servies": list(vers),
                   **identite.numero(store.dernier_tableau)}
            # Typed sort (#336): the gap (values outside type/options, empty cells —
            # put at the tail) is STATED, otherwise the sort looks deliberate and lies.
            if page.get("order_health"):
                out["order_health"] = page["order_health"]
            # Projection on columns absent from ALL rows = same silent trap
            # as the filter (#163): we flag it without blocking.
            # ⚠️ The SCHEMA first, the sample only as a fallback: a column
            # declared but filled in on 12 rows out of 500 is absent from a page
            # where none of the 12 appears (in a stored JSONB row, an empty column
            # does not exist; the served row fills it in with `null` since oto#182, but
            # only for the DECLARED ones — the rest of this reasoning holds). Announcing it as "unknown — check the spelling" does
            # not just miss its target, it POINTS TO A FALSE CAUSE: the caller
            # re-reads their call, which is correct, and concludes the field does not exist.
            if fields and page["rows"]:
                present = {k for r in page["rows"] for k in r}
                declared = dsv2.top_level_keys(store.get_schema(datastore))
                unknown = [f for f in fields
                           if f != TOUT and f not in present and f not in declared]
                # Last resort BEFORE accusing: a column may be neither
                # declared nor on this page, and still exist elsewhere in the
                # table (orphan column left by a rename). Calling it a "spelling
                # mistake" would again point to a false cause. The survey
                # of the datastore's keys settles it — and it only costs on this path,
                # the one where we are about to write a warning.
                if unknown:
                    unknown = [f for f in unknown
                               if f not in _datastore_keys(store, datastore)]
                # FOURTH judge (#350): a LAYER ADDRESS — `effectif.origine`,
                # `contact.comment` — is a perfectly valid projection. It
                # only appears in `present` if the layer is filled in on AT
                # LEAST one row of the page, and never in `declared` (the schema
                # declares columns, not their layers, which are native and
                # universal). On a page where the layer is empty everywhere,
                # the warning therefore accused a correct address — and the caller,
                # re-reading a correct call, concludes the annotation does not exist
                # on this table. Yet another false cause pointed to, the third of the
                # day: the refusal must stay silent when it has nothing to reproach.
                if unknown:
                    unknown = [f for f in unknown
                               if not _adresse_de_couche_valide(f, present, declared)]
                if unknown:
                    out["warning"] = (
                        f"unknown `fields` column(s) in this datastore: "
                        f"{', '.join(unknown)} — check the spelling (absent from the result)")
            # 0 filtered results ≠ "the data does not exist": if a key of the filter
            # appears in NO sampled row, it is probably a
            # misspelled column — we FLAG it (non-blocking, feedback #163).
            if (filter or filters) and not out["rows"]:
                unknown = _unknown_filter_keys(store, datastore, filter, filters)
                if unknown:
                    out["warning"] = (
                        f"unknown filter column(s) in this datastore: "
                        f"{', '.join(sorted(unknown))} — check the spelling "
                        "(0 results may come from that)")
            return out
        except InvalidCursor:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="invalid `cursor` (restart without a cursor)"))
        except DatastoreNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(datastore, e)))
        except RowNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=_row_not_found_hint(store, datastore, id)))
        except ValueError as e:  # malformed filter / unknown operator → actionable
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))

    @mcp.tool()
    @_avec_la_recherche
    def data_aggregate(
        datastore: Adresse,
        metrics: Optional[list[dict]] = None,
        group_by: str | list[str] | None = None,
        filter: Optional[dict] = None,
        filters: Optional[list[dict]] = None,
        q: str | None = None,
        q_scope: recherche.PorteeRecherche | None = None,
        count_by: list[str] | None = None,
        values_limit: int | None = None,
    ) -> dict:
        """Aggregate rows SERVER-SIDE — stats over a whole (optionally filtered) table
        WITHOUT pulling the rows into context (feedback #191). Use this for totals and
        distributions over a large vivier (e.g. total kWc, average score, count per
        department) instead of reading 300+ rows and summing them yourself.

        `metrics` = list of `{op, field?}`; `op` ∈ count|count_rows|sum|avg|min|max
        (default `[{"op":"count"}]`). `count` without `field` = total rows;
        sum/avg/min/max require a numeric `field` and ignore non-numeric values.
        `group_by` = a column to group on (omit = one global row). Results are sorted
        by the first metric descending when grouped (so `group_by` gives you the TOP
        groups first).

        A metric may carry its OWN condition — `where`, same clauses as `filters` —
        so the total and a subset are counted in the SAME query. That is how you get a
        RATE without crossing two calls whose scopes can silently differ. Give such a
        metric a `label`, and it comes back under that name.

        `group_by` also accepts a LIST of columns: their values are POOLED, one row
        contributing one occurrence per filled column ("all ranks together"). Under a
        pooled group, `count` counts OCCURRENCES and `count_rows` counts ROWS — two
        different questions, so ask for the one you mean.

        A `list` column (e.g. `contacts`) is aggregated ACROSS ITS ITEMS with
        `contacts[].<attribute>`. `group_by: "contacts[].fonction"` makes every item of
        every matching row one occurrence — same vocabulary: `count` counts the
        contacts, `count_rows` the rows; a contact without that attribute falls in the
        `null` group. `filters` still select ROWS (a row kept by
        `contacts[].fonction eq DRH` contributes ALL its contacts). sum/avg/min/max
        with `field: "contacts[].<numeric attribute>"` aggregate over all items (read
        on the current item when grouping by the same list). A list path cannot be
        pooled with other columns, nor sorted; `contacts[0].x` targets one rank.
        A list of plain VALUES (e.g. `tags`) is read element by element with
        `tags[]`: `group_by: "tags[]"` counts each tag (`count`) and the rows carrying
        it (`count_rows`); `filters` on `tags[]` keep the rows holding the value.

        ⚠️ Pooling is NOT a two-dimensional group-by, and `group_by: "a,b"` is not one
        either — it is REFUSED (oto#50). A comma-separated string used to be read as a
        single column name containing a comma: no row carries it, so you got 200 with
        ONE group of key `null` holding every row, and no way to tell that from empty
        data. Group on one column per call, or pass the list if you meant to pool.
        Crossing two dimensions is not served yet.

        Returns `{results: [...]}` — each entry carries the `group_by` value (when set)
        plus one key per metric (`count`, `sum_<field>`, `avg_<field>`, or `label`).

        `count_by` = a list of columns to profile in ONE call (not with `group_by`
        or `metrics`): returns `{counts: {column: {values: [{value, count}], empty,
        distinct, total, truncated}}}` — most frequent values first, at most
        `values_limit` (default 50, max 500) per column; `truncated` says more exist.

        Examples —
            - total rows matching a filter: metrics omitted, filter={"statut":"qualified"}
            - MWc by department: group_by="departement",
              metrics=[{"op":"sum","field":"kwc_estime"}, {"op":"count"}]
            - share of companies with an HR contact on ANY rank, by headcount band —
              one call, no rows pulled:
              group_by="tranche_effectif",
              metrics=[{"op":"count","label":"fiches"},
                       {"op":"count","label":"avec_rh","where":[
                          {"fields":["contact1_fonction","contact2_fonction",
                                     "contact3_fonction"],
                           "op":"in","value":["DRH","DAF"]}]}]
            - which roles appear across all contact ranks:
              group_by=["contact1_fonction","contact2_fonction","contact3_fonction"]
            - the same on a `list` column, contacts and companies counted apart:
              group_by="contacts[].fonction",
              metrics=[{"op":"count"}, {"op":"count_rows"}]

        Args:
            datastore: target datastore, or `slot:<name>` (active project).
            metrics: list of `{op, field?, where?, label?}` aggregations
                (default = count of rows).
            group_by: column to group by, `list[].attribute` to group the items of
                a list column, or a LIST of columns whose values are pooled (omit =
                global aggregate, single row).
            filter: dict `{column: value}` exact match to scope the aggregate.
            filters: list of clauses, incl. multi-column ones — same grammar as
                `data_rows.filters`. Combines with `filter` (AND).
            q: <<recherche_q>> Aggregates the same set a `data_rows` search
                shows (ranking aside).
            q_scope: <<recherche_q_scope>>
            count_by: columns to count values and empties of, in one call.
            values_limit: with `count_by`, values kept per column (1-500).
        """
        store = _acting_store()
        datastore, _ = _adresse(datastore)
        try:
            jetons.verifier_champs(filter=filter, filters=filters)
            if count_by is not None:
                if group_by or metrics:
                    raise ValueError("`count_by` does not combine with `group_by` or "
                                     "`metrics`: one call counts columns, the other "
                                     "aggregates")
                return {"counts": store.count_by(
                    datastore, count_by, filter=filter, filters=filters, q=q,
                    q_scope=q_scope, values_limit=values_limit)}
            if values_limit is not None:
                raise ValueError("`values_limit` only applies with `count_by`")
            results = store.aggregate(
                datastore, group_by=group_by, metrics=metrics, filter=filter,
                filters=filters, q=q, q_scope=q_scope)
            return {"results": results}
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        except DatastoreNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(datastore, e)))

    @mcp.tool()
    def data_delete_row(datastore: Adresse, id: str | None = None,
                        expected_revision: Optional[str] = None,
                        ids: list[str | dict] | None = None) -> dict:
        """Delete a row by `_id` — or a BATCH of rows with `ids`, in one call.
        `datastore` accepts `slot:<name>` (active project).

        `expected_revision` = the `_revision` of the row as you read it, when it is
        that read that made you decide to delete. If the row changed since (any
        column, or its reservation), nothing is deleted and the call is refused.
        Omit it when you delete a row you did not have to read first.

        BATCH (`ids`, up to 500): each item is an `_id`, or
        `{"id": …, "expected_revision": …}` to guard that row. Each row is deleted
        on its own: one refused row (changed since read, reserved by another work)
        does not stop the others. Returns `deleted`, `not_found` and `refused`
        (`id`, `error`, `current_revision` on a conflict). A malformed item refuses
        the whole call before anything is deleted.
        """
        sub = access.current_user_sub_or_raise()
        store = _store_for(sub)
        if ids is not None:
            return _delete_rows(store, datastore, id, expected_revision, ids)
        if id is None:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message="`id` (one row) or `ids` (a batch) required"))
        datastore, id = _adresse(datastore, id)
        try:
            store.delete_row(datastore, id, expected_revision=expected_revision)
        except DatastoreNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(datastore, e)))
        except DatastoreReadOnly:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"datastore `{datastore}` shared read-only"))
        except RowNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"row `{id}` not found"))
        except RowLocked as e:
            # #317: a refusal, not a 500. Without this translation the agent sees "Internal
            # server error" where it needs WHO holds the row, UNTIL WHEN,
            # and HOW to release — lived in production on 15/08, on a blocked campaign.
            # The exception's message already carries all three; `_row_locked_message`
            # adds the CAUSE when it is proven (`_run_id` omitted, #547).
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_row_locked_message(e)))
        except ValueError as e:
            # `RevisionConflict` (oto#217) and the unreadable precondition: their message
            # says what changed and what to do. Without this branch they would come out as
            # "Internal server error" — the defect already paid for on `RowLocked`.
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        return {"ok": True, "id": id}

    @mcp.tool()
    def data_url(datastore: Adresse, filter: Optional[dict] = None, q: str | None = None,
                 order_by: str | None = None, order_dir: str = "desc") -> dict:
        """Return the web URL of a datastore, for the user to open it. `url` is null
        when this account's product has no table page — `url_absente` then says why;
        the table still exists. `datastore` accepts `slot:<name>` (active project).
        With `filter`/`q`/`order_by` (as in `data_rows`), the URL opens that filtered,
        sorted view — a shareable link — and `count` = the rows it shows."""
        sub = access.current_user_sub_or_raise()
        store = _store_for(sub)
        datastore, _ = _adresse(datastore)
        try:
            url = store.get_url(datastore)
            if url is None or not (filter or q or order_by):
                return adresse_servie(url, sub)
            # The view is written in the format OUR table page reads (signal 1355); a
            # product not known to read it gets a refusal, never a link that would show
            # the whole table while claiming a filter.
            from .. import links
            from ..datastore import vue_par_url
            if not links.table_lit_sa_vue(sub):
                raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                    "this account's table page is not known to read a filtered view from "
                    "its address: call `data_url` without `filter`/`q`/`order_by` for the "
                    "plain link.")))
            jetons.verifier_champs(filter=filter)
            # The REAL path judges the filter (operators, values, `q`) and counts what the
            # link will show; a column found nowhere in the table is refused, not linked.
            total = store.count_rows(datastore, filter=filter, q=q)
            vise = {**(filter or {}), **({order_by: None} if order_by else {})}
            inconnues = _unknown_filter_keys(store, datastore, vise) if vise else set()
            if inconnues:
                raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                    f"unknown column(s) in this datastore: {', '.join(sorted(inconnues))}"
                    " — check the spelling")))
            return {"url": vue_par_url.avec_la_vue(url, filter=filter, q=q,
                                                   order_by=order_by, order_dir=order_dir),
                    "count": total, **identite.numero(store.dernier_tableau)}
        except DatastoreNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(datastore, e)))
        except ValueError as e:  # malformed filter / unknown operator / dropped value
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))

    @mcp.tool()
    def data_share(
        datastore: Adresse, email: str = "", permission: str = "read", remove: bool = False,
        recipient_sub: str = "",
    ) -> dict:
        """Share (or with `remove=True`, unshare) a datastore with another oto user.
        The recipient accesses it with their own oto account.

        Identify the recipient by `email`, or by `recipient_sub` when one address
        carries several accounts — an ambiguous address is REFUSED, never guessed.

        Args:
            datastore: datastore to (un)share (must be owned by you).
            email: email of the recipient oto user.
            permission: 'read' or 'write' (default write) — when sharing.
            remove: True = revoke access instead of granting it.
            recipient_sub: the recipient's `sub`, when `email` is ambiguous.
        """
        sub = access.current_user_sub_or_raise()
        datastore = _ns(datastore)
        recipient = _destinataire(email, recipient_sub)

        # Sharing is a GOVERNANCE action (owner ∪ roles.py escalation).
        try:
            # The store is guarded: resolution there picks up the table (number +
            # canonical name), and that is what the response must carry — not an echo of
            # the address received.
            store_partage = _store_for(sub)
            ns_id = store_partage.resolve_ns_id(datastore)
        except DatastoreNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(datastore, e)))
        if not ownership.can_govern(sub, ownership.TYPE_RESSOURCE_DATASTORE, str(ns_id)):
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=f"you are not allowed to manage sharing for `{datastore}`"))

        # What we return names the account SERVED, not the argument received: when called by
        # `recipient_sub`, `email` is empty, and "shared with ␣" would be false.
        # `..._sub` is the only identifier that designates one account and only one.
        cible = recipient.get("email") or recipient["sub"]

        if remove:
            removed = ownership.revoke(ownership.TYPE_RESSOURCE_DATASTORE, str(ns_id), "user", recipient["sub"])
            if not removed:
                raise McpError(ErrorData(code=INVALID_PARAMS,
                                         message=f"no active share for {cible} on {datastore}"))
            return {"ok": True,
                    **identite.de_releve(store_partage.dernier_tableau, datastore),
                    "unshared_with": cible,
                    "unshared_with_sub": recipient["sub"]}

        if permission not in ("read", "write"):
            raise McpError(ErrorData(code=INVALID_PARAMS, message="permission must be 'read' or 'write'"))
        ownership.grant(ownership.TYPE_RESSOURCE_DATASTORE, str(ns_id), "user", recipient["sub"],
                        permission, granted_by=sub)
        return {"ok": True,
                **identite.de_releve(store_partage.dernier_tableau, datastore),
                "shared_with": cible,
                "shared_with_sub": recipient["sub"], "permission": permission}

    # --- MCP App: rendered-UI variant of the datastore (SEP-1865) --------
    # `data_app` renders a datastore's content INLINE (card + sortable /
    # searchable table) instead of only returning a dashboard link (`data_url`).
    # OPTIONAL import of prefab_ui (extra `fastmcp[apps]`): absent → the app is
    # not registered, the JSON tools above are enough (graceful
    # degradation, same pattern as foncier.py).
    try:
        from prefab_ui.components import (  # type: ignore
            Card, Column, DataTable, DataTableColumn, Heading, Text,
        )
    # noqa: SILENT — extra `apps` absent ⇒ no app, the JSON tools are enough
    except Exception:  # pragma: no cover - extra `apps` absent
        return

    _META = ("_id", "_created_at", "_updated_at")

    def _label(k: str) -> str:
        return str(k).lstrip("_").replace("_", " ").capitalize()

    def _is_scalar(v: object) -> bool:
        return isinstance(v, (str, int, float, bool)) or v is None

    def _compact(v: object, limit: int = 90) -> str:
        """1-line summary of a nested value for a DataTable cell:
        list → `n × {preview of the 1st item}`; dict → compact JSON. Truncated."""
        try:
            if isinstance(v, list):
                head = json.dumps(v[0], ensure_ascii=False, default=str) if v else ""
                s = f"{len(v)} × {head}" if head else "0 item"
            else:
                s = json.dumps(v, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            s = str(v)
        return s if len(s) <= limit else s[: limit - 1] + "…"

    def _message_card(title: str, message: str) -> "Card":
        with Card() as card:
            with Column(gap=4):
                Heading(title)
                Text(message)
        return card

    # ── schema v2 awareness (ADR 0046) ───────────────────────────────────────
    # A typed datastore carries nested fields (`object`/`list` → occupant{},
    # contacts[], signaux[]) + `title`/`status` roles (+ lifecycle). The flat
    # table collapsed all of that into `n × {...}`: a record lost its structure. So we
    # render (1) the list with the columns IN SCHEMA ORDER, (2) a single
    # record in detail — sub-records unfolded into sub-tables.
    def _fdefs(schema: Optional[dict]) -> list:
        return [f for f in (schema or {}).get("fields") or [] if isinstance(f, dict)]

    def _role_key(schema: Optional[dict], role: str) -> Optional[str]:
        """The key of the field that plays this role on screen.

        ⚠️ `title` goes through `dsv2.title_field` (#317): the designation now lives
        in `display`, and this function must not keep a second reading of
        `role` — two paths for the same question end up diverging."""
        if role == "title":
            return (dsv2.title_field(schema) or {}).get("key")
        for f in _fdefs(schema):
            if f.get("role") == role and f.get("key"):
                return f["key"]
        return None

    def _ordered_keys(schema: Optional[dict], present: list) -> list:
        """Present keys reordered by the schema's declaration order;
        keys outside the schema (including meta) are appended at the end. Without a schema =
        order of appearance unchanged (0016 behavior)."""
        decl = [f["key"] for f in _fdefs(schema) if f.get("key")]
        if not decl:
            return list(present)
        seen, out = set(), []
        for k in decl:
            if k in present and k not in seen:
                out.append(k); seen.add(k)
        for k in present:
            if k not in seen:
                out.append(k); seen.add(k)
        return out

    def _rows_table(records: list, *, show_meta: bool,
                    schema: Optional[dict] = None) -> None:
        """Render a list of dicts as a sortable/searchable DataTable (scalar
        cells only). The meta columns (`_id`/`_created_at`/
        `_updated_at`) are hidden by default for a clean view — `data_rows`
        exposes them as JSON when you need to act (e.g. `_id` for an update). With a
        v2 `schema`, the columns follow the fields' declaration ORDER."""
        rows, keys = [], []
        for r in records:
            row = {}
            for k, v in r.items():
                if k in _META and not show_meta:
                    continue
                if not _is_scalar(v):
                    # Sub-record / list (schema v2, ADR 0046): compact summary
                    # instead of dropping the column (a record without its contacts[] lied).
                    v = _compact(v)
                row[k] = v
                if k not in keys:
                    keys.append(k)
            rows.append(row)
        if schema is not None:
            keys = _ordered_keys(schema, keys)
        cols = [DataTableColumn(key=k, header=_label(k), sortable=True) for k in keys]
        DataTable(columns=cols, rows=rows, search=True, paginated=len(rows) > 20, pageSize=20)

    def _status_line(schema: Optional[dict], value: object) -> None:
        """"Status: X" line enriched with the lifecycle: (terminal) or the
        allowed next states, so the agent knows what to do next."""
        txt = f"Status: {value}"
        lc = dsv2.lifecycle_of(schema)
        if lc:
            if dsv2.is_terminal_status(schema, value):
                txt += " (terminal)"
            else:
                # oto#63: no more wrapping a string into a list — a stored block
                # of the wrong shape is STATED on the line, not guessed.
                try:
                    nxt = (dsv2.table_des_transitions(
                        str(dsv2.status_field(schema)["key"]), lc) or {}).get(
                        str(value)) or []
                except ValueError as e:
                    txt += f" — {e}"
                    nxt = []
                if nxt:
                    txt += f" — next: {', '.join(nxt)}"
        Text(txt)

    def _render_composite(key: str, value: object, fdef: Optional[dict]) -> None:
        """Unfold a nested field: `list` of sub-records → sub-DataTable;
        `list` of scalars → bullets; `object` → key/value pairs. This is the core
        of the v2 adaptation (before, a `contacts[]` ended up as `3 × {...}`)."""
        ftype = (fdef or {}).get("type")
        Heading(_label(key))
        if ftype == "list" or isinstance(value, list):
            items = value if isinstance(value, list) else []
            if not items:
                Text("(empty)")
            elif all(isinstance(it, dict) for it in items):
                _rows_table(items, show_meta=True,
                            schema=(fdef or {}).get("of"))
            else:
                for it in items:
                    Text(f"· {it if _is_scalar(it) else _compact(it, 200)}")
        elif ftype == "object" or isinstance(value, dict):
            d = value if isinstance(value, dict) else {}
            if not d:
                Text("(empty)")
            else:
                for k, v in d.items():
                    Text(f"{_label(k)}: {v if _is_scalar(v) else _compact(v, 200)}")

    def _fiche_card(record: dict, schema: Optional[dict], url: str,
                    *, show_meta: bool) -> "Card":
        """DETAIL view of ONE record: title (`display="title"`), status+lifecycle,
        scalars as key/value, then each sub-record unfolded. The v2 value-add."""
        by_key = {f["key"]: f for f in _fdefs(schema) if f.get("key")}
        title_key = _role_key(schema, "title")
        status_key = _role_key(schema, "status")
        biz_key = (schema or {}).get("key")
        title = (record.get(title_key) if title_key else None) \
            or (record.get(biz_key) if biz_key else None) \
            or record.get("_id") or "Record"
        scalars, composites = [], []
        for k in _ordered_keys(schema, list(record.keys())):
            if k in (title_key, status_key):
                continue
            if k in _META and not show_meta:
                continue
            v = record.get(k)
            fdef = by_key.get(k)
            ftype = (fdef or {}).get("type")
            if ftype in ("object", "list") or (fdef is None and not _is_scalar(v)):
                composites.append((k, v, fdef))
            else:
                scalars.append((k, v))
        with Card() as card:
            with Column(gap=4):
                Heading(str(title))
                if status_key and record.get(status_key) is not None:
                    _status_line(schema, record.get(status_key))
                for k, v in scalars:
                    Text(f"{_label(k)}: {'' if v is None else v}")
                Text(f"edit: {url}")
                for k, v, fdef in composites:
                    _render_composite(k, v, fdef)
        return card

    # An instance that declares a front bundle (`OTO_DATA_APP_BUNDLE`) draws `data_app`
    # in that front's look instead — `tools/datastore_table_app.py` registers it.
    from .datastore_table_app import bundle as _front_bundle
    if _front_bundle() is not None:
        return

    @mcp.tool(app=True)
    def data_app(
        datastore: Optional[Adresse] = None,
        filter: Optional[dict] = None,
        row: str | None = None,
        limit: int = 100,
        show_meta: bool = False,
    ):  # no return annotation `-> Card`: with `from __future__ import
        # annotations`, fastmcp resolves the hints against the module globals at
        # schema build time, but `Card` (prefab_ui) is imported LOCALLY in register() →
        # fatal NameError at startup (data_app outside register_all's try/except,
        # prod crash-loop lived 2026-06-28). The body works through closure. See #69.
        """Rendered datastore browser (MCP App / interactive card).

        Visual variant of `data_url` that renders the data INLINE instead of just
        returning a dashboard link. WITHOUT `datastore` = a table of your
        datastores. WITH `datastore` = a sortable/searchable table of its rows,
        with an optional `filter` (same grammar as `data_rows`).

        Schema-aware (datastore v2, ADR 0046): a typed datastore renders its
        columns in the declared field order, and a SINGLE fiche is shown in a
        detail view — nested `object`/`list` fields (e.g. `contacts[]`, `signaux[]`)
        are expanded as sub-tables instead of a `"3 × {...}"` blob, and the
        `status` field shows its lifecycle (terminal / next allowed states). The
        detail view opens automatically when `filter` narrows to one row, or on
        demand with `row`.

        Use when the user wants to *see* and explore datastore content (e.g. a
        watch-list or a lead fiche) without leaving the chat. For raw JSON use
        `data_rows`; to edit a row, follow the dashboard link shown on the card.

        Args:
            datastore: target datastore ; omit = list all your datastores.
            filter: pre-filter rows, same grammar as `data_rows` — `{column: value}`
                exact match (e.g. `{"priorite": "P1"}`) or `{column: {op: value}}`
                with `op` in `eq`/`ne`/`contains`/`in`/`gt`/`gte`/`lt`/`lte`/
                `empty`/`not_empty` (e.g. `{"statut": {"in": ["actif", "clos"]}}`).
                An unknown operator is refused, never read as "no rows".
            row: open ONE fiche in detail view — matched against `_id`, the
                declared business key (`schema.key`), or the title field value.
            limit: max rows rendered (default 100).
            show_meta: also show the `_id`/`_created_at`/`_updated_at` columns
                (hidden by default).
        """
        sub = access.current_user_sub_or_raise()
        if datastore:
            datastore = _ns(datastore)
        store = _store_for(sub)

        if not datastore:
            spaces = store.list_datastores()
            if not spaces:
                return _message_card(
                    "No datastore",
                    "Create one with data_create_datastore, then write with data_write.",
                )
            index = [
                {"datastore": s["datastore"],
                 "structure": "typed" if _fdefs(s.get("schema")) else "free",
                 "partage": "yes" if s.get("shared") else "no",
                 "lien": s.get("url", "")}
                for s in spaces
            ]
            with Card() as card:
                with Column(gap=4):
                    Heading("Datastore")
                    Text(f"{len(spaces)} datastore(s)")
                    _rows_table(index, show_meta=True)
            return card

        try:
            rows = store.list_rows(datastore, filter=filter, limit=limit)
            url = store.get_url(datastore)
            schema = store.get_schema(datastore)
        except DatastoreNotFound:
            return _message_card(
                "Datastore not found",
                f"No datastore “{datastore}” on your account.",
            )
        except ValueError as e:
            # Malformed filter (unknown operator…): refused by name — an empty
            # table here would read as "the data does not exist" (oto#74).
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))

        # DETAIL view of a record: explicit `row`, or a `filter` that isolates 1 row.
        fiche = None
        if row is not None:
            fiche = pick_fiche(rows, schema, row)
            if fiche is None:
                return _message_card(
                    "Record not found",
                    f"No record “{row}” in “{datastore}”.",
                )
        elif filter and len(rows) == 1:
            fiche = rows[0]
        if fiche is not None:
            return _fiche_card(fiche, schema, url, show_meta=show_meta)

        suffix = f" (filter {filter})" if filter else ""
        with Card() as card:
            with Column(gap=4):
                Heading(datastore)
                Text(f"{len(rows)} row(s){suffix} · edit: {url}")
                if rows:
                    _rows_table(rows, show_meta=show_meta, schema=schema)
                elif filter:
                    Text("No rows for this filter.")
                else:
                    Text("No rows.")
        return card
