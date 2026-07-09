"""Datastore — stockage de données structurées légères par user (PG natif, ADR 0016).

Chaque user a son propre set de "namespaces". Schéma libre : chaque row = un
dict JSON (stocké en JSONB, types préservés), les champs apparaissent au fur et
à mesure. Trois champs auto-managés exposés à plat : `_id`, `_created_at`,
`_updated_at`. Aucune dépendance externe — surface plateforme self-contained.

Surface (« moins d'outils, plus d'args ») : `data_write`/`data_rows`/`data_share`
fondent append↔update / get↔list / share↔unshare via un arg de mode. Les
destructifs (delete_namespace, delete_row) et la création restent séparés.
"""
from __future__ import annotations

import json
from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, datastore_schema as dsv2, db, ownership
from ..datastore import (
    InvalidCursor,
    NamespaceExists,
    NamespaceForbidden,
    NamespaceNotFound,
    NamespaceReadOnly,
    RowNotFound,
    make_org_store,
    make_store,
)


def _store_for(sub: str):
    return make_store(sub)


def _acting_store():
    """Store du datastore pour l'acteur courant, pour les tools NON-gouvernance
    (list/read/write/schema).

    - User authentifié (`sub`) → son store, contexte = son org active (inchangé).
    - Endpoint MCP `secret` avec opt-in datastore (ADR 0032) → store agissant SOUS
      L'ORG propriétaire du projet (sub-less), **scopé aux tableaux LIÉS au projet**
      (anti-fuite #193) et en **lecture seule** sauf opt-in write séparé.
    - Sinon (endpoint sans login SANS opt-in) → McpError « Unauthenticated ».

    Les tools de GOUVERNANCE/destructifs (create/delete/rename/share) n'utilisent PAS
    ce seam : ils gardent `current_user_sub_or_raise()` → jamais exposés sur un endpoint
    sans user identifié."""
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
    access.current_user_sub_or_raise()  # pas d'opt-in → lève « Unauthenticated »


def _anon_project_tableau_ns_ids(project_id: Optional[int]) -> frozenset:
    """Ids des namespaces LIÉS au projet (`project_links` type tableau) — le datastore
    exposé sur un endpoint partagé est scopé à CES tableaux, jamais tout le datastore de
    l'org (anti-fuite #193). Un lien tableau porte soit l'id numérique du namespace, soit
    son NOM (liens legacy d'avant la normalisation nom→id) → on résout LES DEUX formes
    contre le datastore de l'org propriétaire (`current_anon_org`). project_id None /
    erreur / aucun lien ⇒ frozenset() (rien d'exposé, jamais de fallback ouvert)."""
    from .. import subdomain_project
    if project_id is None:
        return frozenset()
    try:
        org = subdomain_project.current_anon_org()
        ids: set[int] = set()
        for l in db.list_project_links(int(project_id)):
            if l.get("target_type") != "tableau":
                continue
            ref = str(l.get("target_ref") or "").strip()
            if not ref:
                continue
            if ref.isdigit():
                ids.add(int(ref))
            elif org is not None:
                ns = db.get_datastore_namespace("org", str(org), ref)
                if ns:
                    ids.add(int(ns["id"]))
        return frozenset(ids)
    except Exception:  # noqa: BLE001
        return frozenset()


def _project_hint(namespace: str) -> Optional[str]:
    """Suggestion inverse run→lien (ADR 0035 B5) : écrire sous PROJET ACTIF dans un
    namespace NON lié au projet ⇒ suggérer le lien — aujourd'hui c'est de la
    discipline LLM (« pense à linker »), ici le substrat le rappelle au moment de
    l'acte. Jamais bloquant, jamais d'auto-link (le lien est une décision).
    Best-effort : toute erreur ⇒ None."""
    try:
        pid = access.current_project()
        if pid is None:
            return None
        links = db.list_project_links(int(pid))
        linked = {l.get("namespace") for l in links if l.get("target_type") == "tableau"}
        if namespace in linked:
            return None
        return (f"ce tableau `{namespace}` n'est pas lié au projet actif (#{pid}) — "
                f"si c'est une sortie du projet, lie-le : `oto_project op=link "
                f"project_id={pid} target_type=tableau target_ref=<id du namespace> "
                "(+ slot='<name>' s'il réalise un slot de procédure)`.")
    except Exception:  # noqa: BLE001
        return None


def _unknown_filter_keys(store, namespace: str, filter: dict) -> set[str]:
    """Clés de `filter` absentes de TOUTES les lignes d'un échantillon du namespace
    (feedback #163 : filtre sur colonne inexistante = 0 résultat silencieux,
    indiscernable d'un « aucune ligne ne matche »). Chemin résultat-vide seulement.
    Namespace vide ou erreur ⇒ set() (rien d'affirmable, pas de faux warning)."""
    try:
        sample = store.cursor_rows(namespace, limit=50)["rows"]
        if not sample:
            return set()
        known: set[str] = set()
        for r in sample:
            known |= set(r.keys())
        return {k for k in filter if k not in known}
    except Exception:  # noqa: BLE001
        return set()


def _row_not_found_hint(store, namespace: str, row_id: object) -> str:
    """Message actionnable d'un lookup `id` raté (feedback #161 : le param `id`
    cherche par `_id` UUID technique ; quand le schéma déclare une clé métier —
    souvent nommée `id` — l'agent passe naturellement SA valeur et tombe sur
    « introuvable » sans piste). Si une ligne matche la clé métier, on le dit."""
    msg = f"row `{row_id}` introuvable (le param `id` cherche par `_id` technique)"
    try:
        key = store.declared_key(namespace)
        if key:
            hit = store.cursor_rows(namespace, filter={key: row_id}, limit=1)["rows"]
            if hit:
                return (f"{msg} ; une ligne a bien `{key}={row_id}` (clé métier) — "
                        f"utilise `filter={{\"{key}\": \"{row_id}\"}}`, son `_id` est "
                        f"`{hit[0].get('_id')}`")
            return f"{msg} ; pour la clé métier `{key}`, utilise `filter={{\"{key}\": …}}`"
    except Exception:  # noqa: BLE001
        pass
    return msg


def _project_row(row: dict, fields: list[str]) -> dict:
    """Projette une row sur `fields` (sous-ensemble de colonnes, feedback #191) en
    gardant TOUJOURS `_id` — sans lui l'agent ne pourrait plus adresser/mettre à jour
    la ligne. Les champs demandés absents de la row sont simplement omis."""
    keep = set(fields)
    keep.add("_id")
    return {k: v for k, v in row.items() if k in keep}


def _ns(namespace: str) -> str:
    """Adressage par SLOT (ADR 0035 B3) : `slot:<name>` = le tableau bindé sous ce
    nom par le PROJET ACTIF (`access.resolve_slot_tableau` — erreur actionnable si
    pas de projet actif / slot non bindé / binding pendouillant, JAMAIS de fallback).
    Un nom nu passe inchangé (zéro magie sur les noms littéraux)."""
    if isinstance(namespace, str) and namespace.strip().lower().startswith(access.SLOT_PREFIX):
        return access.resolve_slot_tableau(namespace.strip()[len(access.SLOT_PREFIX):])
    return namespace


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def data_list_namespaces() -> dict:
        """List the user's datastore namespaces (owned + shared)."""
        store = _acting_store()
        return {"namespaces": store.list_namespaces()}

    @mcp.tool()
    def data_create_namespace(namespace: str) -> dict:
        """Create a new datastore namespace (PG-backed, schema-free).

        Args:
            namespace: kebab-case identifier, unique per user (e.g. `timetrack`).
        """
        sub = access.current_user_sub_or_raise()
        if not namespace or not namespace.strip():
            raise McpError(ErrorData(code=INVALID_PARAMS, message="namespace requis"))
        if namespace.strip().lower().startswith(access.SLOT_PREFIX):
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=("un slot binde un tableau EXISTANT — crée le namespace avec son "
                         "nom réel, puis binde-le au projet "
                         "(`oto_project op=link target_type=tableau … slot='<name>'`).")))
        store = _store_for(sub)
        try:
            return store.create_namespace(namespace.strip())
        except NamespaceExists:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"namespace `{namespace}` existe déjà",
            ))

    @mcp.tool()
    def data_delete_namespace(namespace: str) -> dict:
        """Delete a namespace and all its rows (irreversible). Owner (or org/platform
        admin governing it) only."""
        sub = access.current_user_sub_or_raise()
        namespace = _ns(namespace)
        store = _store_for(sub)
        try:
            store.delete_namespace(namespace)
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))
        except NamespaceForbidden:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=f"tu n'as pas le droit de supprimer `{namespace}`"))
        return {"ok": True, "namespace": namespace}

    @mcp.tool()
    def data_rename_namespace(namespace: str, new_name: str) -> dict:
        """Rename a namespace. Only the name changes — the id, URL/deeplink and shares
        stay stable (grants are keyed by id). Governance right required (owner, or the
        org/platform admin governing it). The new name must be free for the same owner.

        Use this to lift a name collision (e.g. two `reconcile_log` across orgs) before
        transferring/consolidating: rename one side, then transfer with `oto_resource`.

        Args:
            namespace: current namespace (or `slot:<name>` under the active project).
            new_name: the new kebab-case name (must be unique for the owner).
        """
        sub = access.current_user_sub_or_raise()
        namespace = _ns(namespace)
        if not new_name or not new_name.strip():
            raise McpError(ErrorData(code=INVALID_PARAMS, message="new_name requis"))
        if new_name.strip().lower().startswith(access.SLOT_PREFIX):
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message="`slot:` est réservé à l'adressage — choisis un nom réel."))
        store = _store_for(sub)
        try:
            return store.rename_namespace(namespace, new_name)
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))
        except NamespaceForbidden:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=f"tu n'as pas le droit de renommer `{namespace}`"))
        except NamespaceExists as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))

    @mcp.tool()
    def data_set_schema(namespace: str, schema: Optional[dict] = None) -> dict:
        """Declare (or clear with schema=null) a namespace's TYPED schema (ADR 0032 §6).

        A typed namespace renders as readable cards/records instead of a flat table.
        `schema` = {"fields": [{"key": str, "label"?: str, "type"?: "text|number|date|
        bool|json|object|list", "role"?: "title|badge|metric|status|qualif|note"}],
        "key"?: str, "strict"?: bool}.
        The optional top-level `"key"` names the field that is the row's BUSINESS KEY
        (e.g. "email", "siren"): batch writes (`data_write` rows=…, `oto_upload_url`)
        then UPSERT on it — same key value updates the existing row instead of
        duplicating. Default is SOFT (rendering/dedup only, no write validation).
        Pass schema=null to switch back to free-table mode.

        STRUCTURED RECORDS (ADR 0046 — every layer opt-in):
        - nested types: `type:"object"` + `fields:[…]` (sub-record, e.g. occupant);
          `type:"list"` + `of:<field-def>` (list of scalars or sub-records, e.g.
          contacts = list of {nom, titre, email}).
        - write validation: `field.required: true`, type conformity, and
          `field.required_when: {"<field>": "<value>"}` (e.g. deliverables required
          when status="qualified") — active when `strict: true` or any field has
          required/required_when. A non-conforming write FAILS naming the culprit.
        - lifecycle: on the `role:"status"` field, `lifecycle: {states:[…],
          transitions:{from:[to…]}, terminal?:[…]}` — unknown state or undeclared
          transition is refused; entering a terminal state auto-releases the row's
          work-queue claim (cf. data_claim_next).

        Args:
            namespace: target namespace (must exist; you must have write access).
            schema: the schema object, or null to clear it.
        """
        store = _acting_store()
        namespace = _ns(namespace)
        try:
            return store.set_schema(namespace, schema)
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))
        except NamespaceReadOnly:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=f"namespace `{namespace}` partagé en lecture seule"))
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))

    @mcp.tool()
    def data_write(namespace: str, row: dict | None = None, id: str | None = None,
                   rows: list | None = None, key: str | None = None) -> dict:
        """Write one row, or a BATCH of rows in a single call.

        SINGLE (`row`): WITHOUT `id` = append a NEW row (new JSON keys auto-create
        columns). WITH `id` = PARTIAL update of that row (only provided fields
        change). Returns the row (with `_id`/`_created_at`/`_updated_at`).

        BATCH (`rows` = list of dicts): write them all at once — for importing a
        dataset without round-tripping each row through your context. If a business
        KEY is in effect (the `key` arg, else the namespace's declared `schema.key`),
        every row carrying that key value UPSERTS (merges) onto the existing row of
        the same key instead of duplicating; rows without a key are appended. Returns
        a summary {inserted, updated, count, key, ids}. Use `data_set_schema` to
        declare a persistent `key`. For LARGE batches, prefer `oto_upload_url` to push
        the data out-of-band (never through your context).

        ⚠️ The namespace must EXIST first (create it with `data_create_namespace`);
        writing to an unknown namespace raises "namespace inconnu" — it is NOT
        auto-created. New JSON KEYS within an existing namespace, however, do
        auto-create their columns.

        `namespace` also accepts `slot:<name>` = the table BOUND under that slot
        name by the ACTIVE project (procedures reference tables as <slot:name>;
        the project maps the name via its links). Requires an active project +
        the binding — otherwise an actionable error, never a fallback.

        Args:
            namespace: target namespace (must already exist), or `slot:<name>`.
            row: single-row content as a dict (JSON-encoded automatically).
            id: omit = append a new row ; provided = partial update of that `_id`.
            rows: BATCH mode — a list of row dicts written in one call.
            key: business key field for batch upsert/dedup (else `schema.key`).
        """
        store = _acting_store()
        namespace = _ns(namespace)
        try:
            if rows is not None:
                if row is not None or id is not None:
                    raise McpError(ErrorData(code=INVALID_PARAMS,
                                             message="passer `rows` (batch) OU `row`/`id`, pas les deux"))
                if not isinstance(rows, list):
                    raise McpError(ErrorData(code=INVALID_PARAMS, message="rows doit être une liste de dicts"))
                out = {"namespace": namespace, **store.write_rows(namespace, rows, key=key)}
            else:
                if row is None:
                    raise McpError(ErrorData(code=INVALID_PARAMS,
                                             message="fournir `row` (objet) ou `rows` (liste d'objets, mode batch)"))
                if not isinstance(row, dict):
                    raise McpError(ErrorData(code=INVALID_PARAMS, message="row doit être un dict"))
                out = store.append_row(namespace, row) if id is None \
                    else store.update_row(namespace, id, row)
            hint = _project_hint(namespace)
            return {**out, "project_hint": hint} if hint else out
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))
        except NamespaceReadOnly:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` partagé en lecture seule"))
        except RowNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"row `{id}` introuvable"))

    @mcp.tool()
    def data_claim_next(namespace: str, worker: str, filter: Optional[dict] = None,
                        lease_s: int = 900) -> dict:
        """Atomically claim the NEXT unprocessed row of a namespace (work queue).

        The primitive for draining a table with N parallel (sub-)agents without
        collisions: picks the oldest row whose claim lease is free or expired
        (`FOR UPDATE SKIP LOCKED`), stamps `_claimed_by`/`_claimed_until` and
        returns it — two concurrent workers never get the same row. Returns
        `{row: null}` when nothing is left to claim.

        `worker` is a label YOU choose (e.g. "gros-conso-13") and REUSE verbatim
        on data_release — the guard so one agent cannot release another's claim.
        `filter` (exact {col: val}, e.g. {"status": "nouveau"}) selects what counts
        as claimable. The claim does NOT change the row: write your progress via
        data_write (id=…) ; writing a TERMINAL lifecycle status auto-releases the
        claim, data_release only serves abandoning without a verdict. The lease
        (`lease_s`, default 900s) recycles rows from dead workers.

        `namespace` also accepts `slot:<name>` (table bound by the active project).
        """
        store = _acting_store()
        namespace = _ns(namespace)
        try:
            row = store.claim_next(namespace, worker=worker, filter=filter,
                                   lease_s=lease_s)
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))
        except NamespaceReadOnly:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=f"namespace `{namespace}` partagé en lecture seule"))
        return {"namespace": namespace, "row": row,
                **({} if row else {"hint": "plus rien à claim (file vide pour ce filtre, "
                                           "ou tout est sous bail actif)"})}

    @mcp.tool()
    def data_release(namespace: str, id: str, worker: str) -> dict:
        """Release a claimed row WITHOUT a verdict (abandon) — work-queue counterpart
        of data_claim_next. Guarded by `worker` (same label as at claim time).
        Writing a terminal lifecycle status already auto-releases: only call this
        when giving up on a row so another worker can pick it before the lease
        expires. `namespace` also accepts `slot:<name>`."""
        store = _acting_store()
        namespace = _ns(namespace)
        try:
            released = store.release_claim(namespace, id, worker=worker)
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))
        except NamespaceReadOnly:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=f"namespace `{namespace}` partagé en lecture seule"))
        return {"namespace": namespace, "id": id, "released": released,
                **({} if released else
                   {"hint": "rien à libérer : pas de bail sur cette row, ou bail posé "
                            "par un autre worker"})}

    @mcp.tool()
    def data_rows(
        namespace: str, id: str | None = None,
        filter: Optional[dict] = None, limit: int = 100,
        cursor: str | None = None, fields: Optional[list[str]] = None,
        count_only: bool = False,
    ) -> dict:
        """Read rows. WITH `id` = the single row (by `_id`). WITHOUT `id` = one PAGE
        of rows (optional exact-match `filter`, `limit`) with a stable cursor.

        List mode returns `{rows, count, next_cursor}`. When `next_cursor` is not null
        there are MORE rows: call again with `cursor=<next_cursor>` (same namespace/
        filter) to get the next page — repeat until `next_cursor` is null. The cursor
        is keyset-stable (rows created meanwhile don't shift the paging).

        Use `count_only=True` to get just the TOTAL number of (optionally filtered)
        rows — computed server-side, no rows returned — when you only need the count
        (e.g. how many leads match a filter) without pulling the data into context.

        Use `fields` to PROJECT a subset of columns when the full row is heavy and you
        only need a few (e.g. name + email + score over a large vivier): each row is
        trimmed to those columns (plus `_id`, always kept so you can still update the
        row), drastically shrinking the payload. Bump `limit` when projecting — narrow
        rows let you pull far more per page.

        Args:
            namespace: target namespace, or `slot:<name>` = the table bound under
                that slot name by the ACTIVE project (actionable error if unbound).
            id: `_id` of one row ; omit = list rows.
            filter: dict `{column: value}` exact match (list mode only),
                e.g. `{"project": "roundtable"}`.
            limit: page size (default 100, list mode only).
            cursor: opaque `next_cursor` from a previous call = fetch the NEXT page.
            fields: list of column names to keep (projection) — the returned rows
                carry only these plus `_id`. Omit = full rows.
            count_only: return only `{total}` (filtered row count), no rows.
        """
        store = _acting_store()
        namespace = _ns(namespace)
        try:
            if count_only:
                return {"total": store.count_rows(namespace, filter=filter)}
            if id is not None:
                row = store.get_row(namespace, id)
                return _project_row(row, fields) if fields else row
            page = store.cursor_rows(namespace, filter=filter, limit=limit, cursor=cursor)
            rows = [_project_row(r, fields) for r in page["rows"]] if fields else page["rows"]
            out = {"rows": rows, "count": len(rows),
                   "next_cursor": page["next_cursor"]}
            # Projection sur des colonnes absentes de TOUTES les lignes = même piège
            # silencieux que le filter (#163) : on le signale sans bloquer.
            if fields and page["rows"]:
                present = {k for r in page["rows"] for k in r}
                unknown = [f for f in fields if f not in present]
                if unknown:
                    out["warning"] = (
                        f"colonne(s) de `fields` inconnue(s) dans ce namespace : "
                        f"{', '.join(unknown)} — vérifie l'orthographe (absentes du résultat)")
            # 0 résultat filtré ≠ « la donnée n'existe pas » : si une clé du filter
            # n'apparaît dans AUCUNE ligne échantillonnée, c'est probablement une
            # colonne mal orthographiée — on le SIGNALE (non bloquant, feedback #163).
            if filter and not out["rows"]:
                unknown = _unknown_filter_keys(store, namespace, filter)
                if unknown:
                    out["warning"] = (
                        f"colonne(s) de filter inconnue(s) dans ce namespace : "
                        f"{', '.join(sorted(unknown))} — vérifie l'orthographe "
                        "(0 résultat peut venir de là)")
            return out
        except InvalidCursor:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="`cursor` invalide (repartir sans cursor)"))
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))
        except RowNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=_row_not_found_hint(store, namespace, id)))

    @mcp.tool()
    def data_aggregate(
        namespace: str,
        metrics: Optional[list[dict]] = None,
        group_by: str | None = None,
        filter: Optional[dict] = None,
    ) -> dict:
        """Aggregate rows SERVER-SIDE — stats over a whole (optionally filtered) table
        WITHOUT pulling the rows into context (feedback #191). Use this for totals and
        distributions over a large vivier (e.g. total kWc, average score, count per
        department) instead of reading 300+ rows and summing them yourself.

        `metrics` = list of `{op, field?}`; `op` ∈ count|sum|avg|min|max (default
        `[{"op":"count"}]`). `count` without `field` = total rows; sum/avg/min/max
        require a numeric `field` and ignore non-numeric values. `group_by` = a column
        to group on (omit = one global row). Results are sorted by the first metric
        descending when grouped (so `group_by` gives you the TOP groups first).

        Returns `{results: [...]}` — each entry carries the `group_by` value (when set)
        plus one key per metric (`count`, `sum_<field>`, `avg_<field>`…).

        Examples:
            - total rows matching a filter: metrics omitted, filter={"statut":"qualified"}
            - MWc by department: group_by="departement",
              metrics=[{"op":"sum","field":"kwc_estime"}, {"op":"count"}]

        Args:
            namespace: target namespace, or `slot:<name>` (active project).
            metrics: list of `{op, field?}` aggregations (default = count of rows).
            group_by: column to group by (omit = global aggregate, single row).
            filter: dict `{column: value}` exact match to scope the aggregate.
        """
        store = _acting_store()
        namespace = _ns(namespace)
        try:
            results = store.aggregate(
                namespace, group_by=group_by, metrics=metrics, filter=filter)
            return {"results": results}
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))

    @mcp.tool()
    def data_delete_row(namespace: str, id: str) -> dict:
        """Delete a row by `_id`. `namespace` accepts `slot:<name>` (active project)."""
        sub = access.current_user_sub_or_raise()
        namespace = _ns(namespace)
        store = _store_for(sub)
        try:
            store.delete_row(namespace, id)
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))
        except NamespaceReadOnly:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` partagé en lecture seule"))
        except RowNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"row `{id}` introuvable"))
        return {"ok": True, "id": id}

    @mcp.tool()
    def data_url(namespace: str) -> dict:
        """Return the dashboard URL of a namespace (for the user to open/edit in
        browser). `namespace` accepts `slot:<name>` (active project)."""
        sub = access.current_user_sub_or_raise()
        namespace = _ns(namespace)
        store = _store_for(sub)
        try:
            return {"url": store.get_url(namespace)}
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))

    @mcp.tool()
    def data_share(
        namespace: str, email: str, permission: str = "write", remove: bool = False,
    ) -> dict:
        """Share (or with `remove=True`, unshare) a namespace with another oto user
        (by email). The recipient accesses it with their own oto account.

        Args:
            namespace: namespace to (un)share (must be owned by you).
            email: email of the recipient oto user.
            permission: 'read' or 'write' (default write) — when sharing.
            remove: True = revoke access instead of granting it.
        """
        sub = access.current_user_sub_or_raise()
        namespace = _ns(namespace)
        recipient = db.get_user_by_email(email)
        if not recipient:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"aucun utilisateur oto avec l'email {email}"))

        # Le partage est une action de GOUVERNANCE (owner ∪ escalade roles.py).
        try:
            ns_id = _store_for(sub).resolve_ns_id(namespace)
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))
        if not ownership.can_govern(sub, "datastore_namespace", str(ns_id)):
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=f"tu n'as pas le droit de gérer le partage de `{namespace}`"))

        if remove:
            removed = ownership.revoke("datastore_namespace", str(ns_id), "user", recipient["sub"])
            if not removed:
                raise McpError(ErrorData(code=INVALID_PARAMS,
                                         message=f"pas de partage actif pour {email} sur {namespace}"))
            return {"ok": True, "namespace": namespace, "unshared_with": email}

        if permission not in ("read", "write"):
            raise McpError(ErrorData(code=INVALID_PARAMS, message="permission must be 'read' or 'write'"))
        ownership.grant("datastore_namespace", str(ns_id), "user", recipient["sub"],
                        permission, granted_by=sub)
        return {"ok": True, "namespace": namespace, "shared_with": email, "permission": permission}

    # --- MCP App : variante à interface rendue du datastore (SEP-1865) --------
    # `data_app` rend le contenu d'un namespace INLINE (carte + table triable /
    # cherchable) au lieu de seulement renvoyer un lien dashboard (`data_url`).
    # Import OPTIONNEL de prefab_ui (extra `fastmcp[apps]`) : absent → on
    # n'enregistre pas l'app, les tools JSON ci-dessus suffisent (dégradation
    # gracieuse, même pattern que foncier.py).
    try:
        from prefab_ui.components import (  # type: ignore
            Card, Column, DataTable, DataTableColumn, Heading, Text,
        )
    except Exception:  # pragma: no cover - extra `apps` absent
        return

    _META = ("_id", "_created_at", "_updated_at")

    def _label(k: str) -> str:
        return str(k).lstrip("_").replace("_", " ").capitalize()

    def _is_scalar(v: object) -> bool:
        return isinstance(v, (str, int, float, bool)) or v is None

    def _compact(v: object, limit: int = 90) -> str:
        """Résumé 1-ligne d'une valeur imbriquée pour une cellule DataTable :
        liste → `n × {aperçu du 1er item}` ; dict → JSON compact. Tronqué."""
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

    # ── conscience du schéma v2 (ADR 0046) ───────────────────────────────────
    # Un namespace typé porte des fields imbriqués (`object`/`list` → occupant{},
    # contacts[], signaux[]) + des rôles `title`/`status` (+ lifecycle). La table
    # plate collapsait tout ça en `n × {...}` : une fiche perdait sa structure. On
    # rend donc (1) la liste avec les colonnes DANS L'ORDRE du schéma, (2) une
    # fiche seule en détail — sous-records dépliés en sous-tables.
    def _fdefs(schema: Optional[dict]) -> list:
        return [f for f in (schema or {}).get("fields") or [] if isinstance(f, dict)]

    def _role_key(schema: Optional[dict], role: str) -> Optional[str]:
        for f in _fdefs(schema):
            if f.get("role") == role and f.get("key"):
                return f["key"]
        return None

    def _ordered_keys(schema: Optional[dict], present: list) -> list:
        """Clés présentes réordonnées selon l'ordre de déclaration du schéma ;
        les clés hors-schéma (dont les méta) sont appendues en fin. Sans schéma =
        ordre d'apparition inchangé (comportement 0016)."""
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
        """Rend une liste de dicts en DataTable triable/cherchable (cellules
        scalaires uniquement). Les colonnes méta (`_id`/`_created_at`/
        `_updated_at`) sont masquées par défaut pour une vue épurée — `data_rows`
        les expose en JSON quand il faut agir (ex. `_id` pour un update). Avec un
        `schema` v2, les colonnes suivent l'ORDRE de déclaration des fields."""
        rows, keys = [], []
        for r in records:
            row = {}
            for k, v in r.items():
                if k in _META and not show_meta:
                    continue
                if not _is_scalar(v):
                    # Sous-record / liste (schéma v2, ADR 0046) : résumé compact au
                    # lieu de dropper la colonne (une fiche sans ses contacts[] mentait).
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
        """Ligne « Statut : X » enrichie du cycle de vie : (terminal) ou les
        suites autorisées, pour que l'agent sache quoi faire ensuite."""
        txt = f"Statut : {value}"
        lc = dsv2.lifecycle_of(schema)
        if lc:
            if dsv2.is_terminal_status(schema, value):
                txt += " (terminal)"
            else:
                nxt = (lc.get("transitions") or {}).get(str(value))
                nxt = nxt if isinstance(nxt, list) else ([nxt] if nxt else [])
                if nxt:
                    txt += f" — suites : {', '.join(str(s) for s in nxt)}"
        Text(txt)

    def _render_composite(key: str, value: object, fdef: Optional[dict]) -> None:
        """Déplie un field imbriqué : `list` de sous-records → sous-DataTable ;
        `list` de scalaires → puces ; `object` → paires clé/valeur. C'est le cœur
        de l'adaptation v2 (avant, un `contacts[]` finissait en `3 × {...}`)."""
        ftype = (fdef or {}).get("type")
        Heading(_label(key))
        if ftype == "list" or isinstance(value, list):
            items = value if isinstance(value, list) else []
            if not items:
                Text("(vide)")
            elif all(isinstance(it, dict) for it in items):
                _rows_table(items, show_meta=True,
                            schema=(fdef or {}).get("of"))
            else:
                for it in items:
                    Text(f"· {it if _is_scalar(it) else _compact(it, 200)}")
        elif ftype == "object" or isinstance(value, dict):
            d = value if isinstance(value, dict) else {}
            if not d:
                Text("(vide)")
            else:
                for k, v in d.items():
                    Text(f"{_label(k)} : {v if _is_scalar(v) else _compact(v, 200)}")

    def _fiche_card(record: dict, schema: Optional[dict], url: str,
                    *, show_meta: bool) -> "Card":
        """Vue DÉTAIL d'UNE fiche : titre (role=title), statut+lifecycle, scalaires
        en clé/valeur, puis chaque sous-record déplié. La valeur de v2."""
        by_key = {f["key"]: f for f in _fdefs(schema) if f.get("key")}
        title_key = _role_key(schema, "title")
        status_key = _role_key(schema, "status")
        biz_key = (schema or {}).get("key")
        title = (record.get(title_key) if title_key else None) \
            or (record.get(biz_key) if biz_key else None) \
            or record.get("_id") or "Fiche"
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
                    Text(f"{_label(k)} : {'' if v is None else v}")
                Text(f"éditer : {url}")
                for k, v, fdef in composites:
                    _render_composite(k, v, fdef)
        return card

    def _pick_fiche(rows: list, schema: Optional[dict], row: str) -> Optional[dict]:
        """Retrouve UNE fiche par `row` : match sur `_id`, la clé métier déclarée
        (`schema.key`), ou la valeur du field titre — le repère naturel pour l'agent."""
        biz_key = (schema or {}).get("key")
        title_key = _role_key(schema, "title")
        target = str(row)
        for r in rows:
            for probe in ("_id", biz_key, title_key):
                if probe and str(r.get(probe)) == target:
                    return r
        return None

    @mcp.tool(app=True)
    def data_app(
        namespace: str | None = None,
        filter: Optional[dict] = None,
        row: str | None = None,
        limit: int = 100,
        show_meta: bool = False,
    ):  # pas d'annotation de retour `-> Card` : avec `from __future__ import
        # annotations`, fastmcp résout les hints contre les globals du module au
        # build du schéma, or `Card` (prefab_ui) est importé LOCAL à register() →
        # NameError fatal au démarrage (data_app hors try/except de register_all,
        # crash-loop prod vécu 2026-06-28). Le corps marche par closure. Cf. #69.
        """Rendered datastore browser (MCP App / interactive card).

        Visual variant of `data_url` that renders the data INLINE instead of just
        returning a dashboard link. WITHOUT `namespace` = a table of your
        namespaces. WITH `namespace` = a sortable/searchable table of its rows,
        with an optional exact-match `filter` (same shape as `data_rows`).

        Schema-aware (datastore v2, ADR 0046): a typed namespace renders its
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
            namespace: target namespace ; omit = list all your namespaces.
            filter: dict `{column: value}` exact match to pre-filter rows,
                e.g. `{"priorite": "P1"}`.
            row: open ONE fiche in detail view — matched against `_id`, the
                declared business key (`schema.key`), or the title field value.
            limit: max rows rendered (default 100).
            show_meta: also show the `_id`/`_created_at`/`_updated_at` columns
                (hidden by default).
        """
        sub = access.current_user_sub_or_raise()
        if namespace:
            namespace = _ns(namespace)
        store = _store_for(sub)

        if not namespace:
            spaces = store.list_namespaces()
            if not spaces:
                return _message_card(
                    "Aucun namespace",
                    "Crée-en un avec data_create_namespace, puis écris avec data_write.",
                )
            index = [
                {"namespace": s["namespace"],
                 "structure": "typée" if _fdefs(s.get("schema")) else "libre",
                 "partage": "oui" if s.get("shared") else "non",
                 "lien": s.get("url", "")}
                for s in spaces
            ]
            with Card() as card:
                with Column(gap=4):
                    Heading("Datastore")
                    Text(f"{len(spaces)} namespace(s)")
                    _rows_table(index, show_meta=True)
            return card

        try:
            rows = store.list_rows(namespace, filter=filter, limit=limit)
            url = store.get_url(namespace)
            schema = store.get_schema(namespace)
        except NamespaceNotFound:
            return _message_card(
                "Namespace introuvable",
                f"Aucun namespace « {namespace} » sur ton compte.",
            )

        # Vue DÉTAIL d'une fiche : `row` explicite, ou `filter` qui isole 1 ligne.
        fiche = None
        if row is not None:
            fiche = _pick_fiche(rows, schema, row)
            if fiche is None:
                return _message_card(
                    "Fiche introuvable",
                    f"Aucune fiche « {row} » dans « {namespace} ».",
                )
        elif filter and len(rows) == 1:
            fiche = rows[0]
        if fiche is not None:
            return _fiche_card(fiche, schema, url, show_meta=show_meta)

        suffix = f" (filtre {filter})" if filter else ""
        with Card() as card:
            with Column(gap=4):
                Heading(namespace)
                Text(f"{len(rows)} ligne(s){suffix} · éditer : {url}")
                if rows:
                    _rows_table(rows, show_meta=show_meta, schema=schema)
                elif filter:
                    Text("Aucune ligne pour ce filtre.")
                else:
                    Text("Aucune ligne.")
        return card
