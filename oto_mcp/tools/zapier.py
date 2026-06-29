"""Zapier — automatisation via l'AI Actions API (actions exposées + exécution).

Wrappe `oto.tools.zapier.ZapierClient`. Credential = clé API simple (en-tête
`x-api-key`), keyed → résolu par appel via `access.resolve_api_key("zapier")`.
byo (user/org), pas de clé plateforme : chacun pose sa clé (le jeu d'actions
exposées est attaché à la clé, créée sur actions.zapier.com).

Modèle : Zapier expose pour les agents un catalogue d'**actions** que l'user a
explicitement autorisées, exécutables en langage naturel — pas une API de gestion
des Zaps. `zapier_list_actions` découvre les actions, `zapier_execute_action` en
lance une.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.zapier import ZapierClient

    def _client() -> ZapierClient:
        key, _ = access.resolve_api_key("zapier")
        return ZapierClient(api_key=key)

    @mcp.tool()
    def zapier_list_actions() -> dict:
        """List the actions exposed by this Zapier key (id, description, params).

        Each action carries an `id` (pass it to zapier_execute_action) and the
        list of its configurable fields."""
        return _client().list_actions()

    @mcp.tool()
    def zapier_execute_action(
        action_id: str,
        instructions: str,
        params: Optional[dict] = None,
        preview_only: bool = False,
    ) -> dict:
        """Execute an exposed Zapier action.

        Args:
            action_id: action id (see zapier_list_actions).
            instructions: natural-language directive — Zapier fills the fields
                left in "AI guess" mode from this text.
            params: explicit overrides for the action's fields (take precedence
                over what is inferred from instructions).
            preview_only: True = don't run, return what would be done.
        """
        return _client().execute_action(
            action_id, instructions, params=params, preview_only=preview_only)

    @mcp.tool()
    def zapier_execution_log(execution_log_id: str) -> dict:
        """Get the detail of one execution (execution_log_id from execute)."""
        return _client().execution_log(execution_log_id)
