"""LightOn Paradigm — GenAI souveraine : chat (modèles hébergés UE) + base
documentaire RAG d'entreprise.

Wrappe `oto.tools.lighton.LightOnClient` (API Paradigm v2, docs.lighton.ai).
Credential à 2 champs (clé API + base URL optionnelle — Paradigm existe en
instance privée/on-prem, défaut = SaaS public) → modèle générique multi-champs
(ADR 0011), résolu par appel via `access.resolve_credential_fields("lighton")`.
BYO only (le compte Paradigm appartient au client — le credential EST le grant).

⚠️ Gotcha empirique (2026-07-23) : `GET /models` renvoie des templates de prompt
de plusieurs Ko par modèle → `lighton_models` trimme les champs `*_template`
(le reste passe brut). L'upload peut renvoyer 403 selon les droits du compte
Paradigm (rôle/plan côté LightOn, pas un bug du connecteur).
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, file_source


def register(mcp: FastMCP) -> None:
    from oto.tools.lighton import LightOnClient

    def _client() -> LightOnClient:
        creds = access.resolve_credential_fields("lighton")
        return LightOnClient(api_key=creds.get("api_key"),
                             base_url=creds.get("base_url") or None)

    def _run(fn):
        """Exécute un appel Paradigm : traduit une erreur en McpError
        actionnable (401 = clé invalide ; 403 = droits du compte Paradigm
        insuffisants ; 5xx = réessayer)."""
        client = _client()
        try:
            return fn(client)
        except McpError:
            raise
        except RuntimeError as e:
            msg = str(e)
            if msg.startswith("LightOn 401"):
                msg = "Clé LightOn invalide ou révoquée (401). Vérifie la clé posée."
            elif msg.startswith("LightOn 403"):
                msg = ("Le compte Paradigm de cette clé n'a pas le droit de faire "
                       f"cette opération (403 — rôle/plan côté LightOn). {msg}")
            elif msg.startswith("LightOn 5"):
                msg = (f"Paradigm est momentanément indisponible ({msg}). "
                       "Réessaie dans un moment.")
            raise McpError(ErrorData(code=INVALID_PARAMS, message=msg))
        except Exception as e:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"LightOn n'a pas pu traiter la requête ({e})."))

    @mcp.tool()
    def lighton_models() -> dict:
        """List the AI models configured on the LightOn Paradigm instance
        (sovereign GenAI, EU-hosted).

        Returns `data[]` with name, model_type, deployment_type, enabled —
        verbose prompt-template fields are stripped. Use a returned `name` as
        the `model` argument of `lighton_chat`.
        """
        raw = _run(lambda c: c.list_models())
        data = [
            {k: v for k, v in m.items() if not k.endswith("_template")}
            for m in raw.get("data", [])
        ]
        return {"object": raw.get("object", "list"), "data": data}

    @mcp.tool()
    def lighton_chat(
        messages: list[dict],
        model: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> dict:
        """Chat completion on a sovereign LightOn model (Paradigm, EU-hosted —
        OpenAI-compatible response).

        Args:
            messages: `[{"role": "system"|"user"|"assistant", "content": "…"}]`.
            model: model name from `lighton_models` (e.g. "alfred-ft5").
            max_tokens / temperature: standard sampling params (optional).

        Returns the raw completion: `choices[0].message.content` + `usage`.
        """
        return _run(lambda c: c.chat(
            messages, model, max_tokens=max_tokens, temperature=temperature))

    @mcp.tool()
    def lighton_query(
        query: str,
        collection: Optional[str] = None,
        n: int = 5,
    ) -> dict:
        """Semantic search over the Paradigm document base (RAG): retrieve the
        most relevant chunks for a natural-language query (LightOn).

        Args:
            query: the search query.
            collection: collection to search (Paradigm default:
                `base_collection`).
            n: number of chunks to return (default 5).

        Returns the raw chunks with their source document refs. Errors with
        `empty_collection_error` if no document has been uploaded yet
        (`lighton_upload_document`).
        """
        return _run(lambda c: c.query(query, collection=collection, n=n))

    @mcp.tool()
    def lighton_files(
        private_scope: Optional[bool] = None,
        company_scope: Optional[bool] = None,
        workspace_scope: Optional[int] = None,
        page: Optional[int] = None,
    ) -> dict:
        """List documents in the Paradigm document base (LightOn), paginated.

        Args:
            private_scope: include the user's private collection.
            company_scope: include the company collection.
            workspace_scope: include documents of this workspace id.
            page: page number.

        Returns `{count, next, previous, data[]}` (id, filename, status…).
        """
        return _run(lambda c: c.list_files(
            private_scope=private_scope, company_scope=company_scope,
            workspace_scope=workspace_scope, page=page))

    @mcp.tool()
    def lighton_ask_document(file_id: int, question: str) -> dict:
        """Ask a question about ONE document of the Paradigm base — the answer
        is generated from that document's content only (LightOn).

        Args:
            file_id: document id (from `lighton_files` or an upload).
            question: the question, in natural language.
        """
        return _run(lambda c: c.ask_document(file_id, question))

    @mcp.tool()
    def lighton_upload_document(
        source: dict,
        collection_type: Optional[str] = None,
        workspace_id: Optional[int] = None,
        title: Optional[str] = None,
    ) -> dict:
        """Upload a document into the Paradigm document base (LightOn) — it
        becomes searchable via `lighton_query` / `lighton_ask_document` once
        ingested.

        `source` (object, `kind` selects the origin):
        - Drive: `{"kind":"drive","file_id":"<id>"}`
        - Gmail attachment: `{"kind":"gmail","message_id":"<id>","filename":"<name>"}`
        - URL: `{"kind":"url","url":"https://…"}`
        Optional `account` (email) targets a specific Google account.

        Args:
            collection_type: `private` (Paradigm default), `company`, or
                `workspace`.
            workspace_id: required when collection_type='workspace'.
            title: display title (default: filename).

        NOTE: a 403 means the Paradigm account behind the key lacks upload
        rights (LightOn-side role/plan) — not a connector failure.
        """
        try:
            rf = file_source.resolve(source)
        except file_source.FileSourceError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        return _run(lambda c: c.upload_file_bytes(
            rf.data, rf.filename, collection_type=collection_type,
            workspace_id=workspace_id, title=title))

    @mcp.tool()
    def lighton_delete_document(file_id: int) -> dict:
        """Delete a document from the Paradigm document base (LightOn).
        Irreversible.

        Args:
            file_id: document id (from `lighton_files`).
        """
        _run(lambda c: c.delete_file(file_id))
        return {"deleted": file_id}
