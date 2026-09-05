"""PromptWatch — AI visibility monitoring: track how a brand/product appears in
LLM answers (ChatGPT, Claude, Gemini…), across prompts organized into
monitors, with visibility/sentiment/citation analytics and AI-generated
content to close coverage gaps.

Wraps `oto.tools.promptwatch.PromptWatchClient` (oto-core). Credential =
3-field model (ADR 0011, mirrors `lighton`): `api_key` (secret) + optional
`project_id` (org-level keys only — a project-level key is already scoped and
ignores it; `promptwatch_project()` is how an org-level key discovers ids).
Resolved via `access.resolve_credential_fields("promptwatch")` — byo-only, no
platform mode (no Otomata↔PromptWatch commercial deal, same reasoning as
cognism/lighton).

**Full API surface — 19 tools, one per resource family, `op=` selects the
verb** (ADR 0047 — never one tool per endpoint; the client below covers every
documented endpoint 1:1, only the MCP surface is consolidated):
`promptwatch_project`, `promptwatch_monitor`, `promptwatch_prompt` (incl.
native bulk create/delete/activate/deactivate/tag/topic — PromptWatch has
real batch endpoints, no client-side loop), `promptwatch_response`,
`promptwatch_visibility`, `promptwatch_citation`, `promptwatch_content`
(incl. content-gap + query fanouts — same "what/how prompts are covered"
domain), `promptwatch_taxonomy` (tags + topics — small, homogeneous CRUD),
`promptwatch_persona`, `promptwatch_brand`, `promptwatch_publishing` (CMS
connections + publish lifecycle), `promptwatch_content_agent` (settings +
scheduled slots), `promptwatch_ads` (Ads Radar), `promptwatch_shopping`
(product appearances/analytics + tracked products CRUD), `promptwatch_sitemap`
(crawl progress/URLs + site health — same "crawled site" domain),
`promptwatch_page_tracker`, `promptwatch_models`, `promptwatch_actions`,
`promptwatch_social` (Reddit + YouTube citations).

**Live-verified against a real account, 2026-08-20** — every GET op across
all 19 tools was exercised through a real MCP `tools/call` (not just the
client), see the client module docstring for the full list and the two path
corrections that came out of it (`promptwatch_ads(op="domain_analytics")`,
`promptwatch_shopping(op="position_analytics")`). Every mutating op
(create/update/delete/bulk, across every domain) was NOT exercised live —
read-only verification only, by design (no test data was written to the
connected account). Several less-common
query/body param names are still inferred-not-doc-confirmed rather than
individually verified; analytics-heavy ops accept `extra_params` as an
escape hatch for any filter that turns out to differ.
"""
from __future__ import annotations

from typing import Any, List, Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..connectors import verify as connector_verify


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _require(op: str, **fields) -> None:
    missing = [
        name for name, val in fields.items()
        if val is None or (isinstance(val, (list, dict, str)) and len(val) == 0)
    ]
    if missing:
        raise _bad(f"promptwatch(op='{op}') requires {', '.join(missing)}.")


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion » — otomata-tech/oto#69. Couvre `auth` SEUL.

    `GET projects` (déjà dans le client — `list_projects`), le premier appel
    de découverte du connecteur (`promptwatch_project`) : PromptWatch n'expose
    ni `/me` ni solde à cet endpoint. `raise_for_upstream` (typé,
    `UpstreamHTTPError` avec `status_code`) — le tool `_run` de ce module sait
    déjà distinguer 401 (clé) de 402 (quota) sur CET appel, signe qu'un compte
    suspendu peut y répondre 402 ; non lu ici (`auth` seul), le classement
    générique par code (401/402) s'applique tel quel si ça se produit.

    **Authentifié ≠ utilisable** (classe oto#69) : ne distingue pas de scope —
    une clé project-level est déjà scopée à un seul projet, `list_projects`
    la sert quand même (voir `promptwatch_project`).
    """
    from oto.tools.promptwatch.client import PromptWatchClient

    PromptWatchClient(
        api_key=fields["api_key"], project_id=fields.get("project_id") or None,
    ).list_projects()


def register(mcp: FastMCP) -> None:
    from oto.tools.promptwatch.client import PromptWatchClient

    connector_verify.register("promptwatch", _verify)

    def _creds() -> dict:
        return access.resolve_credential_fields("promptwatch")

    def _client(creds: dict) -> PromptWatchClient:
        return PromptWatchClient(api_key=creds.get("api_key"),
                                  project_id=creds.get("project_id") or None)

    def _run(fn):
        """Runs a PromptWatch call, translating upstream errors into an
        actionable McpError (401 key / 402 quota / 404 not found / 5xx retry)."""
        client = _client(_creds())
        try:
            return fn(client)
        except McpError:
            raise
        except ValueError as e:
            raise _bad(str(e))
        except Exception as e:
            status = getattr(e, "status_code", None)
            body = getattr(e, "body", None)
            if status == 401:
                msg = "Invalid or revoked PromptWatch API key (401). Check the key you configured."
            elif status == 402:
                msg = f"PromptWatch quota exceeded (402). {body if body else ''}".strip()
            elif status == 404:
                msg = f"PromptWatch: not found (404). {body if body else ''}".strip()
            elif status and status >= 500:
                msg = f"PromptWatch is temporarily unavailable (server error {status}). Try again shortly."
            else:
                msg = f"PromptWatch could not process the request ({e})."
            raise _bad(msg)

    # --- Projects ------------------------------------------------------------

    @mcp.tool()
    def promptwatch_project() -> Any:
        """List PromptWatch projects reachable by the configured key
        (PromptWatch). Only meaningful for an ORG-level key — a project-level
        key is already scoped to one project and this simply returns it. Use
        the returned `id` as `project_id` in the connector's credential to
        pin an org key to a specific project."""
        return _run(lambda c: c.list_projects())

    # --- Monitors --------------------------------------------------------------

    @mcp.tool()
    def promptwatch_monitor(
        op: Literal["list", "get", "create", "update", "delete"],
        monitor_id: Optional[str] = None,
        page: int = 1,
        size: int = 10,
        name: Optional[str] = None,
        models: Optional[List[str]] = None,
        description: Optional[str] = None,
        language_code: Optional[str] = None,
        country_code: Optional[str] = None,
        state_code: Optional[str] = None,
        city_code: Optional[str] = None,
        prompt_frequency: Optional[Literal["DAILY", "EVERY_OTHER_DAY", "WEEKLY", "MONTHLY"]] = None,
        persona_id: Optional[str] = None,
        persona_stacking_enabled: Optional[bool] = None,
        initial_prompts: Optional[List[dict]] = None,
        generate_prompts: Optional[List[dict]] = None,
    ) -> Any:
        """Monitors — the tracking unit a set of prompts belongs to (models
        tracked, geo/language, prompt cadence) (PromptWatch).

        Args:
            op: list | get | create | update | delete (soft-delete — data
                retained but hidden).
            monitor_id: required for get/update/delete.
            page, size: pagination for op="list" (size max 100).
            name: monitor name — required for create.
            models: LLM models to track, e.g. ["openai/gpt-4.1",
                "anthropic/claude-sonnet-4-20250514"] — required for create.
            description, language_code (default "en-US"), country_code
                (default "US"), state_code, city_code: geo/language targeting.
            prompt_frequency: how often prompts run.
            persona_id: persona injected into prompts if
                persona_stacking_enabled=True.
            initial_prompts: create=only — existing prompts to seed, each
                `{"prompt","type","intent"?,"keywords"?}`.
            generate_prompts: create=only, max 3 — auto-generate prompts,
                each `{"amount" (1-50), "type"?, "instructions"? (max 500 chars)}`.
        """
        if op == "list":
            return _run(lambda c: c.list_monitors(page=page, size=size))
        if op == "get":
            _require(op, monitor_id=monitor_id)
            return _run(lambda c: c.get_monitor(monitor_id))
        if op == "create":
            _require(op, name=name, models=models)
            return _run(lambda c: c.create_monitor(
                name, models, description=description, language_code=language_code,
                country_code=country_code, state_code=state_code, city_code=city_code,
                prompt_frequency=prompt_frequency, persona_id=persona_id,
                persona_stacking_enabled=persona_stacking_enabled,
                initial_prompts=initial_prompts, generate_prompts=generate_prompts,
            ))
        if op == "update":
            _require(op, monitor_id=monitor_id)
            fields = {
                "name": name, "models": models, "description": description,
                "languageCode": language_code, "countryCode": country_code,
                "stateCode": state_code, "cityCode": city_code,
                "promptFrequency": prompt_frequency, "personaId": persona_id,
                "personaStackingEnabled": persona_stacking_enabled,
            }
            fields = {k: v for k, v in fields.items() if v is not None}
            if not fields:
                raise _bad("promptwatch_monitor(op='update') requires at least one field to change.")
            return _run(lambda c: c.update_monitor(monitor_id, **fields))
        if op == "delete":
            _require(op, monitor_id=monitor_id)
            return _run(lambda c: c.delete_monitor(monitor_id))
        raise _bad(f"Unknown op {op!r}.")

    # --- Prompts -----------------------------------------------------------

    @mcp.tool()
    def promptwatch_prompt(
        op: Literal["list", "get", "create", "update", "delete", "bulk_create",
                    "bulk_delete", "activate", "deactivate", "attach_tags",
                    "attach_topics", "bulk_attach_tags", "bulk_attach_topics"],
        prompt_id: Optional[str] = None,
        prompt_ids: Optional[List[str]] = None,
        prompt: Optional[str] = None,
        prompts: Optional[List[dict]] = None,
        llm_monitor_id: Optional[str] = None,
        type: Optional[Literal["ORGANIC", "BRAND_SPECIFIC", "COMPETITOR_COMPARISON"]] = None,
        intent: Optional[Literal["BRANDED", "INFORMATIONAL", "NAVIGATIONAL",
                                  "COMMERCIAL", "TRANSACTIONAL"]] = None,
        language_code: Optional[str] = None,
        keywords: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        topics: Optional[List[str]] = None,
        is_active: Optional[bool] = None,
        page: int = 1,
        size: int = 10,
        query: Optional[str] = None,
        types: Optional[List[str]] = None,
        topic_ids: Optional[List[str]] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[Literal["asc", "desc"]] = None,
    ) -> Any:
        """Prompts — the individual queries tracked under a monitor, and how
        they're tagged/activated. Bulk ops call PromptWatch's own native
        batch endpoints (not a client-side loop) (PromptWatch).

        Args:
            op: list | get | create | update | delete | bulk_create |
                bulk_delete | activate | deactivate | attach_tags |
                attach_topics | bulk_attach_tags | bulk_attach_topics.
                Solo ops (create/update/delete/attach_*) take `prompt_id`;
                bulk ops take `prompt_ids` — 1-100 items, soft-delete on
                delete/bulk_delete.
            prompt_id: required for get/update/delete/attach_tags/attach_topics.
            prompt_ids: required for bulk_delete/activate/deactivate/
                bulk_attach_tags/bulk_attach_topics (1-100 UUIDs).
            prompt: the prompt text — required for create.
            prompts: bulk_create=only, 1-100 items, each
                `{"prompt","type","intent"?,"languageCode"?,"keywords"?,
                "tags"?,"isActive"?}`.
            llm_monitor_id: required for create and bulk_create (monitor to
                attach to — a prompt belongs to exactly one monitor).
            type: required for create and update.
            intent: optional on create, but REQUIRED on update — PromptWatch's
                update endpoint replaces both fields together (not a partial
                patch), so omitting it on update risks nulling it out upstream.
            language_code: create=only, default "en-US".
            keywords, tags: create=only.
            topics: required for attach_topics/bulk_attach_topics — topic
                NAMES (not ids), auto-created if new.
            tags (again, for attach_tags/bulk_attach_tags): tag NAMES,
                auto-created if new.
            is_active: create=only, default true.
            page, size, query, types, topic_ids, sort_by, sort_order: op="list"
                filters — types ⊆ ORGANIC/BRAND_SPECIFIC/COMPETITOR_COMPARISON,
                topic_ids = existing topic UUIDs, sort_by ∈ createdAt/
                updatedAt/isActive/type/intent (default createdAt).
        """
        if op == "list":
            return _run(lambda c: c.list_prompts(
                page=page, size=size, llm_monitor_id=llm_monitor_id, query=query,
                is_active=is_active, types=types, topic_ids=topic_ids,
                sort_by=sort_by, sort_order=sort_order,
            ))
        if op == "get":
            _require(op, prompt_id=prompt_id)
            return _run(lambda c: c.get_prompt(prompt_id))
        if op == "create":
            _require(op, prompt=prompt, llm_monitor_id=llm_monitor_id, type=type)
            return _run(lambda c: c.create_prompt(
                prompt, llm_monitor_id, type, intent=intent,
                language_code=language_code, keywords=keywords, tags=tags,
                is_active=is_active,
            ))
        if op == "update":
            _require(op, prompt_id=prompt_id, type=type, intent=intent)
            return _run(lambda c: c.update_prompt(prompt_id, type, intent))
        if op == "delete":
            _require(op, prompt_id=prompt_id)
            return _run(lambda c: c.delete_prompt(prompt_id))
        if op == "bulk_create":
            _require(op, llm_monitor_id=llm_monitor_id, prompts=prompts)
            return _run(lambda c: c.bulk_create_prompts(llm_monitor_id, prompts))
        if op == "bulk_delete":
            _require(op, prompt_ids=prompt_ids)
            return _run(lambda c: c.bulk_delete_prompts(prompt_ids))
        if op == "activate":
            _require(op, prompt_ids=prompt_ids)
            return _run(lambda c: c.activate_prompts(prompt_ids))
        if op == "deactivate":
            _require(op, prompt_ids=prompt_ids)
            return _run(lambda c: c.deactivate_prompts(prompt_ids))
        if op == "attach_tags":
            _require(op, prompt_id=prompt_id, tags=tags)
            return _run(lambda c: c.attach_tags(prompt_id, tags))
        if op == "attach_topics":
            _require(op, prompt_id=prompt_id, topics=topics)
            return _run(lambda c: c.attach_topics(prompt_id, topics))
        if op == "bulk_attach_tags":
            _require(op, prompt_ids=prompt_ids, tags=tags)
            return _run(lambda c: c.bulk_attach_tags(prompt_ids, tags))
        if op == "bulk_attach_topics":
            _require(op, prompt_ids=prompt_ids, topics=topics)
            return _run(lambda c: c.bulk_attach_topics(prompt_ids, topics))
        raise _bad(f"Unknown op {op!r}.")

    # --- Responses -------------------------------------------------------------

    @mcp.tool()
    def promptwatch_response(
        op: Literal["list", "get", "summary", "sentiment_distribution",
                    "sentiment_time_series", "mentions_time_series", "top_competitors"],
        response_id: Optional[str] = None,
        page: int = 1,
        size: int = 10,
        llm_monitor_id: Optional[str] = None,
        prompt_id: Optional[str] = None,
        models: Optional[List[str]] = None,
        sentiment: Optional[List[Literal["POSITIVE", "NEGATIVE", "NEUTRAL"]]] = None,
        mentioned_our_brand: Optional[bool] = None,
        prompt_types: Optional[List[str]] = None,
        topic_ids: Optional[List[str]] = None,
        date_from: Optional[str] = None,
        until: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[Literal["asc", "desc"]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        extra_params: Optional[dict] = None,
    ) -> Any:
        """LLM responses recorded for tracked prompts, plus response-level
        analytics (PromptWatch).

        Args:
            op: list | get | summary | sentiment_distribution |
                sentiment_time_series | mentions_time_series | top_competitors.
            response_id: required for get (returns full citations, unlike list).
            page, size, llm_monitor_id, prompt_id, models, sentiment,
                mentioned_our_brand, prompt_types, topic_ids, date_from
                (ISO datetime, responses created after), until (ISO datetime,
                before), sort_by (createdAt|visibilityScore|sentimentScore),
                sort_order: op="list" filters.
            start_date, end_date (YYYY-MM-DD, ≤90-day span): date window for
                summary/sentiment_distribution/sentiment_time_series/
                mentions_time_series/top_competitors.
            extra_params: additional PromptWatch query params (camelCase) not
                covered above — escape hatch for the analytics ops, whose
                exact filter set wasn't individually verified against the
                live API.
        """
        if op == "list":
            return _run(lambda c: c.list_responses(
                page=page, size=size, llm_monitor_id=llm_monitor_id,
                prompt_id=prompt_id, models=models, sentiment=sentiment,
                mentioned_our_brand=mentioned_our_brand, prompt_types=prompt_types,
                topic_ids=topic_ids, from_=date_from, until=until,
                sort_by=sort_by, sort_order=sort_order,
            ))
        if op == "get":
            _require(op, response_id=response_id)
            return _run(lambda c: c.get_response(response_id))
        if op == "summary":
            return _run(lambda c: c.response_summary(start_date=start_date, end_date=end_date))
        if op == "sentiment_distribution":
            return _run(lambda c: c.sentiment_distribution(
                start_date=start_date, end_date=end_date, **(extra_params or {})))
        if op == "sentiment_time_series":
            return _run(lambda c: c.response_sentiment_time_series(
                start_date=start_date, end_date=end_date, **(extra_params or {})))
        if op == "mentions_time_series":
            return _run(lambda c: c.mentions_time_series(
                start_date=start_date, end_date=end_date, **(extra_params or {})))
        if op == "top_competitors":
            return _run(lambda c: c.top_competitors(
                start_date=start_date, end_date=end_date, **(extra_params or {})))
        raise _bad(f"Unknown op {op!r}.")

    # --- Visibility --------------------------------------------------------

    @mcp.tool()
    def promptwatch_visibility(
        op: Literal["time_series", "sentiment_time_series", "prompt_time_series", "competitor_heatmap"],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        range: Optional[Literal["day", "week", "month"]] = None,
        models: Optional[List[str]] = None,
        prompt_id: Optional[str] = None,
        llm_monitor_id: Optional[str] = None,
        exclude_self: Optional[bool] = None,
        hide_ignored_brands: Optional[bool] = None,
        relations: Optional[List[Literal["DIRECT_COMPETITOR", "SELF", "OTHER", "IGNORED"]]] = None,
        limit: Optional[int] = None,
        prompt_types: Optional[List[str]] = None,
        tag_ids: Optional[List[str]] = None,
        topic_ids: Optional[List[str]] = None,
    ) -> Any:
        """Brand visibility analytics across LLM answers — overall trend,
        per-prompt trend, and how you compare to competitors (PromptWatch).

        Args:
            op: time_series (brand visibility over time) | sentiment_time_series
                (project-wide brand sentiment over time — distinct from
                promptwatch_response(op="sentiment_time_series"), which is
                response-level) | prompt_time_series (one prompt's trend —
                requires prompt_id) | competitor_heatmap (visibility % per
                competitor × per model).
            start_date, end_date (YYYY-MM-DD, ≤90-day span), range (day|week|
                month grouping, default day), models, prompt_id, llm_monitor_id:
                time_series / sentiment_time_series / prompt_time_series filters.
            exclude_self, hide_ignored_brands, relations, limit (1-100,
                default 20), prompt_types, tag_ids, topic_ids: competitor_heatmap
                filters only.
        """
        if op == "time_series":
            return _run(lambda c: c.visibility_time_series(
                start_date=start_date, end_date=end_date, range=range,
                models=models, prompt_id=prompt_id, llm_monitor_id=llm_monitor_id,
            ))
        if op == "sentiment_time_series":
            return _run(lambda c: c.sentiment_time_series(
                start_date=start_date, end_date=end_date, range=range,
                models=models, prompt_id=prompt_id, llm_monitor_id=llm_monitor_id,
            ))
        if op == "prompt_time_series":
            _require(op, prompt_id=prompt_id)
            return _run(lambda c: c.prompt_visibility_time_series(
                prompt_id, start_date=start_date, end_date=end_date,
                range=range, models=models,
            ))
        if op == "competitor_heatmap":
            return _run(lambda c: c.competitor_heatmap(
                start_date=start_date, end_date=end_date, models=models,
                prompt_id=prompt_id, llm_monitor_id=llm_monitor_id,
                exclude_self=exclude_self, hide_ignored_brands=hide_ignored_brands,
                relations=relations, limit=limit, prompt_types=prompt_types,
                tag_ids=tag_ids, topic_ids=topic_ids,
            ))
        raise _bad(f"Unknown op {op!r}.")

    # --- Citations -----------------------------------------------------------

    @mcp.tool()
    def promptwatch_citation(
        op: Literal["analytics", "rank_analysis", "domains_over_time",
                    "domains_by_llm", "grouped", "llm_sources",
                    "self_frequency", "top_pages"],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        models: Optional[List[str]] = None,
        prompt_id: Optional[str] = None,
        llm_monitor_id: Optional[str] = None,
        prompt_types: Optional[List[str]] = None,
        domains: Optional[List[str]] = None,
        topic_ids: Optional[List[str]] = None,
        page: int = 1,
        size: int = 20,
        extra_params: Optional[dict] = None,
    ) -> Any:
        """Which pages/domains get cited by LLMs when answering tracked
        prompts, and by whom (PromptWatch).

        Args:
            op: analytics (top domains/URLs + authority metrics) |
                rank_analysis | domains_over_time | domains_by_llm | grouped
                (paginated sortable table of cited URLs) | llm_sources
                (top LLM sources) | self_frequency (how often you cite
                yourself) | top_pages (top cited pages with rank).
            start_date, end_date (YYYY-MM-DD, ≤90-day span for op="analytics"),
                models, prompt_id, llm_monitor_id, prompt_types, domains,
                topic_ids: shared filters (op="analytics" verified against
                the live API; other ops accept the same names best-effort).
            page, size: pagination (op="analytics"/"grouped").
            extra_params: additional PromptWatch query params (camelCase) —
                escape hatch, most ops beyond "analytics" weren't individually
                verified against the live API.
        """
        base = dict(startDate=start_date, endDate=end_date, models=models,
                    promptId=prompt_id, llmMonitorId=llm_monitor_id,
                    promptTypes=prompt_types, domains=domains, topicIds=topic_ids)
        base = {k: v for k, v in base.items() if v is not None}
        base.update(extra_params or {})
        if op == "analytics":
            return _run(lambda c: c.citations(
                start_date=start_date, end_date=end_date, models=models,
                prompt_id=prompt_id, llm_monitor_id=llm_monitor_id,
                prompt_types=prompt_types, domains=domains, topic_ids=topic_ids,
                page=page, size=size,
            ))
        if op == "rank_analysis":
            return _run(lambda c: c.citation_rank_analysis(**base))
        if op == "domains_over_time":
            return _run(lambda c: c.citation_domains_over_time(**base))
        if op == "domains_by_llm":
            return _run(lambda c: c.citation_domains_by_llm(**base))
        if op == "grouped":
            return _run(lambda c: c.citation_grouped(page=page, size=size, **base))
        if op == "llm_sources":
            return _run(lambda c: c.citation_llm_sources(**base))
        if op == "self_frequency":
            return _run(lambda c: c.citation_self_frequency(**base))
        if op == "top_pages":
            return _run(lambda c: c.citation_top_pages(**base))
        raise _bad(f"Unknown op {op!r}.")

    # --- Content + content gap ---------------------------------------------

    @mcp.tool()
    def promptwatch_content(
        op: Literal["list", "get", "create", "gap_stats", "gap_prompts",
                    "gap_latest", "gap_recommendations", "query_fanouts"],
        content_id: Optional[str] = None,
        page: int = 1,
        size: int = 25,
        order_by: Optional[Literal["createdAt", "updatedAt"]] = None,
        sort_order: Optional[Literal["asc", "desc"]] = None,
        mode: Optional[Literal["CREATE", "OPTIMIZE"]] = None,
        status: Optional[Literal["DRAFT", "PENDING", "IN_PROGRESS", "COMPLETED",
                                  "FAILED", "STOPPED"]] = None,
        prompt_id: Optional[str] = None,
        persona_id: Optional[str] = None,
        type: Optional[str] = None,
        content_length: Optional[Literal["SHORT", "MEDIUM", "LONG"]] = None,
        optimization_level: Optional[Literal["LOW", "MEDIUM", "HIGH"]] = None,
        url: Optional[str] = None,
        tone_of_voice: Optional[str] = None,
        custom_tone_of_voice: Optional[str] = None,
        language_code: Optional[str] = None,
        image_artistic_style: Optional[str] = None,
        image_prompt_instructions: Optional[str] = None,
        blocked_words: Optional[List[str]] = None,
        brief_title: Optional[str] = None,
        brief_description: Optional[str] = None,
        brief_objective: Optional[str] = None,
        brief_call_to_action: Optional[str] = None,
        brief_key_points: Optional[List[str]] = None,
        brief_context: Optional[str] = None,
        content_gap_recommendation_id: Optional[str] = None,
        query: Optional[str] = None,
        prompt_types: Optional[List[str]] = None,
        intent_types: Optional[List[str]] = None,
        tag_ids: Optional[List[str]] = None,
        topic_ids: Optional[List[str]] = None,
        has_coverage: Optional[bool] = None,
        sort_by: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Any:
        """AI content generation to close visibility gaps, and the gap
        analysis that motivates it (PromptWatch).

        Args:
            op: list | get | create (AI content) | gap_stats (aggregate
                coverage stats) | gap_prompts (prompts with coverage scores)
                | gap_latest (one prompt's latest coverage — requires
                prompt_id) | gap_recommendations (recommendations from that
                latest coverage — requires prompt_id) | query_fanouts
                (prompts that have LLM-generated sub-query fanouts —
                page/size only).
            content_id: required for get.
            page, size, order_by, sort_order, mode, status: op="list" filters.
            prompt_id, persona_id: required for create.
            type: required for create when mode="CREATE" — ARTICLE|BLOG_POST|
                OPINION|LISTICLE|HOW_TO|REVIEW|COMPARISON|CASE_STUDY|
                INTERVIEW|DOCUMENTATION|WIKI|PRODUCT_PAGE|LANDING_PAGE|
                PRESS_RELEASE|GENERIC_CONTENT|PRODUCT_COMPARISON.
            content_length: required for create when mode="CREATE".
            optimization_level, url: required for create when mode="OPTIMIZE"
                (rewrite an existing sitemap page — no brief_* fields apply).
            tone_of_voice, custom_tone_of_voice (required if tone_of_voice=
                "CUSTOM"), language_code, image_artistic_style,
                image_prompt_instructions, blocked_words, brief_title,
                brief_description, brief_objective, brief_call_to_action,
                brief_key_points, brief_context, content_gap_recommendation_id:
                create=only, optional (brief_* apply to mode="CREATE" only).
                create returns `{id, status: "PENDING"}` — poll op="get".
            query, prompt_types, intent_types, tag_ids, topic_ids,
                has_coverage, sort_by (contentCoverageScore|createdAt),
                sort_order, start_date, end_date: gap_prompts filters
                (page/size shared with op="list").
            start_date, end_date: also used by gap_stats (with prompt_types).
        """
        if op == "list":
            return _run(lambda c: c.list_content(
                page=page, size=size, order_by=order_by, sort_order=sort_order,
                mode=mode, status=status,
            ))
        if op == "get":
            _require(op, content_id=content_id)
            return _run(lambda c: c.get_content(content_id))
        if op == "create":
            _require(op, mode=mode, prompt_id=prompt_id, persona_id=persona_id)
            return _run(lambda c: c.create_content(
                mode, prompt_id, persona_id, type=type, content_length=content_length,
                optimization_level=optimization_level, url=url,
                tone_of_voice=tone_of_voice, custom_tone_of_voice=custom_tone_of_voice,
                language_code=language_code, image_artistic_style=image_artistic_style,
                image_prompt_instructions=image_prompt_instructions,
                blocked_words=blocked_words, brief_title=brief_title,
                brief_description=brief_description, brief_objective=brief_objective,
                brief_call_to_action=brief_call_to_action,
                brief_key_points=brief_key_points, brief_context=brief_context,
                content_gap_recommendation_id=content_gap_recommendation_id,
            ))
        if op == "gap_stats":
            return _run(lambda c: c.content_gap_stats(
                prompt_types=prompt_types, start_date=start_date, end_date=end_date))
        if op == "gap_prompts":
            return _run(lambda c: c.content_gap_prompts(
                page=page, size=size, query=query, prompt_types=prompt_types,
                intent_types=intent_types, tag_ids=tag_ids, topic_ids=topic_ids,
                has_coverage=has_coverage, sort_by=sort_by, sort_order=sort_order,
                start_date=start_date, end_date=end_date,
            ))
        if op == "gap_latest":
            _require(op, prompt_id=prompt_id)
            return _run(lambda c: c.content_gap_latest(prompt_id))
        if op == "gap_recommendations":
            _require(op, prompt_id=prompt_id)
            return _run(lambda c: c.content_gap_recommendations(prompt_id))
        if op == "query_fanouts":
            return _run(lambda c: c.list_query_fanouts(page=page, size=size))
        raise _bad(f"Unknown op {op!r}.")

    # --- Tags + topics -----------------------------------------------------

    @mcp.tool()
    def promptwatch_taxonomy(
        op: Literal["list_tags", "create_tags", "delete_tag", "rename_tag",
                    "list_topics", "create_topics", "delete_topic", "rename_topic"],
        id: Optional[str] = None,
        names: Optional[List[str]] = None,
        name: Optional[str] = None,
    ) -> Any:
        """Tags and topics — free-form labels attached to prompts, used to
        filter/group everywhere else in this connector (PromptWatch).

        Args:
            op: list_tags | create_tags | delete_tag | rename_tag |
                list_topics | create_topics | delete_topic | rename_topic.
            id: the tag/topic id — required for delete_tag/rename_tag/
                delete_topic/rename_topic.
            names: new tag/topic names to create — required for
                create_tags/create_topics.
            name: the new name — required for rename_tag/rename_topic.
        """
        if op == "list_tags":
            return _run(lambda c: c.list_tags())
        if op == "create_tags":
            _require(op, names=names)
            return _run(lambda c: c.create_tags(names))
        if op == "delete_tag":
            _require(op, id=id)
            return _run(lambda c: c.delete_tag(id))
        if op == "rename_tag":
            _require(op, id=id, name=name)
            return _run(lambda c: c.rename_tag(id, name))
        if op == "list_topics":
            return _run(lambda c: c.list_topics())
        if op == "create_topics":
            _require(op, names=names)
            return _run(lambda c: c.create_topics(names))
        if op == "delete_topic":
            _require(op, id=id)
            return _run(lambda c: c.delete_topic(id))
        if op == "rename_topic":
            _require(op, id=id, name=name)
            return _run(lambda c: c.rename_topic(id, name))
        raise _bad(f"Unknown op {op!r}.")

    # --- Personas ------------------------------------------------------------

    @mcp.tool()
    def promptwatch_persona(
        op: Literal["list", "get", "create", "update", "delete"],
        persona_id: Optional[str] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        age_range: Optional[str] = None,
        education_level: Optional[str] = None,
        stackable_prompt: Optional[str] = None,
    ) -> Any:
        """Personas — the audience angle a monitor/content piece can be
        written for; `stackable_prompt` injects into tracked prompts when a
        monitor has persona stacking enabled (PromptWatch).

        Args:
            op: list | get | create | update | delete.
            persona_id: required for get/update/delete.
            name, description: required for create.
            age_range, education_level: optional context.
            stackable_prompt: ≥30 chars — used by monitors with
                persona_stacking_enabled=True.
        """
        if op == "list":
            return _run(lambda c: c.list_personas())
        if op == "get":
            _require(op, persona_id=persona_id)
            return _run(lambda c: c.get_persona(persona_id))
        if op == "create":
            _require(op, name=name, description=description)
            return _run(lambda c: c.create_persona(
                name, description, age_range=age_range,
                education_level=education_level, stackable_prompt=stackable_prompt,
            ))
        if op == "update":
            _require(op, persona_id=persona_id)
            fields = {
                "name": name, "description": description, "ageRange": age_range,
                "educationLevel": education_level, "stackablePrompt": stackable_prompt,
            }
            fields = {k: v for k, v in fields.items() if v is not None}
            if not fields:
                raise _bad("promptwatch_persona(op='update') requires at least one field to change.")
            return _run(lambda c: c.update_persona(persona_id, **fields))
        if op == "delete":
            _require(op, persona_id=persona_id)
            return _run(lambda c: c.delete_persona(persona_id))
        raise _bad(f"Unknown op {op!r}.")

    # --- Brands --------------------------------------------------------------

    @mcp.tool()
    def promptwatch_brand(
        op: Literal["list", "create", "update"],
        brand_id: Optional[str] = None,
        name: Optional[str] = None,
        url: Optional[str] = None,
        relation: Optional[Literal["SELF", "DIRECT_COMPETITOR", "OTHER", "IGNORED"]] = None,
    ) -> Any:
        """Brands connected to the project — yours (relation="SELF") and the
        competitors tracked in visibility/citation analytics (PromptWatch).

        Args:
            op: list | create | update.
            brand_id: required for update.
            name, url, relation: required for create. On update, only
                `relation` is meaningful in practice (re-classify a brand as
                SELF/DIRECT_COMPETITOR/OTHER/IGNORED); pass any subset that
                changed.
        """
        if op == "list":
            return _run(lambda c: c.list_brands())
        if op == "create":
            _require(op, name=name, url=url, relation=relation)
            return _run(lambda c: c.create_brand(name, url, relation))
        if op == "update":
            _require(op, brand_id=brand_id)
            fields = {"name": name, "url": url, "relation": relation}
            fields = {k: v for k, v in fields.items() if v is not None}
            if not fields:
                raise _bad("promptwatch_brand(op='update') requires at least one field to change.")
            return _run(lambda c: c.update_brand(brand_id, **fields))
        raise _bad(f"Unknown op {op!r}.")

    # --- Publishing --------------------------------------------------------

    @mcp.tool()
    def promptwatch_publishing(
        op: Literal["list_connections", "status", "set", "clear", "push_draft", "publish_live"],
        content_id: Optional[str] = None,
        url: Optional[str] = None,
        published_at: Optional[str] = None,
        extra_params: Optional[dict] = None,
    ) -> Any:
        """CMS connections and the publish lifecycle of a generated content
        document — separate from PromptWatch's own content generation
        (`promptwatch_content`), which this consumes (PromptWatch).

        Args:
            op: list_connections (connected CMS destinations — Webflow,
                Framer) | status (latest CMS publish record) | set (record a
                live URL, starts citation tracking for it) | clear (unlink
                from its published URL) | push_draft (push to the connected
                CMS as a draft) | publish_live (publish live to the CMS).
            content_id: required for all ops except list_connections.
            url: required for op="set" — the live URL to record.
            published_at: optional for op="set" (ISO 8601).
            extra_params: additional body fields for push_draft/publish_live
                (undocumented body shape — escape hatch).
        """
        if op == "list_connections":
            return _run(lambda c: c.list_cms_connections())
        _require(op, content_id=content_id)
        if op == "status":
            return _run(lambda c: c.get_content_publish_status(content_id))
        if op == "set":
            _require(op, url=url)
            return _run(lambda c: c.set_content_publication(
                content_id, url, published_at=published_at))
        if op == "clear":
            return _run(lambda c: c.clear_content_publication(content_id))
        if op == "push_draft":
            return _run(lambda c: c.push_content_draft_to_cms(content_id, **(extra_params or {})))
        if op == "publish_live":
            return _run(lambda c: c.publish_content_live(content_id, **(extra_params or {})))
        raise _bad(f"Unknown op {op!r}.")

    # --- Content Agent -------------------------------------------------------

    @mcp.tool()
    def promptwatch_content_agent(
        op: Literal["get_settings", "update_settings", "list_slots", "get_slot",
                    "update_slot", "accept_slot", "decline_slot", "publish_slot_now"],
        slot_id: Optional[str] = None,
        page: int = 1,
        size: int = 25,
        order_by: Optional[Literal["publishAt", "publishedAt", "generateBy",
                                    "priorityScore", "createdAt"]] = None,
        sort_order: Optional[Literal["asc", "desc"]] = None,
        statuses: Optional[List[str]] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        enabled: Optional[bool] = None,
        autonomy_mode: Optional[Literal["GATED", "AUTOMATE"]] = None,
        publish_state: Optional[Literal["LIVE", "DRAFT"]] = None,
        publish_timing: Optional[Literal["AT_SLOT", "IMMEDIATE_ON_APPROVAL"]] = None,
        publish_windows: Optional[List[dict]] = None,
        blackout_dates: Optional[List[str]] = None,
        schedule_timezone: Optional[str] = None,
        max_per_day: Optional[int] = None,
        budget_percentage: Optional[float] = None,
        default_enabled_tools: Optional[List[str]] = None,
        cms_connection_id: Optional[str] = None,
        slot_fields: Optional[dict] = None,
        extra_params: Optional[dict] = None,
    ) -> Any:
        """The autonomous Content Agent — its config and its calendar of
        scheduled pieces (PromptWatch, requires the Content Agent
        entitlement + completed setup on the account).

        Args:
            op: get_settings | update_settings | list_slots | get_slot
                (requires slot_id) | update_slot (requires slot_id +
                slot_fields) | accept_slot (approve a draft for publishing,
                requires slot_id) | decline_slot (requires slot_id) |
                publish_slot_now (requires slot_id).
            slot_id: required for get_slot/update_slot/accept_slot/
                decline_slot/publish_slot_now.
            page, size, order_by, sort_order, statuses (⊆ PLANNED|
                GENERATING|REVIEW|APPROVED|SCHEDULED|PUBLISHED|DEFERRED|
                SUPPRESSED|FAILED), from_date, to_date: op="list_slots"
                filters — from_date/to_date switch to "calendar mode" (up to
                500 rows unpaginated, per PromptWatch).
            enabled, autonomy_mode (GATED=human approval, AUTOMATE=auto-
                publish), publish_state, publish_timing, publish_windows
                (per-weekday `{weekday 0-6, startMinute, endMinute}`),
                blackout_dates (ISO dates blocking publication),
                schedule_timezone (IANA tz, omit = org default), max_per_day
                (1-20), budget_percentage (0-1), default_enabled_tools (e.g.
                GOOGLE_SEARCH, CRAWL_WEBSITE, GENERATE_ARTICLE_IMAGE),
                cms_connection_id: op="update_settings", any subset.
            slot_fields: op="update_slot" only — raw field dict (this
                endpoint's body shape isn't individually documented; pass
                whatever the slot object accepts).
            extra_params: body fields for accept_slot/decline_slot/
                publish_slot_now (undocumented — escape hatch).
        """
        if op == "get_settings":
            return _run(lambda c: c.get_content_agent_settings())
        if op == "update_settings":
            fields = {
                "enabled": enabled, "autonomyMode": autonomy_mode,
                "publishState": publish_state, "publishTiming": publish_timing,
                "publishWindows": publish_windows, "blackoutDates": blackout_dates,
                "scheduleTimezone": schedule_timezone, "maxPerDay": max_per_day,
                "budgetPercentage": budget_percentage,
                "defaultEnabledTools": default_enabled_tools,
                "cmsConnectionId": cms_connection_id,
            }
            fields = {k: v for k, v in fields.items() if v is not None}
            if not fields:
                raise _bad("promptwatch_content_agent(op='update_settings') requires at least one field to change.")
            return _run(lambda c: c.update_content_agent_settings(**fields))
        if op == "list_slots":
            return _run(lambda c: c.list_content_agent_slots(
                page=page, size=size, order_by=order_by, sort_order=sort_order,
                statuses=statuses, from_date=from_date, to_date=to_date,
            ))
        if op == "get_slot":
            _require(op, slot_id=slot_id)
            return _run(lambda c: c.get_content_agent_slot(slot_id))
        if op == "update_slot":
            _require(op, slot_id=slot_id, slot_fields=slot_fields)
            return _run(lambda c: c.update_content_agent_slot(slot_id, **slot_fields))
        if op == "accept_slot":
            _require(op, slot_id=slot_id)
            return _run(lambda c: c.accept_content_agent_slot(slot_id, **(extra_params or {})))
        if op == "decline_slot":
            _require(op, slot_id=slot_id)
            return _run(lambda c: c.decline_content_agent_slot(slot_id, **(extra_params or {})))
        if op == "publish_slot_now":
            _require(op, slot_id=slot_id)
            return _run(lambda c: c.publish_content_agent_slot_now(slot_id, **(extra_params or {})))
        raise _bad(f"Unknown op {op!r}.")

    # --- Ads radar -----------------------------------------------------------

    @mcp.tool()
    def promptwatch_ads(
        op: Literal["list_ads", "list_prompts", "list_domains", "domain_analytics"],
        page: int = 1,
        size: int = 25,
        search: Optional[str] = None,
        prompt_ids: Optional[List[str]] = None,
        sort_by: Optional[Literal["createdAt", "positionInResponse",
                                   "advertiserName", "rootDomain"]] = None,
        sort_order: Optional[Literal["asc", "desc"]] = None,
        date_from: Optional[str] = None,
        until: Optional[str] = None,
        models: Optional[List[str]] = None,
        prompt_types: Optional[List[str]] = None,
        intent_types: Optional[List[str]] = None,
        domains: Optional[List[str]] = None,
    ) -> Any:
        """Ads captured in LLM answers — which prompts surface ads, and from
        which advertiser domains (PromptWatch, requires Ads Radar plan
        entitlement).

        Args:
            op: list_ads | list_prompts (prompts that surfaced ≥1 ad) |
                list_domains (advertiser root domains, ordered by ad count) |
                domain_analytics (ad occurrences by domain — aggregated +
                daily breakdown; `domains` narrows to specific domains, omit
                for all).
            page, size, search, prompt_ids, sort_by, sort_order: list_ads
                filters (prompt_ids/sort_by not available on list_prompts;
                unused by list_domains/domain_analytics).
            date_from, until (ISO 8601, ≤90-day span), models, prompt_types,
                intent_types, domains: shared filters across all ops.
        """
        if op == "list_ads":
            return _run(lambda c: c.list_ads(
                page=page, size=size, search=search, prompt_ids=prompt_ids,
                sort_by=sort_by, sort_order=sort_order, from_=date_from, until=until,
                models=models, prompt_types=prompt_types, intent_types=intent_types,
                domains=domains,
            ))
        if op == "list_prompts":
            return _run(lambda c: c.list_prompts_with_ads(
                page=page, size=size, search=search, from_=date_from, until=until,
                models=models, prompt_types=prompt_types, intent_types=intent_types,
                domains=domains,
            ))
        if op == "list_domains":
            return _run(lambda c: c.list_ad_domains(
                from_=date_from, until=until, models=models,
                prompt_types=prompt_types, intent_types=intent_types,
            ))
        if op == "domain_analytics":
            return _run(lambda c: c.ad_domain_analytics(
                from_=date_from, until=until, models=models,
                prompt_types=prompt_types, intent_types=intent_types,
                domains=domains,
            ))
        raise _bad(f"Unknown op {op!r}.")

    # --- Shopping --------------------------------------------------------------

    @mcp.tool()
    def promptwatch_shopping(
        op: Literal["list_appearances", "get_item", "products_over_time",
                    "position_analytics", "top_merchants", "top_products",
                    "list_tracked", "add_tracked", "update_tracked", "delete_tracked"],
        item_id: Optional[str] = None,
        page: int = 1,
        size: int = 25,
        search: Optional[str] = None,
        sort_by: Optional[Literal["createdAt", "positionInResponse", "rating",
                                   "numReviews"]] = None,
        date_from: Optional[str] = None,
        until: Optional[str] = None,
        models: Optional[List[str]] = None,
        prompt_types: Optional[List[str]] = None,
        intent_types: Optional[List[str]] = None,
        limit: Optional[int] = None,
        tracked_product_id: Optional[str] = None,
        products: Optional[List[dict]] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        external_product_id: Optional[str] = None,
        match_status: Optional[Literal["matched", "unmatched"]] = None,
        extra_params: Optional[dict] = None,
    ) -> Any:
        """Shopping/product placements surfaced in LLM answers (e.g. ChatGPT
        Shopping), and the separate list of products you're tracking for
        match/appearance detection (PromptWatch, requires Shopping plan
        entitlement for the analytics ops).

        Args:
            op: list_appearances (product surfaces in responses) | get_item
                (one appearance record, requires item_id) | products_over_time
                (surfaced-product counts by day) | position_analytics (top
                products by average ranking position — top-N, NOT filterable
                to one product; use get_item for a single item's own stats)
                | top_merchants | top_products | list_tracked (your tracked-
                products list) | add_tracked (requires products) |
                update_tracked (requires tracked_product_id) | delete_tracked
                (requires tracked_product_id — UNVERIFIED against the live
                API, follows this API's otherwise-universal DELETE .../{id}
                convention).
            item_id: required for get_item.
            page, size, search, sort_by: list_appearances filters.
            date_from, until, models, prompt_types, intent_types: shared
                filters across the analytics ops.
            limit: top_products/top_merchants/position_analytics (default 8,
                max 20).
            tracked_product_id: required for update_tracked/delete_tracked.
            products: required for add_tracked — 1-5000 items, each
                `{"externalProductId","name","description"?}`. Duplicates
                are skipped, not rejected.
            name, description: update_tracked fields (at least one required).
            search, external_product_id, match_status: list_tracked filters.
            extra_params: escape hatch for products_over_time/top_merchants
                (undocumented extra params, if any).
        """
        if op == "list_appearances":
            return _run(lambda c: c.list_shopping_items(
                page=page, size=size, search=search, sort_by=sort_by,
                from_=date_from, until=until, models=models,
                prompt_types=prompt_types, intent_types=intent_types,
            ))
        if op == "get_item":
            _require(op, item_id=item_id)
            return _run(lambda c: c.get_shopping_item(item_id))
        if op == "products_over_time":
            return _run(lambda c: c.shopping_products_over_time(
                from_=date_from, until=until, **(extra_params or {})))
        if op == "position_analytics":
            return _run(lambda c: c.shopping_product_position_analytics(
                from_=date_from, until=until, models=models,
                prompt_types=prompt_types, intent_types=intent_types, limit=limit,
            ))
        if op == "top_merchants":
            return _run(lambda c: c.shopping_top_merchant_domains(
                limit=limit, **(extra_params or {})))
        if op == "top_products":
            return _run(lambda c: c.shopping_top_products(
                from_=date_from, until=until, models=models,
                prompt_types=prompt_types, intent_types=intent_types, limit=limit,
            ))
        if op == "list_tracked":
            return _run(lambda c: c.list_tracked_products(
                search=search, external_product_id=external_product_id,
                match_status=match_status,
            ))
        if op == "add_tracked":
            _require(op, products=products)
            return _run(lambda c: c.add_tracked_products(products))
        if op == "update_tracked":
            _require(op, tracked_product_id=tracked_product_id)
            if name is None and description is None:
                raise _bad("promptwatch_shopping(op='update_tracked') requires `name` or `description`.")
            return _run(lambda c: c.update_tracked_product(
                tracked_product_id, name=name, description=description))
        if op == "delete_tracked":
            _require(op, tracked_product_id=tracked_product_id)
            return _run(lambda c: c.delete_tracked_product(tracked_product_id))
        raise _bad(f"Unknown op {op!r}.")

    # --- Sitemap + site health -----------------------------------------------

    @mcp.tool()
    def promptwatch_sitemap(
        op: Literal["progress", "list_urls", "health"],
        filter: Optional[Literal["errored", "redirected", "inProgress"]] = None,
        page: int = 1,
        size: Optional[int] = None,
        sort_by: Optional[Literal["canonical", "lastContentCrawlHttpStatus",
                                   "contentCrawlAttempts", "lastContentCrawledAt",
                                   "updatedAt", "createdAt"]] = None,
        sort_order: Optional[Literal["asc", "desc"]] = None,
        http_statuses: Optional[List[int]] = None,
        issue_types: Optional[List[Literal["missingTitle", "missingDescription",
                                            "noH1", "multipleH1", "thinContent"]]] = None,
    ) -> Any:
        """The crawled site: sitemap discovery/crawl progress, per-URL crawl
        status, and flagged on-page SEO issues (PromptWatch).

        Args:
            op: progress (aggregate crawl health) | list_urls (per-URL crawl
                status) | health (pages with flagged SEO issues).
            filter, page, size, sort_by, sort_order, http_statuses:
                op="list_urls" only (size default 50, max 100).
            issue_types: op="health" only (size default 20, max 100).
        """
        if op == "progress":
            return _run(lambda c: c.sitemap_crawl_progress())
        if op == "list_urls":
            return _run(lambda c: c.list_sitemap_urls(
                filter=filter, page=page, size=size if size is not None else 50,
                sort_by=sort_by, sort_order=sort_order, http_statuses=http_statuses,
            ))
        if op == "health":
            return _run(lambda c: c.site_health_pages(
                page=page, size=size if size is not None else 20,
                issue_types=issue_types))
        raise _bad(f"Unknown op {op!r}.")

    # --- Page tracker ------------------------------------------------------

    @mcp.tool()
    def promptwatch_page_tracker(
        op: Literal["list", "add", "get", "delete", "prompts", "responses"],
        page_id: Optional[str] = None,
        urls: Optional[List[str]] = None,
        page: int = 1,
        size: int = 25,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Any:
        """Track specific URLs (yours or a competitor's) and see which
        prompts/responses cite them — independent of PromptWatch's own
        generated content (PromptWatch).

        Args:
            op: list | add (requires urls, 1-100 — returns a 207-style
                `{added,skipped,failed}` breakdown, not all-or-nothing) | get
                (one tracked page's stats, requires page_id) | delete
                (requires page_id) | prompts (paginated prompts citing this
                page, requires page_id) | responses (paginated responses
                citing this page, requires page_id).
            page_id: required for get/delete/prompts/responses.
            urls: required for op="add".
            page, size: pagination for list/prompts/responses.
            start_date, end_date (YYYY-MM-DD, default today): op="get" only.
        """
        if op == "list":
            return _run(lambda c: c.list_tracked_pages(page=page, size=size))
        if op == "add":
            _require(op, urls=urls)
            return _run(lambda c: c.add_tracked_pages(urls))
        if op == "get":
            _require(op, page_id=page_id)
            return _run(lambda c: c.get_tracked_page(
                page_id, start_date=start_date, end_date=end_date))
        if op == "delete":
            _require(op, page_id=page_id)
            return _run(lambda c: c.delete_tracked_page(page_id))
        if op == "prompts":
            _require(op, page_id=page_id)
            return _run(lambda c: c.list_tracked_page_prompts(page_id, page=page, size=size))
        if op == "responses":
            _require(op, page_id=page_id)
            return _run(lambda c: c.list_tracked_page_responses(page_id, page=page, size=size))
        raise _bad(f"Unknown op {op!r}.")

    # --- Models --------------------------------------------------------------

    @mcp.tool()
    def promptwatch_models() -> Any:
        """Available LLM model identifiers — use these values for a
        monitor's `models` list or any `models=` filter elsewhere in this
        connector (PromptWatch)."""
        return _run(lambda c: c.list_models())

    # --- Action items --------------------------------------------------------

    @mcp.tool()
    def promptwatch_actions(
        op: Literal["list", "update"],
        page: int = 1,
        size: int = 25,
        action_id: Optional[str] = None,
        status: Optional[str] = None,
        extra_params: Optional[dict] = None,
    ) -> Any:
        """Action items surfaced by PromptWatch (e.g. suggested fixes) —
        list and dismiss/update them (PromptWatch).

        Args:
            op: list (defaults to non-dismissed items) | update (requires
                action_id and status).
            page, size: op="list" pagination.
            action_id: required for op="update".
            status: required for op="update" (exact accepted values aren't
                individually documented — "DISMISSED" is the common case).
            extra_params: escape hatch, merged into the update body.
        """
        if op == "list":
            return _run(lambda c: c.list_action_items(page=page, size=size))
        if op == "update":
            _require(op, action_id=action_id, status=status)
            return _run(lambda c: c.update_action_item(action_id, status, **(extra_params or {})))
        raise _bad(f"Unknown op {op!r}.")

    # --- Social citations ------------------------------------------------------

    @mcp.tool()
    def promptwatch_social(
        op: Literal["reddit", "youtube"],
        page: int = 1,
        size: int = 25,
        llm_monitor_id: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[Literal["asc", "desc"]] = None,
        date_from: Optional[str] = None,
        until: Optional[str] = None,
        models: Optional[List[str]] = None,
        prompt_types: Optional[List[str]] = None,
        tag_ids: Optional[List[str]] = None,
        query: Optional[str] = None,
        subreddit_name: Optional[str] = None,
        channel_name: Optional[str] = None,
    ) -> Any:
        """Reddit posts and YouTube videos cited by LLMs when answering
        tracked prompts (PromptWatch).

        Args:
            op: reddit | youtube.
            page, size, llm_monitor_id, sort_order, date_from, until, models,
                prompt_types, tag_ids, query: shared filters.
            sort_by: reddit ∈ citationCount|avgPosition|upvotes|numComments|
                publishedAt; youtube ∈ citationCount|avgPosition|viewCount|
                likeCount|commentCount|publishedAt (default citationCount
                both).
            subreddit_name: op="reddit" only, exact match.
            channel_name: op="youtube" only, partial match.
        """
        if op == "reddit":
            return _run(lambda c: c.list_reddit_citations(
                page=page, size=size, llm_monitor_id=llm_monitor_id,
                sort_by=sort_by, sort_order=sort_order, from_=date_from,
                until=until, models=models, prompt_types=prompt_types,
                tag_ids=tag_ids, query=query, subreddit_name=subreddit_name,
            ))
        if op == "youtube":
            return _run(lambda c: c.list_youtube_citations(
                page=page, size=size, llm_monitor_id=llm_monitor_id,
                sort_by=sort_by, sort_order=sort_order, from_=date_from,
                until=until, models=models, prompt_types=prompt_types,
                tag_ids=tag_ids, query=query, channel_name=channel_name,
            ))
        raise _bad(f"Unknown op {op!r}.")
