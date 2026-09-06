"""PostHog — analytics produit : HogQL, events, personnes, comptes, insights,
feature flags, session recordings.

Wrappe `oto.tools.posthog.client.PostHogClient`. Credential à TROIS champs
(`secret_kind="fields"`, résolu par `access.resolve_credential_fields`) :

- `api_key` — la clé **personnelle** `phx_…`. ⚠️ La clé de PROJET `phc_…`, celle
  que PostHog met le plus en avant, est refusée par l'API de lecture ; le client
  la rejette à la construction avec le message qui dit où prendre la bonne.
- `host` — NON secret : `https://us.posthog.com` (défaut) ou
  `https://eu.posthog.com`, ou une instance auto-hébergée. La région fait partie
  de l'adresse ; une clé US est inconnue côté EU (le symptôme est un 401, pas un
  message de région).
- `project_id` — NON secret, facultatif : épingle la clé sur UN projet. Omis, le
  projet est découvert depuis la clé.

**byo-only** : ce sont les données produit du client, pas de clé oto partagée.

**Huit tools**, verbe en `op=` (ADR 0047). Aucun paramètre n'est retenu au
silence (`_refuse_ignored`).

**Rien ne change le produit.** Créer ou basculer un feature flag, écrire un
insight, supprimer une personne ou un enregistrement n'existent pas sur le
client — pas seulement « non exposés ici ». Basculer un flag modifie le produit
pour de vrais utilisateurs ; supprimer une personne est irréversible et
réglementaire. La SEULE écriture est l'annotation, purement additive.

⚠️ **La réponse brute de `/query/` est à 93 % du bruit** : mesurée à 2 525
caractères pour un résultat de deux cellules, dont 1 156 de `modifiers` et 510
de SQL ClickHouse généré. `_projeter` la réduit aux champs utiles (175 car. sur
le même appel) — sans quoi chaque requête mangerait le budget de réponse
(cf. `docs/conventions.md`).

**Testé en live le 2026-08-22** contre un vrai projet PostHog Cloud US
(organisation cliente réelle) : identité, découverte de projet, HogQL,
requêtes typées, ré-exécution d'un insight sauvegardé, schéma (156 tables,
`events` à 52 colonnes), les 14 familles de ressources et l'écriture
d'annotation répondent comme codé. `groups_types` rend une liste NUE (pas
l'enveloppe `{results}`), et `/events/` comme `/persons/` ne portent PAS de
`count` — trois formes qui ne se déduisent pas de la doc.
"""
from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, egress
from ..connectors import verify as connector_verify


def _check_host(host) -> None:
    """Garde d'egress sur l'hôte PostHog, quand il est posé.

    Vide = le SaaS, une constante de la lib. Renseigné, il désigne une instance
    auto-hébergée — donc potentiellement un hôte du réseau interne de la
    plateforme (`oto_mcp/egress.py`)."""
    valeur = (host or "").strip()
    if valeur:
        egress.check_url(valeur, connector="posthog", field="host")


_DEFAULT_LIMIT = 100

# Les champs de la réponse `/query/` qui portent une information ; tout le reste
# (modifiers, clickhouse, cache_key, timings…) est du diagnostic interne.
_QUERY_KEEP = ("columns", "types", "results", "hasMore", "hogql", "error")


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _refuse_ignored(op: str, hint: str, **provided) -> None:
    for name, value in provided.items():
        if value is not None:
            raise _bad(f"op={op!r} n'utilise pas `{name}` — {hint}")


def _projeter(reponse: Any) -> Any:
    """Réduit une réponse `/query/` aux champs utiles (cf. docstring du module)."""
    if not isinstance(reponse, dict):
        return reponse
    out = {k: reponse[k] for k in _QUERY_KEEP if k in reponse and reponse[k] is not None}
    if reponse.get("hasMore"):
        out["note"] = ("Résultat TRONQUÉ par PostHog. Ajoute un LIMIT explicite, "
                       "agrège côté requête, ou restreins la fenêtre.")
    return out or reponse


def _upstream_message(e) -> str:
    status = e.status_code
    body = e.body if isinstance(e.body, dict) else {}
    detail = body.get("detail") or body.get("error") or ""
    code = body.get("code")
    if code == "hogql_query_error" or status == 400:
        # Le message de PostHog nomme le champ fautif et sa position : il est plus
        # utile à l'agent que n'importe quelle reformulation.
        errs = ((body.get("extra") or {}).get("hogql_metadata") or {}).get("errors") or []
        pos = f" (position {errs[0].get('start')}-{errs[0].get('end')})" if errs else ""
        return (f"PostHog a refusé la requête{pos} : {detail} — vérifie les noms de "
                f"tables et de colonnes avec `posthog_schema`.")
    if status in (401, 403):
        return (f"PostHog a rejeté la clé (HTTP {status}) : {detail} — vérifie (1) que "
                f"c'est bien une clé PERSONNELLE `phx_…` et non une clé de projet "
                f"`phc_…`, (2) la RÉGION configurée (us/eu — une clé d'une région est "
                f"inconnue de l'autre), (3) les scopes de la clé.")
    if status == 404:
        return f"PostHog : ressource introuvable — {detail}"
    if status == 429:
        return "PostHog : trop de requêtes (429) — réessaie dans un instant."
    if status >= 500:
        return f"PostHog est momentanément indisponible (HTTP {status}) — réessaie plus tard."
    return f"PostHog a refusé la requête (HTTP {status}) : {detail}"


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion » : identité → projet → requête.

    Une clé PostHog porte des SCOPES choisis à sa création : une clé sans
    `query:read` s'authentifie parfaitement puis échoue au premier appel réel.
    Se contenter d'un GET d'identité dirait « OK » là où l'outil phare est
    inutilisable — d'où les trois étapes, la dernière exerçant vraiment `/query/`.
    """
    from oto.tools.posthog.client import PostHogClient
    cfg = config or {}
    _check_host(cfg.get("host"))
    client = PostHogClient(fields["api_key"], host=cfg.get("host") or None,
                           project_id=cfg.get("project_id") or None)
    client.current_user()
    project = client.resolve_project_id()
    try:
        client.query("SELECT 1")
    except Exception as e:  # noqa: BLE001 — le message d'exception EST le retour d'erreur
        raise RuntimeError(
            f"La clé authentifie bien et voit le projet {project}, mais ne peut pas "
            f"exécuter de requête — il lui manque probablement le scope `query:read`. "
            f"Détail : {e}") from e


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.posthog.client import PostHogClient

    connector_verify.register("posthog", _verify)

    def _client() -> PostHogClient:
        fields = access.resolve_credential_fields("posthog")
        _check_host(fields.get("host"))
        return PostHogClient(fields["api_key"], host=fields.get("host") or None,
                             project_id=fields.get("project_id") or None)

    def _run(fn):
        try:
            return fn()
        except ValueError as e:
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))

    # ================================================================
    # La requête — HogQL libre OU type nommé
    # ================================================================

    @mcp.tool()
    def posthog_query(
        hogql: Optional[str] = None,
        query: Optional[Dict[str, Any]] = None,
        project_id: Optional[str] = None,
    ) -> object:
        """Run an analytics query — free HogQL, or one of PostHog's named query
        kinds. Pass exactly one of `hogql` or `query`.

        ⚠️ **Use `query` (`FunnelsQuery`, `RetentionQuery`) — not `hogql` — for
        funnels and retention.** PostHog's
        funnel semantics — ordered vs unordered steps, conversion window,
        exclusion steps, attribution — do not survive being rewritten as SQL.
        Hand-rolled HogQL returns a plausible number that disagrees with the
        number on the team's own dashboard, and nothing signals the error. Better
        still, when the team already built the chart, call
        `posthog_insight(op="run", insight_id=…)` instead of this tool.

        **HogQL dialect** — ClickHouse SQL with PostHog accessors. Get these
        wrong and the query returns a confidently wrong number:
        - properties: `properties.$browser`, `person.properties.email` — NOT
          `JSONExtract(...)`. Values are strings, so compare numbers with
          `toFloat(properties.amount) > 10`.
        - the event-name column is `event` (not `event_name`); time is
          `timestamp`, filtered like `timestamp >= now() - INTERVAL 7 DAY`.
        - unique users is `uniq(person_id)` — never
          `count(distinct distinct_id)`, which counts devices, not people.
        - joinable tables: `events`, `persons`, `sessions`, `groups`. Discover
          them and their columns with `posthog_schema` BEFORE writing a query.

        PostHog caps an unbounded query at 101 rows and sets `hasMore`; add your
        own `LIMIT`, or aggregate in the query rather than paginating.

        Args:
            hogql: a HogQL (SQL) string. Best for counts, breakdowns, joins and
                anything ad-hoc.
            query: a full PostHog query object with its own `kind` —
                `TrendsQuery`, `FunnelsQuery`, `RetentionQuery`,
                `StickinessQuery`, `LifecycleQuery`.
            project_id: target another project than the configured default.
        """
        if bool(hogql) == bool(query):
            raise _bad("Fournis EXACTEMENT un de `hogql` (SQL libre) ou `query` (objet "
                       "typé avec son `kind`) — pas les deux, pas aucun.")
        client = _client()
        if hogql:
            return _run(lambda: _projeter(client.query(hogql, project_id=project_id)))
        return _run(lambda: _projeter(client.run_query(query, project_id=project_id)))

    # ================================================================
    # Le vocabulaire du projet — sans quoi aucune requête n'est écrivable
    # ================================================================

    @mcp.tool()
    def posthog_schema(
        op: Literal["tables", "columns", "events", "properties", "values"] = "tables",
        table: Optional[str] = None,
        property_key: Optional[str] = None,
        search: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> object:
        """What this project actually contains — read this BEFORE writing HogQL.

        "tables" returns names only, and "columns" one table at a time, on
        purpose: the full schema is ~156 tables and `events` alone has 52
        columns (measured), which would swamp the response budget in one call.

        An empty "events" result means the project has no data yet — say so
        rather than writing a query that will correctly return zero.

        Args:
            op: "tables" (default, the queryable table names) | "columns" (one
                table's columns — REQUIRES `table`) | "events" (event types seen
                in this project) | "properties" (known properties) | "values"
                (observed values of one property — REQUIRES `property_key`).
            table: REQUIRED by "columns", e.g. "events", "persons", "sessions".
            property_key: REQUIRED by "values".
            search: filter for "events"/"properties".
            project_id: target another project.

        """
        client = _client()
        if op == "tables":
            _refuse_ignored(op, "utilise op='columns' pour les colonnes d'UNE table",
                            table=table, property_key=property_key, search=search)

            def _tables():
                schema = client.database_schema(project_id=project_id)
                tables = schema.get("tables") or {}
                return {"tables": sorted(tables),
                        "count": len(tables),
                        "note": "Colonnes d'une table : op='columns', table='events'."}
            return _run(_tables)
        if op == "columns":
            if not table:
                raise _bad("op='columns' requiert `table` (ex. 'events', 'persons')")
            _refuse_ignored(op, "ces champs ne s'appliquent pas à op='columns'",
                            property_key=property_key, search=search)

            def _columns():
                schema = client.database_schema(project_id=project_id)
                tables = schema.get("tables") or {}
                entry = tables.get(table)
                if entry is None:
                    raise _bad(f"Table {table!r} inconnue de ce projet — liste-les avec "
                               "op='tables'.")
                fields = entry.get("fields") or {}
                return {"table": table,
                        "columns": {n: f.get("type") for n, f in fields.items()},
                        "count": len(fields)}
            return _run(_columns)
        if op == "events":
            _refuse_ignored(op, "ces champs ne s'appliquent pas à op='events'",
                            table=table, property_key=property_key)
            return _run(lambda: client.list_event_definitions(
                project_id=project_id, search=search, limit=_DEFAULT_LIMIT))
        if op == "properties":
            _refuse_ignored(op, "ces champs ne s'appliquent pas à op='properties'",
                            table=table, property_key=property_key)
            return _run(lambda: client.list_property_definitions(
                project_id=project_id, search=search, limit=_DEFAULT_LIMIT))
        if op == "values":
            if not property_key:
                raise _bad("op='values' requiert `property_key`")
            _refuse_ignored(op, "ces champs ne s'appliquent pas à op='values'",
                            table=table, search=search)
            return _run(lambda: client.list_property_values(
                property_key, project_id=project_id))
        raise _bad("op doit être 'tables', 'columns', 'events', 'properties' ou 'values'")

    # ================================================================
    # Personnes & cohortes
    # ================================================================

    @mcp.tool()
    def posthog_person(
        op: Literal["list", "get", "activity", "cohorts", "cohort_persons"] = "list",
        person_id: Optional[str] = None,
        cohort_id: Optional[str] = None,
        search: Optional[str] = None,
        limit: Optional[int] = None,
        project_id: Optional[str] = None,
    ) -> object:
        """People tracked in the product, and the cohorts that segment them.

        ⚠️ `/persons/` returns no `count` (measured) — do not report a total
        from a page. To COUNT or aggregate people, use
        `posthog_query(hogql="SELECT uniq(person_id) FROM events WHERE …")`;
        paginating raw persons is slow and gives a wrong total.

        For B2B questions about ACCOUNTS rather than individuals, use
        `posthog_group` — persons cannot answer "which customers are churning".

        Args:
            op: "list" (default) | "get" | "activity" | "cohorts" |
                "cohort_persons".
            person_id: REQUIRED by "get"/"activity".
            cohort_id: REQUIRED by "cohort_persons"; filters "list".
            search: "list" — matches email, name or distinct_id.
            limit: page size (default 100). project_id: another project.

        """
        client = _client()
        if op == "list":
            _refuse_ignored(op, "utilise op='get' pour une personne précise",
                            person_id=person_id)
            return _run(lambda: client.list_persons(
                project_id=project_id, search=search, cohort=cohort_id,
                limit=limit or _DEFAULT_LIMIT))
        if op == "cohorts":
            _refuse_ignored(op, "la liste des cohortes ne prend pas ces filtres",
                            person_id=person_id, cohort_id=cohort_id, search=search)
            return _run(lambda: client.list_cohorts(
                project_id=project_id, limit=limit or _DEFAULT_LIMIT))
        if op == "cohort_persons":
            if not cohort_id:
                raise _bad("op='cohort_persons' requiert `cohort_id`")
            _refuse_ignored(op, "ces champs ne s'appliquent pas à op='cohort_persons'",
                            person_id=person_id, search=search)
            return _run(lambda: client.list_cohort_persons(
                cohort_id, project_id=project_id, limit=limit or _DEFAULT_LIMIT))
        if op in ("get", "activity"):
            if not person_id:
                raise _bad(f"op={op!r} requiert `person_id`")
            _refuse_ignored(op, "ces filtres ne s'appliquent qu'à op='list'",
                            cohort_id=cohort_id, search=search)
            if op == "get":
                return _run(lambda: client.get_person(person_id, project_id=project_id))
            return _run(lambda: client.list_person_activity(
                person_id, project_id=project_id, limit=limit or _DEFAULT_LIMIT))
        raise _bad("op inconnu pour posthog_person")

    # ================================================================
    # Groupes — l'analytics par COMPTE (B2B)
    # ================================================================

    @mcp.tool()
    def posthog_group(
        op: Literal["types", "list", "find"] = "types",
        group_type_index: Optional[int] = None,
        group_key: Optional[str] = None,
        search: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> object:
        """Group analytics — the ACCOUNT-level view (companies, workspaces),
        as opposed to individual people.

        Call op="types" first: without the index no group call is possible. An
        EMPTY result means this project does not do group analytics at all, in
        which case account-level questions ("which customers are churning") have
        no answer here — say so rather than silently answering per person.

        `groups` is also a joinable HogQL table, so once you know the index,
        `posthog_query` can aggregate by account.

        Args:
            op: "types" (default) | "list" | "find".
            group_type_index: REQUIRED by "list"/"find" — from op="types".
            group_key: REQUIRED by "find" — the business key of one group.
            search: "list" filter. project_id: another project.

        """
        client = _client()
        if op == "types":
            _refuse_ignored(op, "la liste des types ne prend pas ces filtres",
                            group_type_index=group_type_index, group_key=group_key,
                            search=search)

            def _types():
                types = client.list_group_types(project_id=project_id)
                if not types:
                    return {"group_types": [],
                            "note": ("Ce projet ne fait pas d'analytics de groupe : il n'y "
                                     "a pas de niveau COMPTE ici, seulement des personnes.")}
                return {"group_types": types}
            return _run(_types)
        if group_type_index is None:
            raise _bad(f"op={op!r} requiert `group_type_index` — lis-le avec op='types'")
        if op == "list":
            _refuse_ignored(op, "utilise op='find' pour un groupe précis", group_key=group_key)
            return _run(lambda: client.list_groups(
                group_type_index, project_id=project_id, search=search))
        if op == "find":
            if not group_key:
                raise _bad("op='find' requiert `group_key`")
            _refuse_ignored(op, "op='find' cible une clé précise", search=search)
            return _run(lambda: client.find_group(
                group_type_index, group_key, project_id=project_id))
        raise _bad("op doit être 'types', 'list' ou 'find'")

    # ================================================================
    # Insights & dashboards — le travail déjà fait par l'équipe
    # ================================================================

    @mcp.tool()
    def posthog_insight(
        op: Literal["list", "get", "run", "dashboards", "dashboard"] = "list",
        insight_id: Optional[str] = None,
        dashboard_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        search: Optional[str] = None,
        limit: Optional[int] = None,
        project_id: Optional[str] = None,
    ) -> object:
        """Saved insights and dashboards — the charts the team already built.

        **`op="run"` is the highest-trust answer in this connector.** It replays
        the insight's OWN query definition through PostHog's planner, so the
        number matches what the team sees in the UI — with an optional different
        date window. Always prefer it to rebuilding a funnel or retention chart
        in HogQL, which produces a plausible number that quietly disagrees.

        Insights saved in PostHog's older `filters` format cannot be replayed
        this way; "run" says so explicitly instead of failing obscurely.

        Args:
            op: "list" (default) | "get" | "run" | "dashboards" | "dashboard".
            insight_id: REQUIRED by "get"/"run".
            dashboard_id: REQUIRED by "dashboard".
            date_from/date_to: "run" only — override the saved window, in
                PostHog syntax (`-7d`, `-30d`, `mStart`, `yStart`, or an ISO
                date). Omitted = the window saved with the insight.
            search/limit: "list"/"dashboards". project_id: another project.

        """
        client = _client()
        if op == "list":
            _refuse_ignored(op, "utilise op='get' pour un insight précis",
                            insight_id=insight_id, dashboard_id=dashboard_id,
                            date_from=date_from, date_to=date_to)
            return _run(lambda: client.list_insights(
                project_id=project_id, search=search, limit=limit or _DEFAULT_LIMIT))
        if op == "dashboards":
            _refuse_ignored(op, "la liste des tableaux de bord ne prend pas ces filtres",
                            insight_id=insight_id, dashboard_id=dashboard_id,
                            date_from=date_from, date_to=date_to)
            return _run(lambda: client.list_dashboards(
                project_id=project_id, limit=limit or _DEFAULT_LIMIT))
        if op == "dashboard":
            if not dashboard_id:
                raise _bad("op='dashboard' requiert `dashboard_id`")
            _refuse_ignored(op, "ces champs ne s'appliquent pas à op='dashboard'",
                            insight_id=insight_id, date_from=date_from,
                            date_to=date_to, search=search)
            return _run(lambda: client.get_dashboard(dashboard_id, project_id=project_id))
        if op in ("get", "run"):
            if not insight_id:
                raise _bad(f"op={op!r} requiert `insight_id`")
            _refuse_ignored(op, "ces champs ne s'appliquent pas ici",
                            dashboard_id=dashboard_id, search=search)
            if op == "get":
                _refuse_ignored(op, "la fenêtre ne se remplace qu'à l'exécution (op='run')",
                                date_from=date_from, date_to=date_to)
                return _run(lambda: client.get_insight(insight_id, project_id=project_id))
            return _run(lambda: _projeter(client.run_insight(
                insight_id, date_from=date_from, date_to=date_to,
                project_id=project_id)))
        raise _bad("op inconnu pour posthog_insight")

    # ================================================================
    # Feature flags & expériences — LECTURE
    # ================================================================

    @mcp.tool()
    def posthog_flag(
        op: Literal["list", "get", "experiments", "experiment"] = "list",
        flag_id: Optional[str] = None,
        experiment_id: Optional[str] = None,
        limit: Optional[int] = None,
        project_id: Optional[str] = None,
    ) -> object:
        """Feature flags and experiments — read-only.

        Each flag carries its `key`, whether it is `active`, and its rollout
        conditions. Creating, editing or toggling a flag is deliberately not
        available: a toggle ships to real users, and that is not a decision an
        assistant should be able to take mid-conversation. Do it in PostHog.

        Args:
            op: "list" (default) | "get" | "experiments" | "experiment".
            flag_id: REQUIRED by "get". experiment_id: REQUIRED by "experiment".
            limit, project_id: as elsewhere.

        """
        client = _client()
        if op == "list":
            _refuse_ignored(op, "utilise op='get' pour un flag précis",
                            flag_id=flag_id, experiment_id=experiment_id)
            return _run(lambda: client.list_feature_flags(
                project_id=project_id, limit=limit or _DEFAULT_LIMIT))
        if op == "experiments":
            _refuse_ignored(op, "la liste des expériences ne prend pas ces filtres",
                            flag_id=flag_id, experiment_id=experiment_id)
            return _run(lambda: client.list_experiments(
                project_id=project_id, limit=limit or _DEFAULT_LIMIT))
        if op == "get":
            if not flag_id:
                raise _bad("op='get' requiert `flag_id`")
            _refuse_ignored(op, "op='get' cible un flag", experiment_id=experiment_id)
            return _run(lambda: client.get_feature_flag(flag_id, project_id=project_id))
        if op == "experiment":
            if not experiment_id:
                raise _bad("op='experiment' requiert `experiment_id`")
            _refuse_ignored(op, "op='experiment' cible une expérience", flag_id=flag_id)
            return _run(lambda: client.get_experiment(experiment_id, project_id=project_id))
        raise _bad("op doit être 'list', 'get', 'experiments' ou 'experiment'")

    # ================================================================
    # Session recordings
    # ================================================================

    @mcp.tool()
    def posthog_recording(
        op: Literal["list", "get"] = "list",
        recording_id: Optional[str] = None,
        person_uuid: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: Optional[int] = None,
        project_id: Optional[str] = None,
    ) -> object:
        """Session recordings — metadata only, never the video.

        Returns duration, the person, and pages visited — enough to decide which
        session a human should watch, and to hand back its id. ⚠️ No `count` in
        the envelope (measured).

        Args:
            op: "list" (default) | "get".
            recording_id: REQUIRED by "get".
            person_uuid: "list" filter — one person's sessions.
            date_from/date_to: "list" window (`-7d` or ISO date).
            limit, project_id: as elsewhere.

        """
        client = _client()
        if op == "list":
            _refuse_ignored(op, "utilise op='get' pour un enregistrement précis",
                            recording_id=recording_id)
            return _run(lambda: client.list_session_recordings(
                project_id=project_id, person_uuid=person_uuid, date_from=date_from,
                date_to=date_to, limit=limit or _DEFAULT_LIMIT))
        if op == "get":
            if not recording_id:
                raise _bad("op='get' requiert `recording_id`")
            _refuse_ignored(op, "ces filtres ne s'appliquent qu'à op='list'",
                            person_uuid=person_uuid, date_from=date_from, date_to=date_to)
            return _run(lambda: client.get_session_recording(
                recording_id, project_id=project_id))
        raise _bad("op doit être 'list' ou 'get'")

    # ================================================================
    # Projet & annotations — dont la seule écriture
    # ================================================================

    @mcp.tool()
    def posthog_project(
        op: Literal["current", "list", "annotations", "annotate"] = "current",
        content: Optional[str] = None,
        date_marker: Optional[str] = None,
        limit: Optional[int] = None,
        project_id: Optional[str] = None,
    ) -> object:
        """The project this key operates on, and the annotations on its charts.

        Call op="current" when a number looks surprising: it confirms WHICH
        project and WHICH account answered, which is the usual explanation.

        **"annotate" is the only write in this connector.** It drops a dated
        marker on the project's charts ("v2.3 shipped here") — purely additive,
        it changes no measurement, only how it reads.

        Args:
            op: "current" (default — which project and which account) | "list"
                (every project the key can read) | "annotations" | "annotate".
            content: REQUIRED by "annotate" — the marker's text.
            date_marker: "annotate" — the instant marked (ISO 8601).
                Default: now.
            limit, project_id: as elsewhere.

        """
        client = _client()
        if op == "current":
            _refuse_ignored(op, "op='current' ne prend pas ces champs",
                            content=content, date_marker=date_marker)

            def _current():
                me = client.current_user()
                org = me.get("organization") or {}
                return {"project_id": client.resolve_project_id(),
                        "organization": org.get("name"),
                        "account": me.get("email"),
                        "host": client.host}
            return _run(_current)
        if op == "list":
            _refuse_ignored(op, "op='list' ne prend pas ces champs",
                            content=content, date_marker=date_marker)
            return _run(lambda: client.list_projects())
        if op == "annotations":
            _refuse_ignored(op, "utilise op='annotate' pour en poser une",
                            content=content, date_marker=date_marker)
            return _run(lambda: client.list_annotations(
                project_id=project_id, limit=limit or _DEFAULT_LIMIT))
        if op == "annotate":
            if not content:
                raise _bad("op='annotate' requiert `content`")
            return _run(lambda: client.create_annotation(
                content, date_marker=date_marker, project_id=project_id))
        raise _bad("op doit être 'current', 'list', 'annotations' ou 'annotate'")
