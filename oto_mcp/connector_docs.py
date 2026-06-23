"""Doc « how-to » user-facing des connecteurs — CONTENU curé, séparé du registre.

Overlay keyé par nom de connecteur (comme `_CATEGORY_BY_CONNECTOR` etc.), sorti de
`providers.py` pour garder le registre lisible et concentrer le contenu (rempli au
fil de l'eau, potentiellement volumineux) dans un seul fichier. `Connector.doc_sections`
en DÉRIVE (property). Sérialisé au catalogue public + `/api/me/connectors`, rendu
partout où le connecteur s'affiche (carte de connexion, connector-library, vitrine).

Convention par `kind` :
- `prerequisite` — ce qu'il faut AVANT de connecter (où prendre la clé, une autorisation
  à poser côté fournisseur…). Affiché avant connexion.
- `setup`        — étapes de configuration.
- `usage`        — ce que le connecteur permet + exemples concrets. Affiché aussi en
  découverte (library/vitrine).
- `note`         — divers.

`body_md` = markdown léger : `[label](url)`, `**gras**`, `` `code` ``, listes `- `.
Rester FACTUEL : décrire ce que font réellement les outils, lier la page API/docs de
l'éditeur plutôt qu'inventer un chemin d'UI exact.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DocSection:
    kind: str            # prerequisite | setup | usage | note
    title: str
    body_md: str


# name de connecteur → sections. Vide pour un connecteur = rien d'affiché.
DOC_SECTIONS: dict[str, tuple[DocSection, ...]] = {
    "atlassian": (
        DocSection(
            kind="prerequisite",
            title="autoriser le callback côté Atlassian",
            body_md=(
                "avant de connecter, un **admin** de ton org Atlassian doit autoriser "
                "l'URL de callback d'oto dans les réglages Rovo MCP Server (sinon le "
                "consentement OAuth échoue).\n"
                "- url à autoriser : `https://mcp.oto.ninja/api/atlassian/oauth/callback`\n"
                "- où : [admin.atlassian.com → Security → Rovo MCP](https://admin.atlassian.com)\n"
                "- [doc Atlassian](https://support.atlassian.com/security-and-access-policies/docs/control-atlassian-rovo-mcp-server-settings/)"
            ),
        ),
        DocSection(
            kind="usage",
            title="ce que tu peux faire",
            body_md=(
                "pilote **Jira** et **Confluence** en langage naturel. par exemple :\n"
                "- crée un ticket Jira dans un projet\n"
                "- recherche des issues en JQL\n"
                "- lis ou crée une page Confluence"
            ),
        ),
    ),
}
