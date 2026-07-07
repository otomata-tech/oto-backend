"""Catalogue des documents légaux + versions courantes + jeux requis par contexte.

⚠️ Doit rester ALIGNÉ avec la source de vérité du CONTENU (oto-websites
`packages/ui/src/legal/*`) : mêmes slugs, mêmes numéros de version courants. Le
backend n'héberge pas le texte — il n'a besoin que du slug + version pour tracer
l'acceptation et calculer le reste-à-accepter.

Bumper une version côté oto-websites ⇒ bumper `CURRENT_VERSIONS` ici ⇒ les
utilisateurs sont re-sollicités (la version acceptée ne correspond plus à la
courante). C'est le mécanisme de re-sollicitation au changement de version.
"""
from __future__ import annotations

import os

# Version courante par document (miroir d'oto-websites `<doc>/index.ts::current`).
CURRENT_VERSIONS: dict[str, str] = {
    "terms": "2.0",     # CGU — conditions d'utilisation
    "cgv": "1.0",       # CGV — conditions de vente (abonnements payants)
    "dpa": "1.0",       # accord de sous-traitance (art. 28 RGPD)
    "privacy": "2.0",   # politique de confidentialité (info, non « accepté »)
    "legal": "2.0",     # mentions légales (info)
}

# Libellé par défaut (fr) — le dashboard localise ; fourni pour les surfaces sans i18n.
LABELS: dict[str, str] = {
    "terms": "Conditions d'utilisation (CGU)",
    "cgv": "Conditions de vente (CGV)",
    "dpa": "Accord de sous-traitance (DPA)",
    "privacy": "Politique de confidentialité",
    "legal": "Mentions légales",
}

# Documents dont l'ACCEPTATION est requise, par contexte. `privacy`/`legal` sont
# informatifs (RGPD/obligation d'info) → jamais « acceptés », donc absents.
REQUIRED_BY_CONTEXT: dict[str, tuple[str, ...]] = {
    "access": ("terms",),                 # inscription / accès à la plateforme
    "purchase": ("terms", "cgv", "dpa"),  # souscription d'un abonnement payant
}


def base_url() -> str:
    """Domaine canonique des pages légales (oto.cx). Surchargeable par env."""
    return os.environ.get("OTO_LEGAL_BASE_URL", "https://oto.cx").rstrip("/")


def doc_url(slug: str) -> str:
    return f"{base_url()}/{slug}"


def required_slugs(context: str) -> tuple[str, ...]:
    return REQUIRED_BY_CONTEXT.get(context, ())


def current_version(slug: str) -> str | None:
    return CURRENT_VERSIONS.get(slug)
