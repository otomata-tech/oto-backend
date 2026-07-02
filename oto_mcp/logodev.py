"""Dérivation d'URL de logo via le CDN logo.dev.

Utilisé par le profil d'org (`org_store.effective_logo_url`, domaine déclaré
par l'org_admin). Même format que `providers.Connector.logo_url_for` (domaine
de marque curé) — qui reste self-contained par sa règle « module pur, aucun
import oto_mcp ».

Le token `LOGODEV_TOKEN` (env) est *publishable* — conçu pour vivre dans
l'URL, pas un secret. Sans token ou sans domaine → None (monogramme côté UI).
"""
from __future__ import annotations

import os


def logo_url(domain: str | None, *, size: int = 256) -> str | None:
    token = os.environ.get("LOGODEV_TOKEN")
    if not domain or not token:
        return None
    return (f"https://img.logo.dev/{domain}"
            f"?token={token}&size={size}&format=png&retina=true")
