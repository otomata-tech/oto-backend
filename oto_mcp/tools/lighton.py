"""LightOn — indexation documentaire souveraine (API v3, api.lighton.ai) :
retrieval hybride (search), RAG groundé (ask), parse → Markdown, extraction
structurée, ingestion par workspace.

Wrappe `oto.tools.lighton.LightOnClient` (v3 — l'API v2 de l'applicatif
Paradigm est dépréciée et n'est plus couverte). Credential à 3 champs
(clé API + base URL optionnelle instance privée + `workspace_id` par défaut
optionnel) → modèle générique multi-champs (ADR 0011), résolu par appel via
`access.resolve_credential_fields("lighton")`. BYO only (le compte LightOn
appartient au client — le credential EST le grant).

Le `workspace_id` du credential fait de l'instance (ADR 0038) « une clé × un
workspace » : une instance liée à un projet scope par défaut search/ask/upload
sur son workspace ; l'argument explicite du tool prime toujours.

Facturation LightOn (lighton.ai/pricing) : ingestion à la page, retrieval à
la requête (search ET ask), stockage vectoriel au Go.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, file_source


def register(mcp: FastMCP) -> None:
    from oto.tools.lighton import LightOnClient

    def _creds() -> dict:
        return access.resolve_credential_fields("lighton")

    def _client(creds: dict) -> LightOnClient:
        return LightOnClient(api_key=creds.get("api_key"),
                             base_url=creds.get("base_url") or None)

    def _default_workspace(creds: dict) -> Optional[int]:
        raw = (creds.get("workspace_id") or "").strip()
        return int(raw) if raw.isdigit() else None

    def _run(fn):
        """Exécute un appel LightOn : traduit une erreur en McpError
        actionnable (401 clé / 403 droits du compte / 5xx retry)."""
        creds = _creds()
        client = _client(creds)
        try:
            return fn(client, _default_workspace(creds))
        except McpError:
            raise
        except RuntimeError as e:
            msg = str(e)
            if msg.startswith("LightOn 401"):
                msg = "Clé LightOn invalide ou révoquée (401). Vérifie la clé posée."
            elif msg.startswith("LightOn 403"):
                msg = ("Le compte LightOn de cette clé n'a pas accès à cette "
                       f"ressource/opération (403). {msg}")
            elif msg.startswith("LightOn 5"):
                msg = (f"LightOn est momentanément indisponible ({msg}). "
                       "Réessaie dans un moment.")
            raise McpError(ErrorData(code=INVALID_PARAMS, message=msg))
        except Exception as e:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"LightOn n'a pas pu traiter la requête ({e})."))

    def _resolve_source(source: dict):
        try:
            return file_source.resolve(source)
        except file_source.FileSourceError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))

    def _strip_images(payload: dict) -> dict:
        """Retire les images de page base64 des résultats retrieval — l'API v3
        les joint D'OFFICE sur /ask (~250 Ko PAR chunk, vécu 23/07 : 3 chunks
        = 820 Ko de réponse), intenable dans un contexte LLM."""
        for r in payload.get("results", []) or []:
            if isinstance(r, dict):
                r.pop("image", None)
        return payload

    @mcp.tool()
    def lighton_search(
        query: str,
        workspace_ids: Optional[list[int]] = None,
        file_ids: Optional[list[int]] = None,
        max_results: int = 5,
        mode: str = "text",
    ) -> dict:
        """Semantic retrieval over the LightOn document index (sovereign,
        EU-hosted): hybrid dense + keyword search with multivector reranking.
        Returns ranked chunks with provenance (file, pages, scores) — NO LLM
        generation (compose the answer yourself, or use `lighton_ask`).

        Args:
            query: natural-language query (max 1500 chars).
            workspace_ids: restrict to these workspaces. Default: the
                connector's configured workspace if set, else the whole
                corpus the key can access. Cannot combine with file_ids.
            file_ids: restrict to specific documents.
            max_results: chunks returned after reranking (1-50).
            mode: "text" (default) or "vision" (searches VLM-embedded page
                images — scanned docs, diagrams).

        Billed: 1 retrieval credit per call.
        """
        return _strip_images(_run(lambda c, ws: c.search(
            query,
            workspace_ids=workspace_ids or (None if file_ids else ([ws] if ws else None)),
            file_ids=file_ids, max_results=max_results,
            mode=mode if mode != "text" else None)))

    @mcp.tool()
    def lighton_ask(
        query: str,
        workspace_ids: Optional[list[int]] = None,
        file_ids: Optional[list[int]] = None,
        max_results: int = 5,
        model: Optional[str] = None,
    ) -> dict:
        """Full RAG over the LightOn document index: retrieves the most
        relevant chunks then generates an LLM answer grounded in them, with
        source citations. Returns `{answer, results[]}`.

        Args:
            query: natural-language question (max 1500 chars).
            workspace_ids / file_ids: scoping — same rules as
                `lighton_search` (connector's configured workspace by
                default).
            max_results: context chunks (1-50).
            model: generation LLM (e.g. "mistral-large-latest"); platform
                default if omitted.

        Billed: 1 retrieval credit per call. For chunks without generation
        (cheaper composition by the agent), use `lighton_search`.
        """
        return _strip_images(_run(lambda c, ws: c.ask(
            query,
            workspace_ids=workspace_ids or (None if file_ids else ([ws] if ws else None)),
            file_ids=file_ids, max_results=max_results, model=model)))

    @mcp.tool()
    def lighton_parse(source: dict) -> dict:
        """Parse a document into clean structured Markdown (LightOn OCR
        pipeline — PDF, Office, images, HTML). One-shot processing: the
        document is NOT added to the search index (use
        `lighton_upload_document` for that).

        `source` (object, `kind` selects the origin):
        - Drive: `{"kind":"drive","file_id":"<id>"}`
        - Gmail attachment: `{"kind":"gmail","message_id":"<id>","filename":"<name>"}`
        - URL: `{"kind":"url","url":"https://…"}`

        Sync limits: ~20 MB / 15 pages. Returns `{status, result, usage}` —
        the Markdown is in `result`.
        """
        rf = _resolve_source(source)
        return _run(lambda c, ws: c.parse_bytes(rf.data, rf.filename))

    @mcp.tool()
    def lighton_extract(source: dict, schema: dict) -> dict:
        """Extract structured fields from a document into a typed JSON Schema
        (LightOn). One-shot processing, document NOT indexed.

        Args:
            source: same shape as `lighton_parse` (drive/gmail/url).
            schema: JSON Schema object describing the fields to extract,
                e.g. `{"type":"object","properties":{"invoice_number":
                {"type":"string"}}}`.

        Sync limits: ~20 MB / 15 pages. Returns `{status, result, usage}`.
        """
        rf = _resolve_source(source)
        return _run(lambda c, ws: c.extract_bytes(rf.data, rf.filename, schema))

    @mcp.tool()
    def lighton_files(
        workspace_ids: Optional[list[int]] = None,
        search: Optional[str] = None,
        status: Optional[str] = None,
        filename: Optional[str] = None,
        page: Optional[int] = None,
    ) -> dict:
        """List documents in the LightOn index (paginated). `search` orders
        results by semantic relevance (quick "find my doc").

        Args:
            workspace_ids: filter by workspaces (default: the connector's
                configured workspace if set, else all accessible).
            search: semantic relevance query.
            status: ingestion status filter (e.g. "pending,embedded").
            filename: case-insensitive partial filename match.

        Returns `{count, next, previous, results[]}` (id, filename, title,
        workspace, status, total_pages…).
        """
        return _run(lambda c, ws: c.list_files(
            workspace_ids=workspace_ids or ([ws] if ws else None),
            search=search, status=status, filename=filename, page=page))

    @mcp.tool()
    def lighton_upload_document(
        source: dict,
        workspace_id: Optional[int] = None,
        title: Optional[str] = None,
    ) -> dict:
        """Upload + index a document into a LightOn workspace — it becomes
        searchable via `lighton_search`/`lighton_ask` once its status reaches
        `embedded` (check with `lighton_files`).

        Args:
            source: same shape as `lighton_parse` (drive/gmail/url).
            workspace_id: destination workspace. REQUIRED unless the
                connector instance has a configured default workspace.
                List available ones with `lighton_workspaces`.
            title: display title (default: filename).

        Billed per ingested page.
        """
        # gate workspace AVANT de résoudre la source (pas de download inutile)
        wid = workspace_id or _default_workspace(_creds())
        if not wid:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message="workspace_id requis (aucun workspace par défaut "
                        "configuré sur le connecteur) — liste-les via "
                        "lighton_workspaces."))
        rf = _resolve_source(source)
        return _run(lambda c, ws: c.upload_file_bytes(
            rf.data, rf.filename, wid, title=title))

    @mcp.tool()
    def lighton_delete_document(file_id: int) -> dict:
        """Permanently delete a document and its index from LightOn.
        Irreversible.

        Args:
            file_id: document id (from `lighton_files`).
        """
        _run(lambda c, ws: c.delete_file(file_id))
        return {"deleted": file_id}

    @mcp.tool()
    def lighton_workspaces(name: Optional[str] = None) -> dict:
        """List LightOn workspaces accessible to the configured key —
        isolated document collections (manually fed, or synced from
        SharePoint / Google Drive). Use a returned `id` as `workspace_id`
        in upload/search/ask.

        Args:
            name: filter by name.
        """
        return _run(lambda c, ws: c.list_workspaces(name=name))
