"""Provider `scaleway` — email hébergé Otomata (Scaleway TEM), credential/config-only.

Aucun tool propre : l'envoi est exécuté par `email_send` (spine) via le service
`otomata-auth-mailer` (transport "mailer"), pas de clé d'org. La gestion (expéditeurs
+ fenêtre calme) vit dans `orgs.email_settings` keyé par connecteur, surfacée par le
panneau email de la carte connecteur ORG. Ce module existe pour satisfaire l'invariant
« un fichier tools/ par provider kind=tools » (test_capabilities_drift) ; `register()`
n'enregistre rien.
"""
from __future__ import annotations

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:  # noqa: ARG001 — config-only, envoi par email_send
    return
