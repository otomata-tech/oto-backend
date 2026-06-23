"""Provider `resend` — credential-only (clé Resend de l'org), aucun tool propre.

La clé est résolue par `email_send` (transport=resend) via
`access.resolve_api_key("resend")` (cascade user > org). Ce module existe
uniquement pour satisfaire l'invariant « un fichier tools/ par provider
kind=tools » (test_capabilities_drift) ; `register_all` l'importe et appelle
`register()` qui n'enregistre rien.
"""
from __future__ import annotations

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:  # noqa: ARG001 — credential consommé par email_send
    return
