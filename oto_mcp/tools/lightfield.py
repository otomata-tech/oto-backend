"""Lightfield — CRM agent-native : comptes, contacts, opportunités, listes, notes,
tâches, réunions, emails.

Wrappe `oto.tools.lightfield.client.LightfieldClient` (API v1, Bearer `sk_lf_…`).
keyed `api_key`, byo-only : chaque organisation pose SA clé, sur SON workspace — il
n'y a pas de clé oto partagée, et il ne peut pas y en avoir (les données sont celles
du client).

**Neuf outils, un par objet métier**, verbe en `op=`. Le regroupement suit l'objet
et non une catégorie vague : les 29 scopes de Lightfield sont eux-mêmes par objet ×
verbe, donc une frontière d'outil = une frontière de permission, et un scope manquant
fait échouer UN outil dont le message peut nommer le scope en cause.

⚠️ **Le modèle de champs est PROPRE À CHAQUE WORKSPACE.** Un enregistrement porte
`fields: {clé: {value, valueType}}` où les clés sont définies par le client, pas par
Lightfield. Aucune clé n'est écrite en dur ici : `op="definitions"` les découvre, et
toute écriture VALIDE ses clés contre les définitions AVANT d'appeler l'API — une clé
inconnue est refusée en nommant les clés valides, jamais avalée en 400.

⚠️ **`op="search"` et `op="get"` ne sont pas interchangeables.** La recherche sert un
index qui peut être en retard (doc éditeur) ; relire une écriture par la recherche
peut rendre l'état d'AVANT. Après une écriture, relire par `op="get"`.

Sorties PROJETÉES par défaut (id, date, lien, champs aplatis `clé: valeur`) ; les
relations sont écartées et la réponse le DIT (bloc `projection`). `full=True` rend
l'enregistrement brut. Un CRM rend des enregistrements gras : sans projection, une
recherche de 25 lignes noie le contexte de l'agent.

Écritures : `dry_run` partout, défaut `False` — SAUF l'envoi d'email, seul geste qui
sort de la plateforme et atteint une personne réelle, en dry-run par DÉFAUT.

Les appels au client sont écrits en clair (`_client().list_accounts(…)`) : c'est ce
qui les rend vérifiables par la sonde version-skew (`test_tools_client_methods_exist`).
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, connector_verify

# Scopes de lecture « cœur CRM » : une clé qui n'en a AUCUN authentifie mais ne peut
# rien faire d'utile — la sonde doit le dire plutôt que de rendre un vert trompeur.
_CORE_READ_SCOPES = ("accounts:read", "contacts:read", "opportunities:read")

# Écarté de la projection par défaut : gras, et rarement ce qu'on lit pour CHOISIR.
_DROPPED = ("relationships",)


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _upstream_message(e) -> str:
    status = e.status_code
    body = e.body if isinstance(e.body, dict) else {}
    code, param = body.get("code"), body.get("param")
    if status == 401:
        return ("Lightfield a rejeté la clé API (401) — vérifie la clé configurée sur "
                "ce connecteur (Lightfield : Settings → API keys).")
    if status == 403:
        return (f"Lightfield a refusé l'accès (403{f', {code}' if code else ''}) — la clé "
                "existe mais il lui manque le scope de cette opération. Les scopes se "
                "choisissent à la CRÉATION de la clé : il faut en créer une nouvelle "
                "avec le scope manquant, on ne peut pas l'ajouter après coup.")
    if status == 404:
        return "Lightfield : ressource introuvable (404) — vérifie l'identifiant."
    if status == 409:
        return ("Lightfield : conflit (409) — l'enregistrement a changé entre-temps. "
                "Relis-le avec op='get' puis réessaie sur l'état frais.")
    if status in (400, 422):
        if code in ("unknown_field", "unknown_relationship"):
            return (f"Lightfield ne connaît pas ce champ dans CE workspace "
                    f"({code}{f' : {param}' if param else ''}). Les champs sont propres "
                    "à chaque workspace : appelle op='definitions' sur cet objet pour "
                    "lire les clés valides.")
        return (f"Lightfield a refusé la requête (HTTP {status}"
                f"{f', {code}' if code else ''}) : {e.body}")
    if status == 429:
        return "Lightfield : trop de requêtes (429) — réessaie dans un instant."
    if status in (500, 502, 503, 504):
        return f"Lightfield est momentanément indisponible (HTTP {status}) — réessaie plus tard."
    return f"Lightfield a refusé la requête (HTTP {status}): {e.body}"


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """Sonde « tester la connexion » : `/auth/validate` (gratuit, AUCUN scope requis)
    — puis on exige au moins un scope de lecture cœur.

    Tester l'auth seule rendrait un vert trompeur : les scopes de Lightfield se
    choisissent à la création de la clé, donc une clé parfaitement valide mais cochée
    sans `accounts/contacts/opportunities:read` répond 200 ici et échouera sur CHAQUE
    appel réel. C'est la leçon Zoho, à l'identique.
    """
    from oto.tools.lightfield.client import LightfieldClient, scope_granted
    info = LightfieldClient(api_key=fields["key"]).validate()
    if not info.get("active"):
        raise ValueError("Lightfield indique que cette clé n'est pas active.")
    if not any(scope_granted(info, s) for s in _CORE_READ_SCOPES):
        granted = info.get("scopes") or []
        raise ValueError(
            "La clé est valide mais ne porte aucun scope de lecture CRM "
            f"(accordés : {granted or 'aucun'}). Recrée une clé Lightfield en cochant "
            "au moins accounts:read, contacts:read ou opportunities:read — les scopes "
            "se choisissent à la création et ne s'ajoutent pas après coup.")


def _flatten(record: Any) -> Any:
    """`fields: {clé: {value, valueType}}` → `fields: {clé: valeur}`, relations
    écartées. Le `valueType` est un détail de transport : l'agent veut la valeur."""
    if not isinstance(record, dict):
        return record
    out = {k: v for k, v in record.items() if k not in _DROPPED and k != "fields"}
    raw = record.get("fields")
    if isinstance(raw, dict):
        out["fields"] = {
            k: (v.get("value") if isinstance(v, dict) else v) for k, v in raw.items()
        }
    return out


def _project(payload: Any, full: bool) -> Any:
    """`full=True` → payload INCHANGÉ. Sinon on aplatit et on NOMME ce qui manque :
    une sortie amputée en silence fait croire à l'agent qu'il a tout lu."""
    if full or not isinstance(payload, dict):
        return payload
    data = payload.get("data")
    if isinstance(data, list):
        out = dict(payload)
        out["data"] = [_flatten(r) for r in data]
    elif isinstance(data, dict):
        out = dict(payload)
        out["data"] = _flatten(data)
    else:
        out = _flatten(payload)
    out["projection"] = {
        "dropped": list(_DROPPED),
        "flattened": "fields[].value",
        "how_to_get_everything": "full=True",
    }
    return out


def _field_keys(definitions: Any) -> set:
    """Les clés de champ déclarées par CE workspace (`fieldDefinitions` est une MAP
    clé → définition)."""
    if not isinstance(definitions, dict):
        return set()
    defs = definitions.get("fieldDefinitions")
    return set(defs) if isinstance(defs, dict) else set()


def _check_fields(payload: dict, definitions: Any, obj: str) -> None:
    """Refuse une clé de champ que CE workspace ne déclare pas, en nommant les clés
    valides. Sans ça l'API rend un 400 `unknown_field` — exact, mais muet sur ce qui
    AURAIT marché, et l'agent réessaie au hasard."""
    fields = payload.get("fields")
    if not isinstance(fields, dict) or not fields:
        return
    known = _field_keys(definitions)
    if not known:                      # définitions illisibles : ne pas bloquer
        return
    unknown = sorted(set(fields) - known)
    if unknown:
        raise _bad(
            f"Champs inconnus sur `{obj}` dans ce workspace : {unknown}. "
            f"Clés valides : {sorted(known)}. "
            "Les champs sont propres à chaque workspace Lightfield.")


# `filters` est SPLATÉ à côté de `limit`/`offset` : une clé de filtre portant l'un de
# ces deux noms lèverait un TypeError (« multiple values for keyword argument ») que
# `_run` ne traduit pas — l'agent recevrait une erreur interne opaque au lieu d'un refus
# qui nomme le paramètre dédié.
_RESERVED_FILTERS = ("limit", "offset")


def _check_filters(filters: Any) -> None:
    clash = sorted(k for k in (filters or {}) if k in _RESERVED_FILTERS)
    if clash:
        raise _bad(f"`filters` ne peut pas porter {clash} : la pagination passe par les "
                   "paramètres dédiés `limit` et `offset` de l'outil.")


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.lightfield.client import LightfieldClient

    connector_verify.register("lightfield", _verify)

    def _client() -> LightfieldClient:
        key, _ = access.resolve_api_key("lightfield")
        return LightfieldClient(api_key=key)

    def _run(fn):
        """Traduit un refus de Lightfield en erreur d'outil actionnable."""
        try:
            return fn()
        except ValueError as e:
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))

    # --- fabrique des trois objets « cœur » ---------------------------------
    # comptes / contacts / opportunités ont EXACTEMENT la même surface : une
    # fabrique évite trois copies qui divergeraient au premier correctif.

    def _core_object(obj: str, list_fn, get_fn, create_fn, update_fn, defs_fn):
        def handler(op, record_id, fields, filters, limit, offset, dry_run, full):
            if op == "definitions":
                return _run(defs_fn)
            if op == "search":
                _check_filters(filters)
                return _project(
                    _run(lambda: list_fn(limit=limit, offset=offset, **(filters or {}))),
                    full)
            if op == "get":
                if not record_id:
                    raise _bad(f"op='get' : `record_id` requis (id d'un {obj}).")
                return _project(_run(lambda: get_fn(record_id)), full)
            if op == "upsert":
                if not isinstance(fields, dict) or not fields:
                    raise _bad("op='upsert' : `fields` requis "
                               "(clé → valeur, clés lues par op='definitions').")
                payload = {"fields": fields}
                _check_fields(payload, _run(defs_fn), obj)
                if dry_run:
                    return {"dry_run": True,
                            "would": "update" if record_id else "create",
                            "record_id": record_id, "payload": payload}
                if record_id:
                    return _project(_run(lambda: update_fn(record_id, payload)), full)
                return _project(_run(lambda: create_fn(payload)), full)
            raise _bad(f"`op` invalide : {op!r} "
                       "(attendu : search | get | upsert | definitions).")
        return handler

    _accounts = _core_object(
        "compte",
        lambda **kw: _client().list_accounts(**kw),
        lambda i: _client().get_account(i),
        lambda p: _client().create_account(p),
        lambda i, p: _client().update_account(i, p),
        lambda: _client().account_definitions())
    _contacts = _core_object(
        "contact",
        lambda **kw: _client().list_contacts(**kw),
        lambda i: _client().get_contact(i),
        lambda p: _client().create_contact(p),
        lambda i, p: _client().update_contact(i, p),
        lambda: _client().contact_definitions())
    _opportunities = _core_object(
        "opportunité",
        lambda **kw: _client().list_opportunities(**kw),
        lambda i: _client().get_opportunity(i),
        lambda p: _client().create_opportunity(p),
        lambda i, p: _client().update_opportunity(i, p),
        lambda: _client().opportunity_definitions())

    @mcp.tool()
    def lightfield_accounts(
        op: Literal["search", "get", "upsert", "definitions"] = "search",
        record_id: Optional[str] = None,
        fields: Optional[dict] = None,
        filters: Optional[dict] = None,
        limit: int = 25,
        offset: Optional[int] = None,
        dry_run: bool = False,
        full: bool = False,
    ) -> dict:
        """Lightfield accounts — the COMPANIES in the CRM: search them, read one, create
        or update one, or discover the field keys of this workspace.

        `op`:
        - **"search"** (default): filtered list. WARNING: served from a search index
          that may LAG behind recent writes — after writing, read back with op="get".
        - **"get"**: one record by `record_id`, read DIRECTLY (always current).
        - **"upsert"**: creates when `record_id` is omitted, updates when given.
          Field keys are validated against this workspace's definitions BEFORE the
          call; an unknown key is refused and the valid ones are named.
        - **"definitions"**: the field and relationship keys THIS workspace declares.
          Call it before your first write — keys are per-workspace, never universal.

        Args:
            op: "search" | "get" | "upsert" | "definitions".
            record_id: op="get" (required) / op="upsert" (present = update).
            fields: op="upsert" — `{field_key: value}`, keys from op="definitions".
            filters: op="search" — `{field_key: value}` filters, and relationship
                filters keyed by relationship slug.
            limit: op="search" — 1..25 (the API caps at 25); paginate with `offset`.
            offset: op="search" — start of the page.
            dry_run: op="upsert" — validate and echo the payload, write nothing.
            full: return the raw record (relationships, valueType) instead of the
                projected one.
        """
        return _accounts(op, record_id, fields, filters, limit, offset, dry_run, full)

    @mcp.tool()
    def lightfield_contacts(
        op: Literal["search", "get", "upsert", "definitions"] = "search",
        record_id: Optional[str] = None,
        fields: Optional[dict] = None,
        filters: Optional[dict] = None,
        limit: int = 25,
        offset: Optional[int] = None,
        dry_run: bool = False,
        full: bool = False,
    ) -> dict:
        """Lightfield contacts — the PEOPLE in the CRM: search them, read one, create or
        update one, or discover the field keys of this workspace.

        `op`:
        - **"search"** (default): filtered list. WARNING: served from a search index
          that may LAG behind recent writes — after writing, read back with op="get".
        - **"get"**: one record by `record_id`, read DIRECTLY (always current).
        - **"upsert"**: creates when `record_id` is omitted, updates when given.
          Field keys are validated against this workspace's definitions BEFORE the
          call; an unknown key is refused and the valid ones are named.
        - **"definitions"**: the field and relationship keys THIS workspace declares.
          Call it before your first write — keys are per-workspace, never universal.

        Args:
            op: "search" | "get" | "upsert" | "definitions".
            record_id: op="get" (required) / op="upsert" (present = update).
            fields: op="upsert" — `{field_key: value}`, keys from op="definitions".
            filters: op="search" — `{field_key: value}` filters, and relationship
                filters keyed by relationship slug.
            limit: op="search" — 1..25 (the API caps at 25); paginate with `offset`.
            offset: op="search" — start of the page.
            dry_run: op="upsert" — validate and echo the payload, write nothing.
            full: return the raw record (relationships, valueType) instead of the
                projected one.
        """
        return _contacts(op, record_id, fields, filters, limit, offset, dry_run, full)

    @mcp.tool()
    def lightfield_opportunities(
        op: Literal["search", "get", "upsert", "definitions"] = "search",
        record_id: Optional[str] = None,
        fields: Optional[dict] = None,
        filters: Optional[dict] = None,
        limit: int = 25,
        offset: Optional[int] = None,
        dry_run: bool = False,
        full: bool = False,
    ) -> dict:
        """Lightfield opportunities — the DEALS in the CRM: search them, read one, create
        or update one, or discover the field keys of this workspace.

        `op`:
        - **"search"** (default): filtered list. WARNING: served from a search index
          that may LAG behind recent writes — after writing, read back with op="get".
        - **"get"**: one record by `record_id`, read DIRECTLY (always current).
        - **"upsert"**: creates when `record_id` is omitted, updates when given.
          Field keys are validated against this workspace's definitions BEFORE the
          call; an unknown key is refused and the valid ones are named.
        - **"definitions"**: the field and relationship keys THIS workspace declares.
          Call it before your first write — keys are per-workspace, never universal.

        Args:
            op: "search" | "get" | "upsert" | "definitions".
            record_id: op="get" (required) / op="upsert" (present = update).
            fields: op="upsert" — `{field_key: value}`, keys from op="definitions".
            filters: op="search" — `{field_key: value}` filters, and relationship
                filters keyed by relationship slug.
            limit: op="search" — 1..25 (the API caps at 25); paginate with `offset`.
            offset: op="search" — start of the page.
            dry_run: op="upsert" — validate and echo the payload, write nothing.
            full: return the raw record (relationships, valueType) instead of the
                projected one.
        """
        return _opportunities(op, record_id, fields, filters, limit, offset, dry_run, full)

    # --- listes -------------------------------------------------------------

    @mcp.tool()
    def lightfield_lists(
        op: Literal["list", "get", "members", "upsert"] = "list",
        list_id: Optional[str] = None,
        of: Literal["accounts", "contacts", "opportunities"] = "accounts",
        fields: Optional[dict] = None,
        limit: int = 25,
        offset: Optional[int] = None,
        dry_run: bool = False,
        full: bool = False,
    ) -> dict:
        """Lightfield lists — the curated selections of accounts, contacts or deals.

        `op`:
        - **"list"** (default): the lists of the workspace.
        - **"get"**: one list by `list_id`.
        - **"members"**: what is IN the list — `of` picks accounts | contacts |
          opportunities. ⚠️ Needs TWO scopes: `lists:read` AND the read scope of the
          member type, so this can fail where op="get" succeeds.
        - **"upsert"**: creates when `list_id` is omitted, updates when given.

        Args:
            op: "list" | "get" | "members" | "upsert".
            list_id: op="get" / "members" (required) / "upsert" (present = update).
            of: op="members" — which member type to read.
            fields: op="upsert" — `{field_key: value}` (e.g. the list name).
            limit: 1..25 (the API caps at 25); paginate with `offset`.
            offset: start of the page.
            dry_run: op="upsert" — validate and echo the payload, write nothing.
            full: raw records instead of projected ones.
        """
        if op == "list":
            return _project(_run(lambda: _client().list_lists(limit=limit, offset=offset)),
                            full)
        if op == "get":
            if not list_id:
                raise _bad("op='get' : `list_id` requis.")
            return _project(_run(lambda: _client().get_list(list_id)), full)
        if op == "members":
            if not list_id:
                raise _bad("op='members' : `list_id` requis.")
            # Lambdas et non méthodes liées : la forme liée construisait les TROIS
            # clients (donc trois `resolve_api_key` — lecture du coffre + déchiffrement)
            # pour n'en garder qu'un. L'appel reste écrit EN CLAIR, seule forme que la
            # sonde version-skew sait lire.
            fn = {
                "accounts": lambda: _client().list_accounts_of_list(
                    list_id, limit=limit, offset=offset),
                "contacts": lambda: _client().list_contacts_of_list(
                    list_id, limit=limit, offset=offset),
                "opportunities": lambda: _client().list_opportunities_of_list(
                    list_id, limit=limit, offset=offset),
            }[of]
            return _project(_run(fn), full)
        if op == "upsert":
            if not isinstance(fields, dict) or not fields:
                raise _bad("op='upsert' : `fields` requis.")
            payload = {"fields": fields}
            if dry_run:
                return {"dry_run": True, "would": "update" if list_id else "create",
                        "list_id": list_id, "payload": payload}
            if list_id:
                return _project(_run(lambda: _client().update_list(list_id, payload)), full)
            return _project(_run(lambda: _client().create_list(payload)), full)
        raise _bad(f"`op` invalide : {op!r} (attendu : list | get | members | upsert).")

    # --- notes & tâches -----------------------------------------------------

    @mcp.tool()
    def lightfield_notes(
        op: Literal["create", "definitions"] = "create",
        fields: Optional[dict] = None,
        dry_run: bool = False,
        full: bool = False,
    ) -> dict:
        """Lightfield notes — write a note onto the CRM (attached to an account,
        contact or opportunity through a relationship field).

        `op`:
        - **"create"** (default): writes the note. Field keys validated against this
          workspace's definitions first.
        - **"definitions"**: the field and relationship keys notes accept here —
          including how to attach the note to a record.

        Args:
            op: "create" | "definitions".
            fields: op="create" — `{field_key: value}`, keys from op="definitions".
            dry_run: validate and echo the payload, write nothing.
            full: raw record instead of the projected one.
        """
        if op == "definitions":
            return _run(lambda: _client().note_definitions())
        if op != "create":
            raise _bad(f"`op` invalide : {op!r} (attendu : create | definitions).")
        if not isinstance(fields, dict) or not fields:
            raise _bad("op='create' : `fields` requis "
                       "(clé → valeur, clés lues par op='definitions').")
        payload = {"fields": fields}
        _check_fields(payload, _run(lambda: _client().note_definitions()), "note")
        if dry_run:
            return {"dry_run": True, "would": "create", "payload": payload}
        return _project(_run(lambda: _client().create_note(payload)), full)

    @mcp.tool()
    def lightfield_tasks(
        op: Literal["upsert", "definitions"] = "upsert",
        record_id: Optional[str] = None,
        fields: Optional[dict] = None,
        dry_run: bool = False,
        full: bool = False,
    ) -> dict:
        """Lightfield tasks — create or update a task in the CRM.

        `op`:
        - **"upsert"** (default): creates when `record_id` is omitted, updates when
          given. Field keys validated against this workspace's definitions first.
        - **"definitions"**: the field and relationship keys tasks accept here.

        Args:
            op: "upsert" | "definitions".
            record_id: present = update that task, omitted = create a new one.
            fields: `{field_key: value}`, keys from op="definitions".
            dry_run: validate and echo the payload, write nothing.
            full: raw record instead of the projected one.
        """
        if op == "definitions":
            return _run(lambda: _client().task_definitions())
        if op != "upsert":
            raise _bad(f"`op` invalide : {op!r} (attendu : upsert | definitions).")
        if not isinstance(fields, dict) or not fields:
            raise _bad("op='upsert' : `fields` requis.")
        payload = {"fields": fields}
        _check_fields(payload, _run(lambda: _client().task_definitions()), "tâche")
        if dry_run:
            return {"dry_run": True, "would": "update" if record_id else "create",
                    "record_id": record_id, "payload": payload}
        if record_id:
            return _project(_run(lambda: _client().update_task(record_id, payload)), full)
        return _project(_run(lambda: _client().create_task(payload)), full)

    # --- réunions -----------------------------------------------------------

    @mcp.tool()
    def lightfield_meetings(
        op: Literal["search", "get", "definitions"] = "search",
        record_id: Optional[str] = None,
        filters: Optional[dict] = None,
        limit: int = 25,
        offset: Optional[int] = None,
        full: bool = False,
    ) -> dict:
        """Lightfield meetings — READ the meetings captured in the CRM.

        `op`:
        - **"search"** (default): filtered list (search index, may lag).
        - **"get"**: one meeting by `record_id`, read directly.
        - **"definitions"**: the field keys meetings carry here.

        Args:
            op: "search" | "get" | "definitions".
            record_id: op="get" — the meeting.
            filters: op="search" — `{field_key: value}` filters.
            limit: 1..25 (the API caps at 25); paginate with `offset`.
            offset: start of the page.
            full: raw records instead of projected ones.
        """
        if op == "definitions":
            return _run(lambda: _client().meeting_definitions())
        if op == "search":
            _check_filters(filters)
            return _project(_run(lambda: _client().list_meetings(
                limit=limit, offset=offset, **(filters or {}))), full)
        if op == "get":
            if not record_id:
                raise _bad("op='get' : `record_id` requis.")
            return _project(_run(lambda: _client().get_meeting(record_id)), full)
        raise _bad(f"`op` invalide : {op!r} (attendu : search | get | definitions).")

    # --- emails -------------------------------------------------------------

    @mcp.tool()
    def lightfield_emails(
        op: Literal["search", "get", "send", "draft"] = "search",
        record_id: Optional[str] = None,
        sender: Optional[str] = None,
        to: Optional[list[str]] = None,
        cc: Optional[list[str]] = None,
        bcc: Optional[list[str]] = None,
        subject: Optional[str] = None,
        body: Optional[str] = None,
        filters: Optional[dict] = None,
        limit: int = 25,
        offset: Optional[int] = None,
        dry_run: bool = True,
        full: bool = False,
    ) -> dict:
        """Lightfield emails — read the synced mail, or send a new one from a
        connected mailbox.

        `op`:
        - **"search"** (default): synced emails (search index, may lag).
        - **"get"**: one email by `record_id`.
        - **"send"**: SENDS A REAL EMAIL from `sender`. ⚠️ `dry_run` defaults to
          **True** here — the only tool of this connector whose effect leaves the
          platform and reaches a person. Pass `dry_run=False` deliberately to send.
        - **"draft"**: writes a draft into the mailbox of `sender`; nothing is sent.

        ⚠️ `sender` must be a mailbox CONNECTED to Lightfield (Google or Microsoft)
        and owned by the API key's user. Without one the API refuses and nothing
        leaves.

        ⚠️ Lightfield cannot reply or forward: a send always creates a NEW message,
        never a reply in an existing thread — quote the context yourself.

        Args:
            op: "search" | "get" | "send" | "draft".
            record_id: op="get" — the email.
            sender: op="send"/"draft" — the connected mailbox address (required).
            to: op="send"/"draft" — recipients.
            cc: carbon copy.
            bcc: blind carbon copy.
            subject: the subject line.
            body: the message body (text).
            filters: op="search" — `{field_key: value}` filters.
            limit: 1..25 (the API caps at 25); paginate with `offset`.
            offset: start of the page.
            dry_run: op="send"/"draft" — echo the payload, send nothing.
                DEFAULTS TO TRUE on this tool.
            full: raw records instead of projected ones.
        """
        if op == "search":
            _check_filters(filters)
            return _project(_run(lambda: _client().list_emails(
                limit=limit, offset=offset, **(filters or {}))), full)
        if op == "get":
            if not record_id:
                raise _bad("op='get' : `record_id` requis.")
            return _project(_run(lambda: _client().get_email(record_id)), full)
        if op not in ("send", "draft"):
            raise _bad(f"`op` invalide : {op!r} (attendu : search | get | send | draft).")
        if not sender or not sender.strip():
            raise _bad(f"op='{op}' : `sender` requis — l'adresse d'une boîte mail "
                       "CONNECTÉE à Lightfield (Google ou Microsoft) appartenant au "
                       "propriétaire de la clé API. Sans elle, rien ne peut partir.")
        payload: dict = {"from": sender.strip()}
        for k, v in (("to", to), ("cc", cc), ("bcc", bcc)):
            if v:
                payload[k] = list(v)
        if subject:
            payload["subject"] = subject
        if body:
            payload["messageBody"] = {"content": body}
        if op == "send" and not payload.get("to"):
            raise _bad("op='send' : `to` requis (au moins un destinataire).")
        if dry_run:
            return {"dry_run": True, "would": op, "payload": payload,
                    "note": ("Rien n'est parti. Repasse avec dry_run=False pour "
                             + ("envoyer." if op == "send" else "écrire le brouillon."))}
        if op == "send":
            return _run(lambda: _client().send_email(payload))
        return _run(lambda: _client().draft_email(payload))

    # --- types d'objets -----------------------------------------------------

    @mcp.tool()
    def lightfield_objects(
        op: Literal["list", "definitions"] = "list",
        object_type: Optional[str] = None,
    ) -> dict:
        """Lightfield object types — what kinds of records THIS workspace has,
        including the custom ones, and the field keys of any of them.

        `op`:
        - **"list"** (default): `{data: [{label, objectType}]}` — `objectType` is the
          slug to pass back as `object_type`.
        - **"definitions"**: the field and relationship keys of `object_type`. Use it
          for custom objects; the standard ones have op="definitions" on their own
          tool.

        Args:
            op: "list" | "definitions".
            object_type: op="definitions" — the slug from op="list".
        """
        if op == "list":
            return _run(lambda: _client().list_object_types())
        if op != "definitions":
            raise _bad(f"`op` invalide : {op!r} (attendu : list | definitions).")
        if not object_type:
            raise _bad("op='definitions' : `object_type` requis (slug lu par op='list').")
        return _run(lambda: _client().object_definitions(object_type))
