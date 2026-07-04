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

from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, db, ownership
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
      L'ORG propriétaire du projet (sub-less) : lecture/écriture décidées sur le
      principal org (owner-match / grant d'org).
    - Sinon (endpoint sans login SANS opt-in) → McpError « Unauthenticated ».

    Les tools de GOUVERNANCE/destructifs (create/delete/rename/share) n'utilisent PAS
    ce seam : ils gardent `current_user_sub_or_raise()` → jamais exposés sur un endpoint
    sans user identifié."""
    sub = access.current_user_sub_from_token()
    if sub:
        return make_store(sub)
    from .. import subdomain_project
    if subdomain_project.current_anon_datastore_exposed():
        return make_org_store(int(subdomain_project.current_anon_org()))
    access.current_user_sub_or_raise()  # pas d'opt-in → lève « Unauthenticated »


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
        bool|json", "role"?: "title|badge|metric|status|qualif|note"}], "key"?: str}.
        The optional top-level `"key"` names the field that is the row's BUSINESS KEY
        (e.g. "email", "siren"): batch writes (`data_write` rows=…, `oto_upload_url`)
        then UPSERT on it — same key value updates the existing row instead of
        duplicating. SOFT: no write validation — it drives rendering, dedup and tells
        the agent what each field means. Requires write access. Pass schema=null to
        switch back to free-table mode.

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
    def data_rows(
        namespace: str, id: str | None = None,
        filter: Optional[dict] = None, limit: int = 100,
        cursor: str | None = None,
    ) -> dict:
        """Read rows. WITH `id` = the single row (by `_id`). WITHOUT `id` = one PAGE
        of rows (optional exact-match `filter`, `limit`) with a stable cursor.

        List mode returns `{rows, count, next_cursor}`. When `next_cursor` is not null
        there are MORE rows: call again with `cursor=<next_cursor>` (same namespace/
        filter) to get the next page — repeat until `next_cursor` is null. The cursor
        is keyset-stable (rows created meanwhile don't shift the paging).

        Args:
            namespace: target namespace, or `slot:<name>` = the table bound under
                that slot name by the ACTIVE project (actionable error if unbound).
            id: `_id` of one row ; omit = list rows.
            filter: dict `{column: value}` exact match (list mode only),
                e.g. `{"project": "roundtable"}`.
            limit: page size (default 100, list mode only).
            cursor: opaque `next_cursor` from a previous call = fetch the NEXT page.
        """
        store = _acting_store()
        namespace = _ns(namespace)
        try:
            if id is not None:
                return store.get_row(namespace, id)
            page = store.cursor_rows(namespace, filter=filter, limit=limit, cursor=cursor)
            return {"rows": page["rows"], "count": len(page["rows"]),
                    "next_cursor": page["next_cursor"]}
        except InvalidCursor:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="`cursor` invalide (repartir sans cursor)"))
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))
        except RowNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"row `{id}` introuvable"))

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

    def _message_card(title: str, message: str) -> "Card":
        with Card() as card:
            with Column(gap=4):
                Heading(title)
                Text(message)
        return card

    def _rows_table(records: list, *, show_meta: bool) -> None:
        """Rend une liste de dicts en DataTable triable/cherchable (cellules
        scalaires uniquement). Les colonnes méta (`_id`/`_created_at`/
        `_updated_at`) sont masquées par défaut pour une vue épurée — `data_rows`
        les expose en JSON quand il faut agir (ex. `_id` pour un update)."""
        rows, keys = [], []
        for r in records:
            row = {}
            for k, v in r.items():
                if not _is_scalar(v):
                    continue
                if k in _META and not show_meta:
                    continue
                row[k] = v
                if k not in keys:
                    keys.append(k)
            rows.append(row)
        cols = [DataTableColumn(key=k, header=_label(k), sortable=True) for k in keys]
        DataTable(columns=cols, rows=rows, search=True, paginated=len(rows) > 20, pageSize=20)

    @mcp.tool(app=True)
    def data_app(
        namespace: str | None = None,
        filter: Optional[dict] = None,
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

        Use when the user wants to *see* and explore datastore content (e.g. a
        watch-list) without leaving the chat. For raw JSON use `data_rows`; to
        edit a row, follow the dashboard link shown on the card.

        Args:
            namespace: target namespace ; omit = list all your namespaces.
            filter: dict `{column: value}` exact match to pre-filter rows,
                e.g. `{"priorite": "P1"}`.
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
        except NamespaceNotFound:
            return _message_card(
                "Namespace introuvable",
                f"Aucun namespace « {namespace} » sur ton compte.",
            )
        suffix = f" (filtre {filter})" if filter else ""
        with Card() as card:
            with Column(gap=4):
                Heading(namespace)
                Text(f"{len(rows)} ligne(s){suffix} · éditer : {url}")
                if rows:
                    _rows_table(rows, show_meta=show_meta)
                elif filter:
                    Text("Aucune ligne pour ce filtre.")
                else:
                    Text("Aucune ligne.")
        return card
