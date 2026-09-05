"""Greenhouse Harvest API — ATS (candidats, jobs, candidatures, notes).

Wrappe `oto.tools.greenhouse.GreenhouseClient` (Harvest API key, Basic auth). Clé
résolue par appel via `access.resolve_api_key("greenhouse")` — byo (clé user sur
/account ou credential partagé de l'org). Pas de clé plateforme.

⚠️ Greenhouse exige un **`on_behalf_of`** (id d'un utilisateur Greenhouse) sur les
écritures (création de candidat, note) — récupérer un id via `greenhouse_users`.

**Surface consolidée (ADR 0047 §Amendement, appliqué au connecteur greenhouse)** :
un tool par OBJET métier, le verbe en paramètre `op` — `greenhouse_candidate`
(list/get/create/add_note), `greenhouse_job` (list/get), `greenhouse_application`
(list/get). Ce qui NE fusionne pas, et pourquoi :

- **`greenhouse_users` reste SEUL** : c'est le seul objet « utilisateur » du
  connecteur, il n'a qu'un verbe (lister) et il sert d'ANNUAIRE aux écritures des
  autres (`on_behalf_of` / `user_id`). Un `op=` à valeur unique n'homogénéiserait
  rien — même cas que `zoho_modules` / `gmail_list_accounts`.
- **job et application ne fusionnent pas entre eux** malgré des paramètres presque
  identiques (`per_page`/`page`/`job_id`/`status`) : ce sont deux objets métier
  distincts (`/jobs` vs `/applications`, et le `status` n'y a même pas le même
  domaine de valeurs — open/closed/draft vs active/rejected/hired). Les confondre
  derrière un `kind=` rendrait le schéma moins lisible, pas plus.

⚠️ Ce module ÉCRIT dans l'ATS : `greenhouse_candidate(op="create")` crée une fiche
candidat, `op="add_note"` publie une note dans son fil d'activité (lue par l'équipe
de recrutement). Le défaut de CHAQUE tool est `op="list"` — une LECTURE : un appel
sans `op` ne peut rien créer ni annoter.

⚠️ Un id que seule `op="get"` consomme (`candidate_id`, `job_id` de
`greenhouse_job`, `application_id`) est REFUSÉ sous `op="list"` plutôt qu'ignoré :
avant la consolidation, `greenhouse_candidate(candidate_id=456)` lisait UNE fiche ;
l'accepter en silence sous le nouveau défaut rendrait la liste entière en faisant
croire à l'agent que sa demande a été honorée.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..connectors import verify as connector_verify


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion » — otomata-tech/oto#69. Couvre `auth` SEUL.

    `GET /v1/users` (`list_users`, déjà dans le client), `per_page=1` — le plus
    petit format disponible, Greenhouse n'exposant ni `/me` ni solde. Basic
    auth (clé en username, mot de passe vide), lecture sans effet de bord.
    Aucune mention de coût ni de limite de débit particulière pour cet appel.

    **Authentifié ≠ utilisable** (classe oto#69) : ne distingue pas de scope —
    Greenhouse n'a pas de permission par clé au-delà du périmètre Harvest
    global de la clé elle-même.
    """
    from oto.tools.greenhouse.client import GreenhouseClient

    GreenhouseClient(api_key=fields["key"]).list_users(per_page=1)


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _need(value, name: str, op: str):
    """Argument obligatoire pour CET op — erreur actionnable, jamais de fallback.

    Une valeur VIDE compte comme absente : `candidate={}` créerait une fiche vide
    dans l'ATS et `body=""` publierait une note blanche dans le fil d'activité d'un
    candidat — deux écritures réelles qui passeraient pour un succès.
    """
    if value is None or (isinstance(value, (str, list, dict)) and not value):
        raise _bad(f"op='{op}' requiert {name}")
    return value


def _not_for(value, name: str, op: str, right_op: str) -> None:
    """Un id que SEULE `op=<right_op>` consomme, passé sous une autre op → refus.

    Greenhouse ne sait pas filtrer une liste par l'id de l'objet : le laisser
    passer rendrait la page entière sous couvert d'avoir répondu à la question.
    """
    if value is not None:
        raise _bad(f"op='{op}' ne filtre pas par {name} — utilise op='{right_op}' "
                   f"pour lire un objet par son id")


def register(mcp: FastMCP) -> None:
    from oto.tools.greenhouse.client import GreenhouseClient

    connector_verify.register("greenhouse", _verify)

    def _client() -> GreenhouseClient:
        key, _ = access.resolve_api_key("greenhouse")
        return GreenhouseClient(api_key=key)

    @mcp.tool()
    def greenhouse_candidate(
        op: Literal["list", "get", "create", "add_note"] = "list",
        candidate_id: Optional[int] = None,
        per_page: int = 50,
        page: int = 1,
        job_id: Optional[int] = None,
        email: Optional[str] = None,
        created_after: Optional[str] = None,
        updated_after: Optional[str] = None,
        candidate: Optional[dict] = None,
        on_behalf_of: Optional[int] = None,
        body: Optional[str] = None,
        user_id: Optional[int] = None,
        visibility: str = "public",
    ) -> Any:
        """A Greenhouse candidate — list, read, create, annotate.

        `op`:
        - **"list"** (default): list candidates, paginated. Filters: `job_id` (only
          candidates with an application on this job), `email` (exact match),
          `created_after` / `updated_after` (ISO 8601 timestamps).
        - **"get"**: fetch one candidate by id (`candidate_id`), with their
          applications.
        - **"create"**: create a candidate/prospect. **WRITES** to the ATS.
        - **"add_note"**: add a note to a candidate's activity feed. **WRITES** —
          the note is readable by the hiring team at the chosen `visibility`.

        ⚠️ Greenhouse requires an **acting user id** on every write (`On-Behalf-Of`
        header): `on_behalf_of` for op="create", `user_id` for op="add_note" (the
        note's author doubles as the acting user). Get an id from `greenhouse_users`.

        Args:
            op: list (default) | get | create | add_note.
            candidate_id: op="get"/"add_note" — the candidate.
            per_page: op="list" — page size (default 50, capped at 500 upstream).
            page: op="list" — 1-based page number.
            job_id: op="list" — only candidates with an application on this job.
            email: op="list" — filter by exact email.
            created_after: op="list" — ISO 8601 timestamp.
            updated_after: op="list" — ISO 8601 timestamp.
            candidate: op="create" — Greenhouse candidate object (first_name,
                last_name, email_addresses, phone_numbers, applications, …).
            on_behalf_of: op="create" — Greenhouse user id to act as (required for
                writes — see greenhouse_users).
            body: op="add_note" — the note text.
            user_id: op="add_note" — Greenhouse user id authoring the note.
            visibility: op="add_note" — admin_only | private | public.
        """
        client = _client()

        if op == "list":
            _not_for(candidate_id, "candidate_id", op, "get")
            return client.list_candidates(
                per_page=per_page, page=page, job_id=job_id, email=email,
                created_after=created_after, updated_after=updated_after)

        if op == "get":
            return client.get_candidate(_need(candidate_id, "candidate_id", op))

        if op == "create":
            return client.add_candidate(
                _need(candidate, "candidate", op),
                on_behalf_of=_need(on_behalf_of, "on_behalf_of", op))

        if op == "add_note":
            return client.add_note(
                _need(candidate_id, "candidate_id", op),
                _need(body, "body", op),
                _need(user_id, "user_id", op),
                visibility=visibility)

        raise _bad("op doit être 'list', 'get', 'create' ou 'add_note'")

    @mcp.tool()
    def greenhouse_job(
        op: Literal["list", "get"] = "list",
        job_id: Optional[int] = None,
        per_page: int = 50,
        page: int = 1,
        status: Optional[str] = None,
    ) -> Any:
        """A Greenhouse job (an open position) — list or read one.

        `op`:
        - **"list"** (default): list jobs, paginated. `status` filters
          open | closed | draft.
        - **"get"**: fetch one job by id (`job_id`).

        Args:
            op: list (default) | get.
            job_id: op="get" — the job.
            per_page: op="list" — page size (default 50, capped at 500 upstream).
            page: op="list" — 1-based page number.
            status: op="list" — open | closed | draft.
        """
        client = _client()

        if op == "list":
            _not_for(job_id, "job_id", op, "get")
            return client.list_jobs(per_page=per_page, page=page, status=status)

        if op == "get":
            return client.get_job(_need(job_id, "job_id", op))

        raise _bad("op doit être 'list' ou 'get'")

    @mcp.tool()
    def greenhouse_application(
        op: Literal["list", "get"] = "list",
        application_id: Optional[int] = None,
        per_page: int = 50,
        page: int = 1,
        job_id: Optional[int] = None,
        status: Optional[str] = None,
    ) -> Any:
        """A Greenhouse application (a candidate on a job) — list or read one.

        `op`:
        - **"list"** (default): list applications, paginated. Filters: `job_id`,
          `status` (active | rejected | hired).
        - **"get"**: fetch one application by id (`application_id`).

        Args:
            op: list (default) | get.
            application_id: op="get" — the application.
            per_page: op="list" — page size (default 50, capped at 500 upstream).
            page: op="list" — 1-based page number.
            job_id: op="list" — only applications on this job.
            status: op="list" — active | rejected | hired.
        """
        client = _client()

        if op == "list":
            _not_for(application_id, "application_id", op, "get")
            return client.list_applications(
                per_page=per_page, page=page, job_id=job_id, status=status)

        if op == "get":
            return client.get_application(
                _need(application_id, "application_id", op))

        raise _bad("op doit être 'list' ou 'get'")

    @mcp.tool()
    def greenhouse_users(per_page: int = 50, page: int = 1) -> list:
        """List Greenhouse users (recruiters) — get an id for `on_behalf_of`.

        Paginated (`per_page` default 50, capped at 500 upstream). This is the
        directory the writes of `greenhouse_candidate` (op="create" / "add_note")
        read their acting user id from.
        """
        return _client().list_users(per_page=per_page, page=page)
